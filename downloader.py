"""
DownloadWorker —— 在后台线程中运行 yt-dlp，并通过 Qt 信号向前台 GUI
推送进度 / 状态 / 错误信息。

使用 yt-dlp 的 Python API + progress_hooks，比解析 subprocess 文本更
准确（可拿到精确的已下载字节数、总字节数、速度、ETA）。
"""

import os
from urllib.parse import urlparse, parse_qs, urlunparse

from PyQt6.QtCore import QThread, pyqtSignal

from i18n import tr


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



# YouTube 字幕语言代码 → 友好名称的映射（常见语言）。
# 未列出的语言代码原样显示（如 'af' 显示 'af'）。
_SUBTITLE_LANG_NAMES = {
    "zh-Hans": "简体中文", "zh-Hant": "繁体中文", "zh": "中文",
    "zh-CN": "简体中文", "zh-TW": "繁体中文",
    "en": "英语", "en-US": "英语(美)", "en-GB": "英语(英)",
    "ja": "日语", "ko": "韩语",
    "fr": "法语", "de": "德语", "es": "西班牙语", "it": "意大利语",
    "pt": "葡萄牙语", "pt-BR": "葡萄牙语(巴)", "ru": "俄语",
    "ar": "阿拉伯语", "hi": "印地语", "th": "泰语", "vi": "越南语",
    "id": "印尼语", "ms": "马来语", "tr": "土耳其语", "nl": "荷兰语",
    "pl": "波兰语", "uk": "乌克兰语",
}


def subtitle_display_name(lang_code):
    """把语言代码转成友好显示名，如 'zh-Hans' → '简体中文'。"""
    return _SUBTITLE_LANG_NAMES.get(lang_code, lang_code)


