"""
DownloadWorker —— 在后台线程中运行 yt-dlp，并通过 Qt 信号向前台 GUI
推送进度 / 状态 / 错误信息。

使用 yt-dlp 的 Python API + progress_hooks，比解析 subprocess 文本更
准确（可拿到精确的已下载字节数、总字节数、速度、ETA）。
"""

import os
from urllib.parse import urlparse, parse_qs, urlunparse

from PyQt6.QtCore import QThread, pyqtSignal


def normalize_youtube_url(url):
    """
    把 YouTube URL 规范化为「单个视频」的干净链接。

    用户从浏览器复制的 URL 常带播放列表/电台参数，例如：
      https://www.youtube.com/watch?v=IkIxTn6OiWE&list=RDIkIxTn6OiWE&start_radio=1

    这些参数会让 yt-dlp 尝试拉取整个播放列表（[youtube:tab] 提取），
    产生大量请求、易触发 SSL 限流（UNEXPECTED_EOF_WHILE_READING），
    且与「只下载单个视频」的意图不符。

    本函数只保留视频 ID，丢弃 list=/start_radio=/index=/t= 等参数：
      watch?v=XXX&list=...  →  watch?v=XXX
      youtu.be/XXX?list=... →  youtu.be/XXX
     youtu.be/XXX           →  youtu.be/XXX  (原样)
    """
    if not url:
        return url
    url = url.strip()

    try:
        parsed = urlparse(url)
        host = (parsed.netloc or "").lower()
        is_yt = "youtube.com" in host or "youtu.be" in host
        if not is_yt:
            return url

        # 普通长链接：watch?v=XXX
        if "youtube.com" in host and parsed.path.startswith("/watch"):
            qs = parse_qs(parsed.query)
            vid = (qs.get("v") or [""])[0]
            if vid:
                return f"https://www.youtube.com/watch?v={vid}"
            return url

        # 短链接：youtu.be/XXX
        if "youtu.be" in host:
            vid = parsed.path.lstrip("/")
            if vid:
                return f"https://youtu.be/{vid}"
            return url

        # embed / shorts 等其他形式：原样返回，交给 yt-dlp 处理
        return url
    except Exception:
        return url



def ffmpeg_available():
    """检测系统是否可用 ffmpeg（yt-dlp 合并流 / 提取音频需要它）。"""
    import shutil
    try:
        import yt_dlp.utils as _u
        # yt-dlp 自带的探测，会查 PATH 和常见安装位置
        if _u.find_exe("ffmpeg"):
            return True
    except Exception:
        pass
    return shutil.which("ffmpeg") is not None


def detect_js_runtimes():
    """
    探测系统可用的 JavaScript runtime（node / deno / bun）。

    新版 yt-dlp（2026.x）解析 YouTube 需要 JS runtime 来执行签名脚本。
    默认只启用 deno，若系统只装了 node 则必须显式指定，否则会出现：
      - WARNING: No supported JavaScript runtime could be found
      - 部分/全部格式 URL 签名失效 → HTTP 403 Forbidden

    返回 yt-dlp 的 js_runtimes 参数（dict 格式，如 {'node': {}}）。
    探测不到任何 runtime 时返回 None（交由 yt-dlp 走默认/降级）。
    """
    import shutil
    # 按优先级探测
    for runtime in ("deno", "node", "bun"):
        if shutil.which(runtime):
            return {runtime: {}}
    return None



