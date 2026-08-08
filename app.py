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
    QLabel, QLineEdit, QPlainTextEdit, QComboBox, QPushButton, QProgressBar,
    QRadioButton, QButtonGroup, QCheckBox, QFileDialog, QMessageBox, QFrame,
)

from downloader import (
    DownloadWorker, ProbeWorker, normalize_youtube_url, subtitle_display_name,
)


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
            if unit == "KB":
                return f"{int(n)} KB"
            return f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} PB"


def _parse_urls(text):
    """从多行文本解析 YouTube URL 列表：按行分割、规范化、去重、过滤无效。

    保留输入顺序（去重时取首次出现）。返回 (有效URL列表, 无效行列表)。"""
    seen = set()
    valid = []
    invalid = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        norm = normalize_youtube_url(line)
        if ("youtube.com" in norm or "youtu.be" in norm) and norm not in seen:
            seen.add(norm)
            valid.append(norm)
        else:
            invalid.append(line)
    return valid, invalid


def _resource_path(relative_name):
    """获取资源文件的绝对路径，兼容「源码运行」和「PyInstaller 打包后运行」。

    源码运行：资源在脚本同目录（如 app.ico 与 app.py 同级）。
    打包后：PyInstaller 解压到临时目录 sys._MEIPASS，资源在那里。
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

    is_windowed = sys.stdout is None or sys.stderr is None

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

    if sys.stdout is None:
        sys.stdout = _NullStream()
    if sys.stderr is None:
        sys.stderr = _NullStream()


def _install_excepthook():
    """全局异常钩子：未捕获异常写入 crash.log，避免「闪退且无任何线索」。"""
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
        self.setMinimumHeight(520)
        self.resize(660, 580)

        icon_path = _resource_path("app.ico")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 20)
        root.setSpacing(14)

        title_font = QFont()
        title_font.setPointSize(14)
        title_font.setBold(True)
        title_label = QLabel("YouTube 视频下载器")
        title_label.setFont(title_font)
        root.addWidget(title_label)

        # ---- URL（多行，支持批量）+ 检测按钮 ----
        url_label = QLabel("YouTube 链接（每行一个，支持批量）")
        root.addWidget(url_label)
        url_row = QHBoxLayout()
        self.url_edit = QPlainTextEdit()
        self.url_edit.setMinimumHeight(58)
        self.url_edit.setPlaceholderText(
            "https://www.youtube.com/watch?v=...\nhttps://youtu.be/..."
        )
        # textChanged：内容变动即触发；改了链接则检测结果失效。
        # 用 blockSignals 避免程序化清空时误触发。
        self.url_edit.textChanged.connect(self._on_url_edited)
        url_row.addWidget(self.url_edit, stretch=1)
        # 检测/下载按钮竖排放在右侧
        url_btn_col = QVBoxLayout()
        url_btn_col.setSpacing(8)
        self.probe_btn = QPushButton("检测分辨率")
        self.probe_btn.setMinimumHeight(30)
        self.probe_btn.clicked.connect(self._on_probe)
        url_btn_col.addWidget(self.probe_btn)
        url_btn_col.addStretch()
        url_row.addLayout(url_btn_col)
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

        # ---- 画质 + 格式 + 字幕 ----
        opt_form = QFormLayout()
        opt_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        opt_form.setHorizontalSpacing(12)
        opt_form.setVerticalSpacing(10)
        self.quality_combo = QComboBox()
        self.quality_combo.setMinimumHeight(30)
        self.quality_combo.setEnabled(False)  # 初始：未检测，禁用
        opt_form.addRow("画质:", self.quality_combo)

        self.format_combo = QComboBox()
        self.format_combo.setMinimumHeight(30)
        self.format_combo.addItems(FORMAT_OPTIONS)
        self.format_combo.setCurrentText("mp4")
        opt_form.addRow("格式:", self.format_combo)

        # 字幕：复选框 + 语言下拉（视频模式、检测后启用）
        sub_row = QHBoxLayout()
        self.sub_check = QCheckBox("嵌入字幕")
        self.sub_check.toggled.connect(self._refresh_controls)
        self.sub_combo = QComboBox()
        self.sub_combo.setMinimumHeight(30)
        self.sub_combo.setEnabled(False)
        self.sub_combo.addItem("（未检测到字幕）", userData=None)
        sub_row.addWidget(self.sub_check)
        sub_row.addWidget(self.sub_combo, stretch=1)
        opt_form.addRow("字幕:", sub_row)
        root.addLayout(opt_form)

        # ---- 保存位置 ----
        save_row = QHBoxLayout()
        save_row.addWidget(QLabel("保存位置:"))
        self.save_edit = QLineEdit()
        self.save_edit.setMinimumHeight(30)
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

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.setMinimumHeight(20)
        self.status_label.setMaximumHeight(60)
        self.status_label.setStyleSheet("color: #999;")
        root.addWidget(self.status_label)

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
        """集中式状态刷新。规则：
          - 下载中/检测中：输入禁用，下载按钮变「取消」。
          - 视频模式：画质为空时禁用下载（需先检测）。
          - MP3 模式：无需检测即可下载；字幕/画质/格式禁用。"""
        downloading = self.worker is not None
        probing = self.prober is not None
        is_audio = self.rb_audio.isChecked()
        has_qualities = self.quality_combo.count() > 0
        busy = downloading or probing

        self.url_edit.setEnabled(not busy)
        self.probe_btn.setEnabled((not busy) and (not is_audio))
        self.rb_video.setEnabled(not busy)
        self.rb_audio.setEnabled(not busy)
        self.save_edit.setEnabled(not busy)
        self.browse_btn.setEnabled(not busy)

        self.quality_combo.setEnabled((not busy) and (not is_audio) and has_qualities)
        self.format_combo.setEnabled((not busy) and (not is_audio))

        # 字幕：仅视频模式、非 busy、有可选语言时启用
        has_subs = self.sub_combo.count() > 0 and self.sub_combo.itemData(0) is not None
        sub_enabled = (not busy) and (not is_audio) and has_subs
        self.sub_check.setEnabled(sub_enabled)
        self.sub_combo.setEnabled(sub_enabled and self.sub_check.isChecked())
        # MP3 模式或无字幕时取消勾选
        if (is_audio or not has_subs) and self.sub_check.isChecked():
            self.sub_check.blockSignals(True)
            self.sub_check.setChecked(False)
            self.sub_check.blockSignals(False)

        if downloading:
            self.action_btn.setText("取消")
            self.action_btn.setEnabled(True)
        else:
            self.action_btn.setText("开始下载")
            if is_audio:
                self.action_btn.setEnabled(not probing)
            else:
                self.action_btn.setEnabled(has_qualities and (not probing))

    # -------------------------------------------------------------- 交互
    def _on_url_edited(self):
        """链接被改动 → 之前的检测失效，清空画质与字幕下拉。"""
        self.quality_combo.clear()
        # 清空字幕（恢复初始提示项）
        self.sub_combo.blockSignals(True)
        self.sub_combo.clear()
        self.sub_combo.addItem("（未检测到字幕）", userData=None)
        self.sub_combo.blockSignals(False)
        self.sub_check.blockSignals(True)
        self.sub_check.setChecked(False)
        self.sub_check.blockSignals(False)
        self.status_label.setText("")
        self.progress_bar.setValue(0)
        self.stats_label.setText("准备就绪")
        self._refresh_controls()

    def _on_type_changed(self):
        """类型切换：清掉进度状态，刷新控件。"""
        self.progress_bar.setValue(0)
        self.stats_label.setText("准备就绪" if not self.prober else "正在检测可用分辨率…")
        self._refresh_controls()

    def _on_browse(self):
        current = self.save_edit.text().strip()
        start_dir = current if current and os.path.isdir(current) else ""
        chosen = QFileDialog.getExistingDirectory(self, "选择保存位置", start_dir)
        if chosen:
            self.save_edit.setText(chosen)

    # -------------------------------------------------------- 检测分辨率
    def _on_probe(self):
        """检测按钮：用第一个有效 URL 发起 ProbeWorker（批量时画质统一，检测一次即可）。"""
        valid, invalid = _parse_urls(self.url_edit.toPlainText())
        if not valid:
            QMessageBox.warning(self, "提示", "请输入有效的 YouTube 链接（每行一个）。")
            return

        self.quality_combo.clear()
        self.sub_combo.clear()
        self.sub_combo.addItem("（未检测到字幕）", userData=None)
        self.sub_check.setChecked(False)
        self.stats_label.setText(f"正在检测可用分辨率…（第 1 个链接）")
        self.status_label.setText("")

        # 检测用第一个 URL
        self.prober = ProbeWorker(url=valid[0])
        self.prober.info_ready.connect(self._on_probe_info)
        self.prober.probed.connect(self._on_probed)
        self.prober.subs_ready.connect(self._on_subs_ready)
        self.prober.failed.connect(self._on_probe_failed)
        self.prober.finished.connect(self._on_probe_thread_finished)
        self.prober.start()
        self._refresh_controls()

    def _on_probe_info(self, title):
        self.status_label.setText(f"已找到: {title}")

    def _on_probed(self, resolutions):
        """检测成功：填充画质下拉框（best 在前），默认选 best。"""
        self.quality_combo.clear()
        self.quality_combo.addItem(BEST_LABEL, userData="best")
        for res_name, size_bytes in resolutions:
            display = f"{res_name} ({_fmt_size(size_bytes)})"
            self.quality_combo.addItem(display, userData=res_name)
        self.quality_combo.setCurrentIndex(0)
        n = len(resolutions)
        self.stats_label.setText(f"检测到 {n} 种分辨率，请选择画质。")
        self._refresh_controls()

    def _on_subs_ready(self, langs):
        """字幕检测完成：填充字幕语言下拉框。无字幕时保持禁用提示。"""
        self.sub_combo.blockSignals(True)
        self.sub_combo.clear()
        if langs:
            for lang in langs:
                self.sub_combo.addItem(subtitle_display_name(lang), userData=lang)
            # 默认选第一个（通常是简体中文/英语）
            self.sub_combo.setCurrentIndex(0)
        else:
            self.sub_combo.addItem("（未检测到字幕）", userData=None)
        self.sub_combo.blockSignals(False)
        self._refresh_controls()

    def _on_probe_failed(self, msg):
        self.quality_combo.clear()
        self.stats_label.setText("检测失败")
        self.status_label.setText(msg)
        QMessageBox.critical(self, "检测失败", msg)
        self._refresh_controls()

    def _on_probe_thread_finished(self):
        self.prober = None
        self._refresh_controls()

    # ------------------------------------------------------------- 下载
    def _on_action(self):
        """下载 / 取消 按钮的统一入口。"""
        if self.worker is not None:
            self._cancel_download()
            return

        # ---- 解析多行 URL ----
        valid, invalid = _parse_urls(self.url_edit.toPlainText())
        if not valid:
            QMessageBox.warning(self, "提示", "请输入有效的 YouTube 链接（每行一个）。")
            return

        audio_only = self.rb_audio.isChecked()

        # 视频模式：必须有选中的画质（已检测）
        if not audio_only and self.quality_combo.count() == 0:
            QMessageBox.warning(self, "提示", "请先点击「检测分辨率」选择画质。")
            return

        save_dir = self.save_edit.text().strip()
        if not save_dir:
            QMessageBox.warning(self, "提示", "请选择保存位置。")
            return
        if not os.path.isdir(save_dir):
            ret = QMessageBox.question(self, "提示", f"目录不存在，是否创建？\n{save_dir}")
            if ret != QMessageBox.StandardButton.Yes:
                return
            try:
                os.makedirs(save_dir, exist_ok=True)
            except OSError as e:
                QMessageBox.critical(self, "错误", f"无法创建目录：\n{e}")
                return

        quality = self.quality_combo.currentData() or "best"
        video_format = self.format_combo.currentText()

        # 字幕：视频模式且勾选且有有效语言时启用
        subtitle_lang = None
        if (not audio_only) and self.sub_check.isChecked() and self.sub_combo.isEnabled():
            subtitle_lang = self.sub_combo.currentData()

        # 批量提示：多于 1 个时确认
        if len(valid) > 1:
            extra = f"\n\n无效行 {len(invalid)} 个已忽略。" if invalid else ""
            ret = QMessageBox.question(
                self, "确认批量下载",
                f"共 {len(valid)} 个有效链接，将依次下载（统一画质：{quality}）。\n{extra}"
            )
            if ret != QMessageBox.StandardButton.Yes:
                return

        # ---- 启动下载 ----
        self.progress_bar.setValue(0)
        self.stats_label.setText("正在获取视频信息…" if len(valid) == 1
                                 else f"准备下载 {len(valid)} 个视频…")
        self.status_label.setText("")
        self._current_title = ""

        self.worker = DownloadWorker(
            urls=valid,
            output_dir=save_dir,
            quality=quality,
            video_format=video_format,
            audio_only=audio_only,
            subtitle_lang=subtitle_lang,
        )
        self.worker.info_ready.connect(self._on_info)
        self.worker.stage.connect(self._on_stage)
        self.worker.progress.connect(self._on_progress)
        self.worker.finished_ok.connect(self._on_finished)
        self.worker.failed.connect(self._on_failed)
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
        self._current_title = title
        self.status_label.setText(title)

    def _on_stage(self, stage_text):
        if self._current_title:
            self.status_label.setText(f"{stage_text} — {self._current_title}")
        else:
            self.status_label.setText(stage_text)

    def _on_progress(self, percent, size_text, speed_text, eta_text):
        self.progress_bar.setValue(percent)
        self.stats_label.setText(f"{size_text}    {speed_text}    ETA {eta_text}")

    def _on_finished(self, msg):
        self.progress_bar.setValue(100)
        self.stats_label.setText("下载完成！")
        self.status_label.setText(msg)
        QMessageBox.information(self, "完成", msg)

    def _on_failed(self, msg):
        self.stats_label.setText("下载失败")
        self.status_label.setText(msg)
        QMessageBox.critical(self, "下载失败", msg)

    def _on_thread_finished(self):
        self.worker = None
        self._refresh_controls()


def main():
    _bootstrap_stdio()      # 兜底 stdout/stderr，防止 windowed 模式闪退
    _install_excepthook()   # 全局异常写日志

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

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
