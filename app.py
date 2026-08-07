"""
YouTube 视频下载器 —— PyQt6 GUI 入口。

运行：
    python app.py

打包：
    运行 build_exe.bat 生成 dist/YouTubeDownloader.exe
"""

import os
import sys
import traceback
from datetime import datetime

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont, QIcon
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QLineEdit, QComboBox, QPushButton, QProgressBar,
    QRadioButton, QButtonGroup, QFileDialog, QMessageBox, QFrame,
)

from downloader import DownloadWorker, ProbeWorker


# best 选项固定显示在最前，之后追加检测到的真实分辨率
BEST_LABEL = "best (最高画质)"
FORMAT_OPTIONS = ["mp4", "webm", "mkv"]


def _fmt_size(num_bytes):
    """把字节数转成人类可读的 KB/MB/GB，用于画质体积显示。"""
    try:
        n = float(num_bytes)
    except (TypeError, ValueError):
        return "?"
    if n <= 0:
        return "未知大小"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024.0:
            # 小于 1MB 显示整数 KB，否则一位小数
            if unit == "KB":
                return f"{int(n)} KB"
            return f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} PB"


def _resource_path(relative_name):
    """获取资源文件的绝对路径，兼容「源码运行」和「PyInstaller 打包后运行」。

    源码运行：资源在脚本同目录（如 app.ico 与 app.py 同级）。
    打包后：PyInstaller 解压到临时目录 sys._MEIPASS，资源在那里。
    用 --add-data 把资源打进 exe 后，必须用此函数定位。
    """
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative_name)


def _bootstrap_stdio():
    """
    PyInstaller --windowed 模式下没有控制台，这会导致两类问题：

    1. Python 层 sys.stdout/stderr 为 None → yt-dlp 等 print() 时抛
       AttributeError（之前的「点下载闪退」根因）。

    2. 【更隐蔽】OS 层文件句柄 (fd 1/2) 无效 → yt-dlp 调用的子进程
       （node 执行 JS 签名脚本、ffmpeg 合并）继承到无效的 stdout/stderr，
       子进程在写输出时 hang 住 → extract_info / download 永久卡住，
       表现为「一直显示正在获取视频信息」。

    本函数两层都做兜底：
      - OS 层：把 fd 0/1/2 重定向到 NUL 设备，子进程继承到有效句柄
      - Python 层：sys.stdout/stderr 设为 NullStream，print() 不报错
    """
    class _NullStream:
        def write(self, *a, **k):
            pass
        def flush(self, *a, **k):
            pass

    # 判断是否处于无控制台（windowed）环境：sys.stdout 为 None 是典型特征。
    is_windowed = sys.stdout is None or sys.stderr is None

    # ---- OS 层：重定向文件句柄，让子进程继承有效 stdout/stderr ----
    # 这是修复「windowed 模式下 node/ffmpeg 子进程卡住」的关键。
    if is_windowed:
        try:
            nul = os.open(os.devnull, os.O_RDWR)
            for fd in (0, 1, 2):
                try:
                    os.dup2(nul, fd)
                except OSError:
                    pass
        except OSError:
            pass

    # ---- Python 层兜底 ----
    if sys.stdout is None:
        sys.stdout = _NullStream()
    if sys.stderr is None:
        sys.stderr = _NullStream()



def _install_excepthook():
    """
    安装全局异常钩子：任何未捕获异常（包括 GUI 线程崩溃）都写入
    crash.log，避免「闪退且无任何线索」。
    """
    def hook(exc_type, exc_value, exc_tb):
        try:
            log_path = os.path.join(os.path.expanduser("~"), "YouTubeDownloader_crash.log")
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"\n{'=' * 60}\n")
                f.write(f"{datetime.now().isoformat()}\n")
                traceback.print_exception(exc_type, exc_value, exc_tb, file=f)
        except Exception:
            pass
    sys.excepthook = hook


class DownloaderWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.worker = None       # 当前后台下载线程
        self.prober = None       # 当前后台检测线程
        self._current_title = "" # 当前下载视频的标题（用于阶段提示拼接）
        self._build_ui()
        self._refresh_controls()

    # ---------------------------------------------------------------- UI
    def _build_ui(self):
        self.setWindowTitle("YouTube 视频下载器")
        self.setMinimumWidth(600)
        self.setMinimumHeight(480)
        self.resize(640, 520)  # 启动时给足空间，避免内容拥挤

        # 窗口图标（任务栏、标题栏都会显示）
        icon_path = _resource_path("app.ico")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 20)
        root.setSpacing(16)

        title_font = QFont()
        title_font.setPointSize(14)
        title_font.setBold(True)
        title_label = QLabel("YouTube 视频下载器")
        title_label.setFont(title_font)
        root.addWidget(title_label)

        # ---- URL + 检测按钮 ----
        url_label = QLabel("YouTube 链接")
        root.addWidget(url_label)
        url_row = QHBoxLayout()
        self.url_edit = QLineEdit()
        self.url_edit.setMinimumHeight(30)
        self.url_edit.setPlaceholderText("https://www.youtube.com/watch?v=...")
        # textEdited：用户手敲或粘贴改动时触发；程序化 setText 不触发。
        # 一旦链接变了，之前检测到的分辨率就失效，需清空。
        self.url_edit.textEdited.connect(self._on_url_edited)
        url_row.addWidget(self.url_edit, stretch=1)
        self.probe_btn = QPushButton("检测分辨率")
        self.probe_btn.setMinimumHeight(30)
        self.probe_btn.clicked.connect(self._on_probe)
        url_row.addWidget(self.probe_btn)
        root.addLayout(url_row)

        # ---- 类型选择（视频 / MP3）----
        type_row = QHBoxLayout()
        type_row.addWidget(QLabel("类型:"))
        self.rb_video = QRadioButton("视频")
        self.rb_audio = QRadioButton("仅音频 (MP3)")
        self.rb_video.setChecked(True)
        self.type_group = QButtonGroup(self)
        self.type_group.addButton(self.rb_video)
        self.type_group.addButton(self.rb_audio)
        self.rb_video.toggled.connect(self._on_type_changed)
        type_row.addWidget(self.rb_video)
        type_row.addWidget(self.rb_audio)
        type_row.addStretch()
        root.addLayout(type_row)

        # ---- 画质 + 格式 ----
        opt_form = QFormLayout()
        opt_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        opt_form.setHorizontalSpacing(12)
        opt_form.setVerticalSpacing(12)
        self.quality_combo = QComboBox()
        self.quality_combo.setMinimumHeight(30)
        self.quality_combo.setEnabled(False)  # 初始：未检测，禁用
        opt_form.addRow("画质:", self.quality_combo)

        self.format_combo = QComboBox()
        self.format_combo.setMinimumHeight(30)
        self.format_combo.addItems(FORMAT_OPTIONS)
        self.format_combo.setCurrentText("mp4")
        opt_form.addRow("格式:", self.format_combo)
        root.addLayout(opt_form)

        # ---- 保存位置 ----
        save_row = QHBoxLayout()
        save_row.addWidget(QLabel("保存位置:"))
        self.save_edit = QLineEdit()
        self.save_edit.setMinimumHeight(30)
        # 默认：用户「下载」目录，回退到家目录
        default_dir = os.path.join(os.path.expanduser("~"), "Downloads")
        if not os.path.isdir(default_dir):
            default_dir = os.path.expanduser("~")
        self.save_edit.setText(default_dir)
        self.save_edit.setPlaceholderText("选择保存目录")
        save_row.addWidget(self.save_edit, stretch=1)
        self.browse_btn = QPushButton("浏览…")
        self.browse_btn.setMinimumHeight(30)
        self.browse_btn.clicked.connect(self._on_browse)
        save_row.addWidget(self.browse_btn)
        root.addLayout(save_row)

        # 分隔线
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        root.addWidget(sep)

        # ---- 进度条 ----
        self.progress_bar = QProgressBar()
        self.progress_bar.setMinimumHeight(22)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("%p%")
        root.addWidget(self.progress_bar)

        self.stats_label = QLabel("准备就绪")
        self.stats_label.setMinimumHeight(20)
        self.stats_label.setStyleSheet("color: #666;")
        root.addWidget(self.stats_label)

        # status_label 用于显示视频标题/阶段提示（可能很长），设最小高度并
        # 允许换行，但限制最大行数避免它撑高布局挤压其他控件。
        # 颜色用浅灰，让「正在下载 XXX」作为次要信息，不抢进度条焦点。
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.setMinimumHeight(20)
        self.status_label.setMaximumHeight(60)
        self.status_label.setStyleSheet("color: #999;")
        root.addWidget(self.status_label)

        # ---- 按钮 ----
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self.action_btn = QPushButton("开始下载")
        self.action_btn.setMinimumWidth(140)
        self.action_btn.setMinimumHeight(34)
        self.action_btn.clicked.connect(self._on_action)
        btn_row.addWidget(self.action_btn)
        root.addLayout(btn_row)

        root.addStretch()

    # -------------------------------------------------------------- 状态
    def _refresh_controls(self):
        """
        集中式状态刷新：依据当前下载/检测/类型/是否已检测分辨率，
        统一计算每个控件的启用状态与按钮文案。

        规则：
          - 下载中/检测中：输入类控件全部禁用，下载按钮变「取消」(仅下载中)。
          - 视频模式：画质下拉框为空时禁用下载（需先检测）；检测按钮可用。
          - MP3 模式：无需检测，下载按钮恒可用，画质/格式/检测按钮禁用。
        """
        downloading = self.worker is not None
        probing = self.prober is not None
        is_audio = self.rb_audio.isChecked()
        has_qualities = self.quality_combo.count() > 0
        busy = downloading or probing

        # 输入项：busy 时禁用；非 busy 时按类型规则
        self.url_edit.setEnabled(not busy)
        # 检测按钮：检测/下载中禁用；MP3 模式禁用（音频无分辨率）
        self.probe_btn.setEnabled((not busy) and (not is_audio))
        # 类型单选：busy 时禁用，避免切换破坏进行中的任务
        self.rb_video.setEnabled(not busy)
        self.rb_audio.setEnabled(not busy)
        # 保存位置：busy 时禁用
        self.save_edit.setEnabled(not busy)
        self.browse_btn.setEnabled(not busy)

        # 画质下拉框：
        #   busy 时禁用；MP3 模式禁用；否则需有内容才启用
        self.quality_combo.setEnabled((not busy) and (not is_audio) and has_qualities)
        # 格式下拉框：MP3 模式禁用；busy 时禁用
        self.format_combo.setEnabled((not busy) and (not is_audio))

        # 下载按钮文案
        if downloading:
            self.action_btn.setText("取消")
            self.action_btn.setEnabled(True)
        else:
            self.action_btn.setText("开始下载")
            if is_audio:
                # MP3 模式无需先检测
                self.action_btn.setEnabled(not probing)
            else:
                # 视频模式：必须先检测到分辨率，且当前不在检测中
                self.action_btn.setEnabled(has_qualities and (not probing))

    # -------------------------------------------------------------- 交互
    def _on_url_edited(self):
        """链接被改动 → 之前的检测失效，清空画质下拉框。"""
        self.quality_combo.clear()
        self.status_label.setText("")
        self.progress_bar.setValue(0)
        self.stats_label.setText("准备就绪")
        self._refresh_controls()

    def _on_type_changed(self):
        """类型切换：MP3 模式禁用画质/格式/检测，并清掉进度状态。"""
        self.progress_bar.setValue(0)
        self.stats_label.setText("准备就绪" if not self.prober else "正在检测可用分辨率…")
        self._refresh_controls()

    def _on_browse(self):
        """弹出目录选择对话框。"""
        current = self.save_edit.text().strip()
        start_dir = current if current and os.path.isdir(current) else ""
        chosen = QFileDialog.getExistingDirectory(
            self, "选择保存位置", start_dir
        )
        if chosen:
            self.save_edit.setText(chosen)

    # -------------------------------------------------------- 检测分辨率
    def _on_probe(self):
        """检测按钮：发起 ProbeWorker 获取可用分辨率。"""
        url = self.url_edit.text().strip()
        if not url:
            QMessageBox.warning(self, "提示", "请输入 YouTube 链接。")
            return
        if "youtube.com" not in url and "youtu.be" not in url:
            QMessageBox.warning(self, "提示", "请输入有效的 YouTube 链接。")
            return

        # 清空旧检测结果
        self.quality_combo.clear()
        self.stats_label.setText("正在检测可用分辨率…")
        self.status_label.setText("")

        self.prober = ProbeWorker(url=url)
        self.prober.info_ready.connect(self._on_probe_info)
        self.prober.probed.connect(self._on_probed)
        self.prober.failed.connect(self._on_probe_failed)
        self.prober.finished.connect(self._on_probe_thread_finished)
        self.prober.start()
        self._refresh_controls()

    def _on_probe_info(self, title):
        self.status_label.setText(f"已找到: {title}")

    def _on_probed(self, resolutions):
        """检测成功：填充画质下拉框（best 在前），默认选 best。

        resolutions 是 [(分辨率字符串, 体积字节), ...]。
        显示文本带体积如「1080p (273 MB)」；itemData 存纯分辨率字符串
        （如「1080p」），供下载时取用，不受显示文本影响。
        """
        self.quality_combo.clear()
        self.quality_combo.addItem(BEST_LABEL, userData="best")
        for res_name, size_bytes in resolutions:
            display = f"{res_name} ({_fmt_size(size_bytes)})"
            self.quality_combo.addItem(display, userData=res_name)
        self.quality_combo.setCurrentIndex(0)
        self.stats_label.setText(
            f"检测到 {len(resolutions)} 种分辨率，请选择画质。"
        )
        self._refresh_controls()

    def _on_probe_failed(self, msg):
        self.quality_combo.clear()
        self.stats_label.setText("检测失败")
        self.status_label.setText(msg)
        QMessageBox.critical(self, "检测失败", msg)
        self._refresh_controls()

    def _on_probe_thread_finished(self):
        """检测线程结束：清理引用。"""
        self.prober = None
        self._refresh_controls()

    # ------------------------------------------------------------- 下载
    def _on_action(self):
        """下载 / 取消 按钮的统一入口。"""
        if self.worker is not None:
            # 正在下载 → 执行取消
            self._cancel_download()
            return

        # ---- 参数校验 ----
        url = self.url_edit.text().strip()
        if not url:
            QMessageBox.warning(self, "提示", "请输入 YouTube 链接。")
            return
        if "youtube.com" not in url and "youtu.be" not in url:
            QMessageBox.warning(self, "提示", "请输入有效的 YouTube 链接。")
            return

        audio_only = self.rb_audio.isChecked()

        # 视频模式：必须有选中的画质
        if not audio_only and self.quality_combo.count() == 0:
            QMessageBox.warning(self, "提示", "请先点击「检测分辨率」选择画质。")
            return

        save_dir = self.save_edit.text().strip()
        if not save_dir:
            QMessageBox.warning(self, "提示", "请选择保存位置。")
            return
        if not os.path.isdir(save_dir):
            ret = QMessageBox.question(
                self, "提示",
                f"目录不存在，是否创建？\n{save_dir}"
            )
            if ret != QMessageBox.StandardButton.Yes:
                return
            try:
                os.makedirs(save_dir, exist_ok=True)
            except OSError as e:
                QMessageBox.critical(self, "错误", f"无法创建目录：\n{e}")
                return

        # 画质取值：从 itemData 取纯分辨率（如 "best"/"1080p"），
        # 不受下拉框显示文本（带体积）影响。
        quality = self.quality_combo.currentData() or "best"
        video_format = self.format_combo.currentText()

        # ---- 启动下载 ----
        self.progress_bar.setValue(0)
        self.stats_label.setText("正在获取视频信息…")
        self.status_label.setText("")
        self._current_title = ""  # 清空上次的标题

        self.worker = DownloadWorker(
            url=url,
            output_dir=save_dir,
            quality=quality,
            video_format=video_format,
            audio_only=audio_only,
        )
        self.worker.info_ready.connect(self._on_info)
        self.worker.stage.connect(self._on_stage)
        self.worker.progress.connect(self._on_progress)
        self.worker.finished_ok.connect(self._on_finished)
        self.worker.failed.connect(self._on_failed)
        # 线程结束（无论成功失败）后统一清理
        self.worker.finished.connect(self._on_thread_finished)
        self.worker.start()
        self._refresh_controls()

    def _cancel_download(self):
        if self.worker is not None:
            self.stats_label.setText("正在取消…")
            self.action_btn.setEnabled(False)
            self.worker.cancel()

    # ------------------------------------------------------ 信号回调
    def _on_info(self, title):
        # 只记录标题；具体阶段提示由 _on_stage 显示。
        # info_ready 也用于「重试」「降级提示」等场景，直接显示标题。
        self._current_title = title
        # 如果 stage 还没发过（纯下载单流），显示默认提示
        self.status_label.setText(title)

    def _on_stage(self, stage_text):
        """下载阶段提示：视频流/音频流/合并等。标题用浅色作为次要信息。"""
        if self._current_title:
            # 阶段在前（强提示），标题在后（弱信息）
            self.status_label.setText(f"{stage_text} — {self._current_title}")
        else:
            self.status_label.setText(stage_text)

    def _on_progress(self, percent, size_text, speed_text, eta_text):
        self.progress_bar.setValue(percent)
        self.stats_label.setText(
            f"{size_text}    {speed_text}    ETA {eta_text}"
        )

    def _on_finished(self, filepath):
        self.progress_bar.setValue(100)
        self.stats_label.setText("下载完成！")
        self.status_label.setText(f"已保存到: {filepath}")
        QMessageBox.information(
            self, "完成", f"下载完成！\n\n已保存到:\n{filepath}"
        )

    def _on_failed(self, msg):
        self.stats_label.setText("下载失败")
        self.status_label.setText(msg)
        QMessageBox.critical(self, "下载失败", msg)

    def _on_thread_finished(self):
        """下载线程真正结束后恢复 UI 状态。"""
        self.worker = None
        self._refresh_controls()


def main():
    # 入口加固：必须在创建 QApplication 之前执行
    _bootstrap_stdio()      # 兜底 stdout/stderr，防止 windowed 模式闪退
    _install_excepthook()   # 全局异常写日志

    # 高 DPI 支持
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    # 捕获 Qt 内部未处理的消息（另一种崩溃来源）
    def _qt_message_handler(mode, ctx, message):
        try:
            log_path = os.path.join(os.path.expanduser("~"), "YouTubeDownloader_crash.log")
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"[Qt {mode}] {message}\n")
        except Exception:
            pass
    from PyQt6.QtCore import qInstallMessageHandler
    qInstallMessageHandler(_qt_message_handler)

    win = DownloaderWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