def build_format_string(quality, audio_only, has_ffmpeg=True):
    """
    根据 GUI 选择构造 yt-dlp 的 -f 格式字符串。

    有 ffmpeg 时（默认）优先合并的最高画质流：
      - best  → bestvideo+bestaudio/best
      - worst → worstvideo+worstaudio/worst
      - 1080p 等 → bestvideo[height<=N]+bestaudio/best[height<=N]

    无 ffmpeg 时降级到不需要合并的单流（画质/音质可能略低，但能下载）：
      - 音频：无 ffmpeg 无法提取 MP3，由调用方拦截，这里仍返回 bestaudio
      - 视频：best[ext=mp4]/best 等，避免触发「需要合并」的错误
    """
    if audio_only:
        return "bestaudio/best"

    if not has_ffmpeg:
        # 无 ffmpeg：只能下单一文件流，不能合并 video+audio
        if quality == "worst":
            return "worst[ext=mp4]/worst"
        if quality == "best":
            return "best[ext=mp4]/best"
        height = quality.replace("p", "")
        return f"best[height<={height}][ext=mp4]/best[height<={height}]/best"

    if quality == "best":
        return "bestvideo+bestaudio/best"
    if quality == "worst":
        return "worstvideo+worstaudio/worst"
    # 具体分辨率：1080p → 1080
    height = quality.replace("p", "")
    return f"bestvideo[height<={height}]+bestaudio/best[height<={height}]"


def human_size(num_bytes):
    """把字节数转成人类可读的 KB/MB/GB。"""
    try:
        n = float(num_bytes)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024.0:
            return f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} PB"


def human_speed(speed):
    """把字节/秒转成 MB/s 等。"""
    return human_size(speed) + "/s" if speed else ""


class _YDLLogger:
    """
    yt-dlp 自定义 logger：捕获 error/warning/debug 输出。

    yt-dlp 的很多错误（ffmpeg 缺失、下载失败、签名错误等）不是抛 Python
    异常，而是通过 logger.error() 报告。把这些收集起来，下载失败时连同
    一起报告给用户，避免只看到笼统的「下载失败」而看不到真实原因。
    """
    def __init__(self):
        self.errors = []
        self.warnings = []

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        self.warnings.append(msg)

    def error(self, msg):
        self.errors.append(msg)


class _DownloadCancelled(Exception):
    """用户取消下载时，在 progress_hook 里抛出以中断 yt-dlp 下载循环。"""


def _setup_thread_stdio():
    """
    在 worker 线程的 run() 开头调用：把 OS 层 stdout/stderr(fd 1/2)
    重定向到 NUL 设备。

    【关键修复】yt-dlp 解析 YouTube 时会调用 node 子进程执行 JS 签名
    脚本（jsc challenge）。在 PyQt6 的 QThread 里，子进程继承的
    stdout/stderr 管道会与 Qt 的 IO 监控死锁，导致 extract_info /
    download 永久 hang（表现为「一直显示正在获取视频信息」）。

    把 fd 1/2 重定向到 NUL 后，子进程的输出不经过管道，死锁消失。
    验证：未重定向时 QThread 内 extract_info 120s+ 卡死；
          重定向后 2.3 秒完成。
    """
    try:
        nul = os.open(os.devnull, os.O_RDWR)
        for fd in (1, 2):
            try:
                os.dup2(nul, fd)
            except OSError:
                pass
    except OSError:
        pass