def extract_subtitle_languages(info):
    """
    从 yt-dlp 的 info 字典中提取可用字幕语言代码列表。

    优先人工字幕（subtitles，准确度高），再补自动字幕（automatic_captions）。
    返回语言代码列表（如 ['zh-Hans', 'en', 'ja']），无字幕时返回空列表。
    两种来源的语言去重（同一语言有人工字幕就不重复列自动字幕）。
    """
    if not info:
        return []
    manual = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}
    # 人工字幕优先
    langs = list(manual.keys())
    # 补充自动字幕里人工没有的语言
    for lang in auto.keys():
        if lang not in manual:
            langs.append(lang)
    # 常见语言优先排序：中英日韩在前，其余按字母序
    priority = ["zh-Hans", "zh-Hant", "zh", "en", "ja", "ko"]
    langs.sort(key=lambda x: (0 if x in priority else 1, priority.index(x) if x in priority else 99, x))
    return langs


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
        # 明确选纯音频流：[acodec!=none] 约束避免兜底到含视频的混合流。
        # 不加约束时，某些视频在签名失效场景下 bestaudio 会错误选中视频流，
        # 导致「从视频提取音频」（慢 + 易 403）。
        return "bestaudio[acodec!=none]/bestaudio/best"

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
    后台下载线程，支持单/多 URL 队列。

    信号：
      info_ready(str)   —— 单个视频标题（或队列进度提示）
      stage(str)        —— 当前阶段提示（如「[2/5] 正在下载视频流」）
      progress(int, str, str, str) —— (百分比0-100, 已下载/总量文本, 速度文本, ETA文本)
      finished_ok(str)  —— 全部成功，附带最终文件路径
      failed(str)       —— 失败信息（全部失败或部分失败汇总）
    """

    info_ready = pyqtSignal(str)
    stage = pyqtSignal(str)
    progress = pyqtSignal(int, str, str, str)
    finished_ok = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, urls, output_dir, quality, video_format, audio_only,
                 subtitle_lang=None, parent=None):
        super().__init__(parent)
        # urls：URL 列表（支持批量）；单视频时长度为 1
        self.urls = list(urls) if urls else []
        self.output_dir = output_dir
        self.quality = quality
        self.video_format = video_format
        self.audio_only = audio_only
        self.subtitle_lang = subtitle_lang  # 字幕语言代码，None=不下字幕
        self._ydl = None
        self._final_filepath = None
        self._cancelled = False
        # 多流进度跟踪
        self._seen_files = []
        self._current_file_idx = 0
        self._total_streams = 1
        self._stream_kinds = ["audio"] if audio_only else ["video"]
        self._base_opts = None  # 缓存基础 ydl_opts

    def _progress_hook(self, d):
        if self._cancelled:
            raise _DownloadCancelled()
        status = d.get("status")
        filename = d.get("filename", "")

        if status == "downloading":
            if filename and filename not in self._seen_files:
                self._seen_files.append(filename)
                self._current_file_idx = len(self._seen_files) - 1
                kind = (self._stream_kinds[self._current_file_idx]
                        if self._current_file_idx < len(self._stream_kinds)
                        else "")
                if kind == "video":
                    self.stage.emit(tr("stage_video"))
                elif kind == "audio":
                    self.stage.emit(tr("stage_audio"))

            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes", 0)
            speed = d.get("speed") or 0
            eta = d.get("eta")

            file_pct = (int(downloaded * 100 / total) if total > 0 else 0)
            total_streams = max(self._total_streams, 1)
            overall_pct = int((self._current_file_idx + file_pct / 100) / total_streams * 100)
            overall_pct = min(overall_pct, 99)

            if total > 0:
                size_text = f"{human_size(downloaded)} / {human_size(total)}"
            else:
                size_text = human_size(downloaded)

            speed_text = human_speed(speed)
            eta_text = f"{eta}s" if isinstance(eta, (int, float)) else "--"
            self.progress.emit(overall_pct, size_text, speed_text, eta_text)

        elif status == "finished":
            # MP3 模式下 filename 是转码前的 .m4a，不能覆盖最终路径
            if not self.audio_only:
                self._final_filepath = d.get("filename")

    def _postprocessor_hook(self, d):
        if self._cancelled:
            return
        status = d.get("status")
        if status != "started":
            return
        pp_name = (d.get("postprocessor") or "").lower()
        if "merge" in pp_name or "ffmpegvideo" in pp_name:
            self.stage.emit(tr("stage_merge"))
            self.progress.emit(99, "", "", "")
        elif "extractaudio" in pp_name:
            self.stage.emit(tr("stage_mp3"))
            self.progress.emit(99, "", "", "")
        elif "embed" in pp_name:
            self.stage.emit(tr("stage_meta"))

    def cancel(self):
        self._cancelled = True
        self.requestInterruption()

    def run(self):
        _setup_thread_stdio()
        try:
            self._run_inner()
        except Exception as e:
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
            self.failed.emit(tr("worker_err", err=str(e)))

    def _run_inner(self):
        try:
            import yt_dlp
        except ImportError:
            self.failed.emit(tr("no_ytdlp"))
            return

        if not self.urls:
            self.failed.emit(tr("no_urls"))
            return

        has_ffmpeg = ffmpeg_available()
        if self.audio_only and not has_ffmpeg:
            self.failed.emit(tr("need_ffmpeg_mp3"))
            return

        format_string = build_format_string(self.quality, self.audio_only, has_ffmpeg)
        if not has_ffmpeg and not self.audio_only:
            self.info_ready.emit(tr("downgrade"))

        try:
            output_dir = os.path.normpath(os.path.expanduser(self.output_dir))
        except Exception:
            output_dir = self.output_dir

        outtmpl = os.path.join(output_dir, "%(title).80s.%(ext)s")

        ydl_opts = {
            "format": format_string,
            "outtmpl": outtmpl,
            "no_playlist": True,
            "progress_hooks": [self._progress_hook],
            "postprocessor_hooks": [self._postprocessor_hook],
            "noprogress": True,
            "quiet": True,
            "no_warnings": False,
            "retries": 10,
            "fragment_retries": 10,
            "extractor_retries": 3,
            "socket_timeout": 30,
            "ignoreerrors": False,
        }

        js_runtimes = detect_js_runtimes()
        if js_runtimes:
            ydl_opts["js_runtimes"] = js_runtimes

        if self.audio_only:
            ydl_opts.update({
                "extractaudio": True,
                "audio_format": "mp3",
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }],
            })
        else:
            ydl_opts["merge_output_format"] = self.video_format
            # 字幕：仅视频模式且指定语言时下载并嵌入（嵌入需 ffmpeg）。
            # 注意 key 必须是 'FFmpegEmbedSubtitle'（带 FFmpeg 前缀），
            # 写成 'EmbedSubtitle' 会触发 KeyError('EmbedSubtitlePP')。
            # subtitlesformat=srt：YouTube 自动字幕默认 json3，需转成可嵌入的 srt。
            if self.subtitle_lang and has_ffmpeg:
                ydl_opts.update({
                    "writesubtitles": True,
                    "writeautomaticsub": True,
                    "subtitleslangs": [self.subtitle_lang],
                    "subtitlesformat": "srt",
                    "postprocessors": list(ydl_opts.get("postprocessors", [])) + [
                        {"key": "FFmpegEmbedSubtitle"},
                    ],
                })

        self._base_opts = ydl_opts

        # 队列遍历：依次下载每个 URL，单个失败不中断，最后汇总
        total = len(self.urls)
        ok_count = 0
        failures = []
        for idx, raw_url in enumerate(self.urls, start=1):
            if self._cancelled or self.isInterruptionRequested():
                break
            url = normalize_youtube_url(raw_url)
            prefix = f"[{idx}/{total}] " if total > 1 else ""
            success, err = self._download_one(yt_dlp, url, prefix)
            if success:
                ok_count += 1
            else:
                if self._cancelled or self.isInterruptionRequested():
                    break
                failures.append((raw_url, err))

        # 汇总结果
        if self._cancelled or self.isInterruptionRequested():
            self.failed.emit(tr("cancelled_partial", done=ok_count, total=total) if ok_count else tr("cancelled"))
            return
        if ok_count == total:
            self.finished_ok.emit(self._final_filepath or self.output_dir)
        elif ok_count == 0:
            self.failed.emit(f"全部下载失败（共 {total} 个）：\n" + "\n".join(f"- {u}: {e[:80]}" for u, e in failures))
        else:
            detail = "\n".join(f"- {u}: {e[:80]}" for u, e in failures)
            self.failed.emit(f"部分完成 {ok_count}/{total}，失败 {len(failures)} 个：\n{detail}")

    def _download_one(self, yt_dlp, url, prefix):
        """下载单个 URL（含外层重试）。返回 (是否成功, 错误信息)。
        prefix 是队列前缀（如 '[2/5] '），用于 stage/info 提示。"""
        MAX_ATTEMPTS = 5
        last_err = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if self._cancelled or self.isInterruptionRequested():
                return False, tr("cancelled")

            attempt_logger = _YDLLogger()
            attempt_opts = dict(self._base_opts)
            attempt_opts["logger"] = attempt_logger

            # 重置单视频流跟踪状态
            self._seen_files = []
            self._current_file_idx = 0
            self._total_streams = 1
            self._stream_kinds = ["audio"] if self.audio_only else ["video"]

            try:
                with yt_dlp.YoutubeDL(attempt_opts) as ydl:
                    self._ydl = ydl
                    if attempt == 1:
                        try:
                            info = ydl.extract_info(url, download=False)
                            if info:
                                title = info.get("title") or "未知标题"
                                self.info_ready.emit(f"{prefix}{title}")
                                req_formats = info.get("requested_formats") or []
                                if req_formats:
                                    self._total_streams = len(req_formats)
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
                                elif self.audio_only:
                                    self._stream_kinds = ["audio"]
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
                        return False, tr("cancelled")

                    retcode = ydl.download([url])

                if self._cancelled or self.isInterruptionRequested():
                    return False, tr("cancelled")

                if retcode == 0 and not attempt_logger.errors:
                    return True, ""

                last_err = "\n".join(attempt_logger.errors) if attempt_logger.errors else f"错误码 {retcode}"

            except _DownloadCancelled:
                return False, tr("cancelled")
            except Exception as e:
                if self._cancelled:
                    return False, tr("cancelled")
                last_err = str(e)
                retcode = 1
                extra = "\n".join(attempt_logger.errors) if attempt_logger.errors else ""
                if extra:
                    last_err = f"{e}\n{extra}"

            if self._cancelled or self.isInterruptionRequested():
                return False, tr("cancelled")

            if attempt < MAX_ATTEMPTS:
                self.info_ready.emit(tr("retry_one", prefix=prefix, n=attempt + 1, max=MAX_ATTEMPTS))

        return False, f"重试 {MAX_ATTEMPTS} 次仍失败：{last_err[:120]}"
def _extract_resolutions(info):
    """
    从 yt-dlp 的 info 字典中解析出真实可用的视频分辨率列表，并估算每档
    分辨率的下载体积。

    返回降序的元组列表 [(分辨率字符串, 体积字节数), ...]，例如：
        [('2160p', 1370000000), ('1080p', 286000000), ('720p', 168000000)]
    若某档体积无法估算则为 0。无法解析任何分辨率时返回空列表。

    体积估算逻辑（与 yt-dlp 实际下载的选流一致）：
      - 视频流：在 height<=该分辨率的纯视频流中，选 vbr 最高的一档
      - 音频流：取所有纯音频流中 abr 最高的一档
      - 两者 filesize/filesize_approx 之和为该分辨率估算体积
    """
    if not info:
        return []
    formats = info.get("formats") or []

    def _fmt_size(fmt):
        """取格式体积，优先 filesize，其次 filesize_approx。"""
        s = fmt.get("filesize")
        if s is None:
            s = fmt.get("filesize_approx")
        if isinstance(s, (int, float)) and s > 0:
            return int(s)
        return 0

    def _vbr(fmt):
        v = fmt.get("vbr")
        return v if isinstance(v, (int, float)) else 0

    def _abr(fmt):
        a = fmt.get("abr")
        return a if isinstance(a, (int, float)) else 0

    def _vcodec_priority(fmt):
        """视频编码优先级，模拟 yt-dlp 的默认视频流偏好：
        av01（新一代，体积小质量好）> vp9 > vp09 > avc1/h264。
        yt-dlp 实际倾向选 av01 流（同分辨率下体积最小），而非 vbr 最高的流。
        返回数值越大优先级越高。"""
        vc = (fmt.get("vcodec") or "").lower()
        if vc.startswith("av01"):
            return 4
        if vc.startswith("vp09"):
            return 3
        if vc.startswith("vp9"):
            return 2
        if vc.startswith("avc1") or vc.startswith("h264"):
            return 1
        return 0

    # 纯视频流（vcodec 有效、acodec 为 none）
    video_streams = [f for f in formats
                     if (f.get("vcodec") or "none") != "none"
                     and (f.get("acodec") or "none") == "none"
                     and isinstance(f.get("height"), int) and f["height"] > 0]
    # 纯音频流（acodec 有效、vcodec 为 none）
    audio_streams = [f for f in formats
                     if (f.get("acodec") or "none") != "none"
                     and (f.get("vcodec") or "none") == "none"]

    # 最优音频流体积（所有分辨率共用同一档音频）
    best_audio_size = 0
    if audio_streams:
        best_audio = max(audio_streams, key=_abr)
        best_audio_size = _fmt_size(best_audio)

    # 兜底：没有独立视频流时，退化为任意带 height 的流
    use_streams = video_streams if video_streams else [
        f for f in formats if isinstance(f.get("height"), int) and f["height"] > 0
    ]

    if not use_streams:
        # 再兜底：info 顶层自身就带 height（单格式场景）
        top_h = info.get("height")
        if isinstance(top_h, int) and top_h > 0:
            return [(f"{top_h}p", 0)]
        return []

    heights = sorted({f["height"] for f in use_streams}, reverse=True)
    result = []
    for h in heights:
        # bestvideo[height<=h]：模拟 yt-dlp 选流。
        # yt-dlp 默认偏好 av01 等新编码（体积小质量好），而非 vbr 最高，
        # 所以按 (vcodec优先级, vbr) 联合排序选最优一档。
        candidates = [f for f in use_streams if f["height"] <= h]
        if not candidates:
            result.append((f"{h}p", 0))
            continue
        best_video = max(candidates, key=lambda f: (_vcodec_priority(f), _vbr(f)))
        total = _fmt_size(best_video) + best_audio_size
        result.append((f"{h}p", total))

    return result


class ProbeWorker(QThread):
    """
    轻量探测线程：只调用 extract_info(download=False) 获取视频信息，
    不进行任何下载。用于「检测分辨率」按钮。

    信号：
      info_ready(str)  —— 视频标题
      probed(list)     —— [(分辨率字符串, 体积字节), ...]（降序，
                          如 [('1080p', 286000000), ('720p', 168000000)]）
      failed(str)      —— 失败信息
    """

    info_ready = pyqtSignal(str)
    probed = pyqtSignal(list)
    subs_ready = pyqtSignal(list)   # 可用字幕语言代码列表，无字幕时为空
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
                self.failed.emit(tr("no_res"))
                return

            self.probed.emit(resolutions)

            # 检测可用字幕语言（人工 + 自动），供 UI 选择
            subs = extract_subtitle_languages(info)
            self.subs_ready.emit(subs)

        except Exception as e:
            extra = "\n".join(probe_logger.errors) if probe_logger.errors else ""
            self.failed.emit(f"检测失败: {e}\n{extra}" if extra else f"检测失败: {e}")