class DownloadWorker(QThread):
    """
    后台下载线程。

    信号：
      info_ready(str)   —— 拿到视频标题后发出，用于在 UI 显示
      stage(str)        —— 当前阶段提示（如「正在下载视频流」「正在合并」）
      progress(int, str, str, str) —— (百分比0-100, 已下载/总量文本, 速度文本, ETA文本)
      finished_ok(str)  —— 下载成功，附带最终文件路径
      failed(str)       —— 下载失败，附带错误信息
    """

    info_ready = pyqtSignal(str)
    stage = pyqtSignal(str)
    progress = pyqtSignal(int, str, str, str)
    finished_ok = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, url, output_dir, quality, video_format, audio_only, parent=None):
        super().__init__(parent)
        self.url = url
        self.output_dir = output_dir
        self.quality = quality
        self.video_format = video_format
        self.audio_only = audio_only
        self._ydl = None  # yt-dlp 实例引用
        self._final_filepath = None
        self._cancelled = False  # 取消标志（progress_hook 检查它来快速中断）
        # 多流进度跟踪：合并格式会分别下载 video + audio，每个流各自触发
        # 0→100%，需归一化为整体进度，避免「到100又从0开始」。
        self._seen_files = []      # 已出现的下载文件名（按顺序）
        self._current_file_idx = 0  # 当前下载的是第几个文件（0-based）
        self._total_streams = 1     # 将要下载的流数（video+audio=2，单流=1），
                                    # 由 extract_info 的 requested_formats 确定
        self._stream_kinds = []     # 每个流的类型（'video'/'audio'），与流顺序对应

    def _progress_hook(self, d):
        """yt-dlp progress_hooks 回调：在下载线程内被调用。
        yt-dlp 在每个数据块下载后都会调用它，是「快速取消」的最佳钩子点：
        检查到取消标志时抛异常，可在几百毫秒内中断下载（而不必等整个
        download() 阻塞调用返回）。"""
        if self._cancelled:
            raise _DownloadCancelled()
        status = d.get("status")
        filename = d.get("filename", "")

        if status == "downloading":
            # 跟踪当前文件索引（首次见到该 filename 时登记）
            if filename and filename not in self._seen_files:
                self._seen_files.append(filename)
                self._current_file_idx = len(self._seen_files) - 1
                # 切换到新流时，提示阶段（视频流/音频流）
                kind = (self._stream_kinds[self._current_file_idx]
                        if self._current_file_idx < len(self._stream_kinds)
                        else "")
                if kind == "video":
                    self.stage.emit("正在下载视频流")
                elif kind == "audio":
                    self.stage.emit("正在下载音频流")

            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes", 0)
            speed = d.get("speed") or 0
            eta = d.get("eta")

            # 单文件进度
            file_pct = (int(downloaded * 100 / total) if total > 0 else 0)

            # 归一化为整体进度：把每个流平均分配进度区间。
            # 例如 2 个流（video+audio）：流1 占 0-50%，流2 占 50-100%。
            # _total_streams 由 extract_info 的 requested_formats 确定，
            # 确保 video 流下载时就按正确的总流数归一化，不会到 100% 再回 0。
            total_streams = max(self._total_streams, 1)
            overall_pct = int((self._current_file_idx + file_pct / 100) / total_streams * 100)
            overall_pct = min(overall_pct, 99)  # 不让进度在下载阶段就显示 100%（留给完成）

            if total > 0:
                size_text = f"{human_size(downloaded)} / {human_size(total)}"
            else:
                size_text = human_size(downloaded)

            speed_text = human_speed(speed)
            eta_text = f"{eta}s" if isinstance(eta, (int, float)) else "--"
            self.progress.emit(overall_pct, size_text, speed_text, eta_text)

        elif status == "finished":
            # 单个文件片段下载完成（合并前），记下文件路径
            self._final_filepath = d.get("filename")

    def _postprocessor_hook(self, d):
        """yt-dlp postprocessor_hooks 回调：捕获合并/提取音频等后处理阶段。
        下载完所有流后，yt-dlp 会调 ffmpeg 合并或提取，这是较耗时的阶段，
        需要给用户明确提示（否则进度卡在 99% 让人以为卡住了）。"""
        if self._cancelled:
            return
        status = d.get("status")
        if status != "started":
            return
        pp_name = (d.get("postprocessor") or "").lower()
        if "merge" in pp_name or "ffmpeg" in pp_name:
            self.stage.emit("正在合并视频和音频")
            self.progress.emit(99, "", "", "")  # 合并阶段进度固定在 99%
        elif "extractaudio" in pp_name or "audio" in pp_name:
            self.stage.emit("正在提取音频")
        elif "embed" in pp_name:
            self.stage.emit("正在写入元数据")

    def cancel(self):
        """请求取消下载。在 GUI 线程中调用。
        设置 _cancelled 标志后，下一次 progress_hook 回调（几百毫秒内）
        会抛 _DownloadCancelled 异常，快速中断 yt-dlp 的下载循环。"""
        self._cancelled = True
        self.requestInterruption()

    def run(self):
        """线程主函数：执行 yt-dlp。整个函数体包在兜底 try 中，
        保证线程内任何异常都通过 failed 信号反馈给 GUI（弹框），
        而不是静默崩溃导致界面「闪退」。"""
        _setup_thread_stdio()  # 修复 QThread 内子进程死锁
        try:
            self._run_inner()
        except Exception as e:
            # 记录到崩溃日志，便于排查
            try:
                import os, traceback as _tb
                from datetime import datetime
                log_path = os.path.join(
                    os.path.expanduser("~"), "YouTubeDownloader_crash.log"
                )
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(f"\n{'=' * 60}\n[worker] {datetime.now().isoformat()}\n")
                    _tb.print_exc(file=f)
            except Exception:
                pass
            self.failed.emit(f"发生错误: {e}")

    def _run_inner(self):
        # 延迟导入，避免打包/启动阶段强依赖 yt-dlp
        try:
            import yt_dlp
        except ImportError:
            self.failed.emit("未找到 yt-dlp，请先运行: pip install yt-dlp")
            return

        # 规范化 URL：去掉 list=/start_radio= 等播放列表参数，
        # 避免 yt-dlp 拉取整个播放列表触发 SSL 限流/403。
        url = normalize_youtube_url(self.url)

        # 检测 ffmpeg：合并 video+audio 流、提取 MP3 都需要它。
        # 无 ffmpeg 时降级到单流格式（视频）或直接报错（MP3）。
        has_ffmpeg = ffmpeg_available()
        if self.audio_only and not has_ffmpeg:
            self.failed.emit(
                "下载 MP3 需要 ffmpeg，但系统未检测到 ffmpeg。\n\n"
                "请安装 ffmpeg 并确保它在系统 PATH 中，或手动指定路径。\n"
                "下载地址：https://ffmpeg.org/download.html"
            )
            return

        format_string = build_format_string(self.quality, self.audio_only, has_ffmpeg)

        # 降级提示：无 ffmpeg 时视频画质可能受限
        if not has_ffmpeg and not self.audio_only:
            self.info_ready.emit("（未检测到 ffmpeg，已降级为单流下载，画质可能略低）")

        # 规范化保存目录：把正斜杠转成 Windows 原生反斜杠，
        # 避免混用斜杠 + 中文路径触发 OSError [Errno 22] Invalid argument。
        # 同时展开 ~ 等，确保目录存在。
        try:
            output_dir = os.path.normpath(os.path.expanduser(self.output_dir))
        except Exception:
            output_dir = self.output_dir

        # 输出模板：标题截断到 80 字符，避免 Windows 路径超长。
        # 目录与模板名之间用 os.path.join 保证用平台正确的分隔符。
        outtmpl = os.path.join(output_dir, "%(title).80s.%(ext)s")

        # 自定义 logger 在每次重试循环里单独创建（见下方 attempt_logger），
        # 这里只定义基础 opts。
        ydl_opts = {
            "format": format_string,
            "outtmpl": outtmpl,
            "no_playlist": True,
            "progress_hooks": [self._progress_hook],
            "postprocessor_hooks": [self._postprocessor_hook],
            "noprogress": True,   # 不让 yt-dlp 直接打印进度（进度经 progress_hooks 拿）
            "quiet": True,        # 静默：避免向 stdout 打印（windowed 打包下无控制台）
            "no_warnings": False,  # 保留警告，由 logger 捕获
            # 网络容错：YouTube 限流/SSL 中断时自动重试，缓解
            # UNEXPECTED_EOF_WHILE_READING 等偶发网络错误
            "retries": 10,              # 下载重试次数
            "fragment_retries": 10,     # 分片下载重试次数
            "extractor_retries": 3,     # 提取阶段重试次数
            "socket_timeout": 30,       # 单次请求超时（秒）
            "ignoreerrors": False,
        }

        # 启用 JS runtime（node/deno）：解析 YouTube 签名必需，否则易 403
        js_runtimes = detect_js_runtimes()
        if js_runtimes:
            ydl_opts["js_runtimes"] = js_runtimes

        if self.audio_only:
            ydl_opts.update({
                "extractaudio": True,
                "audio_format": "mp3",
                "audio_quality": "0",  # 最高质量
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "0",
                }],
            })
        else:
            ydl_opts["merge_output_format"] = self.video_format

        # 外层重试：YouTube 会对可疑连接做 TLS 限流（SSL: UNEXPECTED_EOF），
        # 这是概率性的。yt-dlp 内部的 retries 只在分片级重试，某些 SSL 失败
        # 会让整个下载 abort。这里在 SSL/网络错误时整体重新发起下载。
        MAX_ATTEMPTS = 5
        last_err = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if self._cancelled or self.isInterruptionRequested():
                self.failed.emit("已取消下载")
                return

            # 每次重试用新实例 + 新 logger，避免内部状态/错误累积污染
            attempt_logger = _YDLLogger()
            attempt_opts = dict(ydl_opts)
            attempt_opts["logger"] = attempt_logger

            try:
                with yt_dlp.YoutubeDL(attempt_opts) as ydl:
                    self._ydl = ydl
                    # 先取信息（标题等），让 UI 尽早显示（仅首次）
                    if attempt == 1:
                        try:
                            info = ydl.extract_info(url, download=False)
                            if info:
                                title = info.get("title") or "未知标题"
                                self.info_ready.emit(title)
                                # 确定将下载的流数（video+audio 分开则是 2）
                                req_formats = info.get("requested_formats") or []
                                if req_formats:
                                    self._total_streams = len(req_formats)
                                    # 记录每个流的类型，用于下载时提示「视频流/音频流」
                                    self._stream_kinds = []
                                    for fmt in req_formats:
                                        vcodec = (fmt.get("vcodec") or "none").lower()
                                        acodec = (fmt.get("acodec") or "none").lower()
                                        if vcodec != "none":
                                            self._stream_kinds.append("video")
                                        elif acodec != "none":
                                            self._stream_kinds.append("audio")
                                        else:
                                            self._stream_kinds.append("")
                                try:
                                    self._final_filepath = ydl.prepare_filename(info)
                                    if self.audio_only:
                                        base, _ = os.path.splitext(self._final_filepath)
                                        self._final_filepath = base + ".mp3"
                                except Exception:
                                    pass
                        except Exception:
                            pass

                    if self._cancelled or self.isInterruptionRequested():
                        self.failed.emit("已取消下载")
                        return

                    # download 返回错误码：0=成功，非0=有错误（即使没抛异常）
                    retcode = ydl.download([url])

                if self._cancelled or self.isInterruptionRequested():
                    self.failed.emit("已取消下载")
                    return

                # 成功判定：retcode==0 且无 error
                if retcode == 0 and not attempt_logger.errors:
                    self.finished_ok.emit(self._final_filepath or self.output_dir)
                    return

                # 失败：收集错误信息，判断是否值得重试（网络/SSL 类错误）
                last_err = "\n".join(attempt_logger.errors) if attempt_logger.errors else f"错误码 {retcode}"

            except _DownloadCancelled:
                # 用户取消：立即停止，不重试
                self.failed.emit("已取消下载")
                return
            except Exception as e:
                # 用户取消（其他路径触发）也立即停止
                if self._cancelled:
                    self.failed.emit("已取消下载")
                    return
                last_err = str(e)
                retcode = 1
                # 重新拿 logger 错误
                extra = "\n".join(attempt_logger.errors) if attempt_logger.errors else ""
                if extra:
                    last_err = f"{e}\n{extra}"

            # 用户取消则不再重试
            if self._cancelled or self.isInterruptionRequested():
                self.failed.emit("已取消下载")
                return

            # 还有机会则提示重试
            if attempt < MAX_ATTEMPTS:
                self.info_ready.emit(f"网络异常，正在重试（第 {attempt + 1}/{MAX_ATTEMPTS} 次）…")

        # 全部重试用尽
        self.failed.emit(f"下载失败（已重试 {MAX_ATTEMPTS} 次）：\n{last_err}")



def _extract_resolutions(info):
    """
    从 yt-dlp 的 info 字典中解析出真实可用的视频分辨率列表。

    返回降序、去重的字符串列表，例如 ['2160p', '1080p', '720p', '480p']。
    若无法解析则返回空列表。
    """
    if not info:
        return []
    formats = info.get("formats") or []
    heights = set()

    def _consider(fmt):
        # 只要带正整数高度的流都纳入候选（视频或视频+音频的混合流都算）
        h = fmt.get("height")
        if isinstance(h, int) and h > 0:
            heights.add(h)

    # 优先：仅视频流（vcodec 有效、acodec 为 none）
    for fmt in formats:
        if fmt.get("vcodec") and fmt.get("vcodec") != "none" and fmt.get("acodec") == "none":
            _consider(fmt)

    # 兜底：没有独立视频流时，退化为任意带 height 的流
    if not heights:
        for fmt in formats:
            _consider(fmt)

    # 再兜底：info 顶层自身就带 height（单格式场景）
    if not heights:
        top_h = info.get("height")
        if isinstance(top_h, int) and top_h > 0:
            heights.add(top_h)

    return [f"{h}p" for h in sorted(heights, reverse=True)]


class ProbeWorker(QThread):
    """
    轻量探测线程：只调用 extract_info(download=False) 获取视频信息，
    不进行任何下载。用于「检测分辨率」按钮。

    信号：
      info_ready(str)  —— 视频标题
      probed(list)     —— 可用分辨率字符串列表（降序，如 ['1080p','720p']）
      failed(str)      —— 失败信息
    """

    info_ready = pyqtSignal(str)
    probed = pyqtSignal(list)
    failed = pyqtSignal(str)

    def __init__(self, url, parent=None):
        super().__init__(parent)
        self.url = url

    def run(self):
        _setup_thread_stdio()  # 修复 QThread 内子进程死锁
        try:
            import yt_dlp
        except ImportError:
            self.failed.emit("未找到 yt-dlp，请先运行: pip install yt-dlp")
            return

        probe_logger = _YDLLogger()
        ydl_opts = {
            "quiet": True,
            "no_warnings": False,
            "noplaylist": True,
            "skip_download": True,
            "logger": probe_logger,
            # 网络容错（与下载一致）
            "retries": 10,
            "extractor_retries": 3,
            "socket_timeout": 30,
        }

        # 启用 JS runtime：检测阶段也需要正确解析格式
        js_runtimes = detect_js_runtimes()
        if js_runtimes:
            ydl_opts["js_runtimes"] = js_runtimes

        # 规范化 URL：去掉播放列表参数，避免拉取整个播放列表
        url = normalize_youtube_url(self.url)

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)

            if not info:
                extra = "\n".join(probe_logger.errors) if probe_logger.errors else ""
                self.failed.emit(
                    f"无法获取视频信息，请检查链接是否有效。\n{extra}" if extra
                    else "无法获取视频信息，请检查链接是否有效。"
                )
                return

            title = info.get("title") or "未知标题"
            self.info_ready.emit(title)

            resolutions = _extract_resolutions(info)
            if not resolutions:
                self.failed.emit("未检测到可用分辨率。")
                return

            self.probed.emit(resolutions)

        except Exception as e:
            extra = "\n".join(probe_logger.errors) if probe_logger.errors else ""
            self.failed.emit(f"检测失败: {e}\n{extra}" if extra else f"检测失败: {e}")

