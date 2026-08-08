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

import i18n
from i18n import tr
from downloader import (
    DownloadWorker, ProbeWorker, normalize_youtube_url, subtitle_display_name,
)


FORMAT_OPTIONS = ["mp4", "webm", "mkv"]


def _fmt_size(num_bytes):
    """把字节数转成人类可读的 KB/MB/GB，用于画质体积显示。"""
    try:
        n = float(num_bytes)
    except (TypeError, ValueError):
        return "?"
    if n <= 0:
        return "?"
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
    """获取资源文件的绝对路径，兼容「源码运行」和「PyInstaller 打包后运行」。"""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative_name)


def _bootstrap_stdio():
    """PyInstaller --windowed 模式下兜底 stdout/stderr + OS 层文件句柄，
    防止子进程（node/ffmpeg）因无效管道 hang 住。"""
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
    """全局异常钩子：未捕获异常写入 crash.log。"""
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
        self.worker = None
        self.prober = None
        self._current_title = ""
        self._build_ui()
        # 加载保存的语言，并注册语言切换回调
        i18n.load_lang()
        self._apply_lang_combo()
        i18n.on_language_change(self._retranslate)
        self._retranslate()
        self._refresh_controls()

    # ---------------------------------------------------------------- UI
    def _build_ui(self):
        self.setMinimumWidth(640)
        self.setMinimumHeight(540)
        self.resize(680, 600)

        icon_path = _resource_path("app.ico")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 20)
        root.setSpacing(14)

        # ---- 标题行 + 语言选择 ----
        top_row = QHBoxLayout()
        title_font = QFont()
        title_font.setPointSize(14)
        title_font.setBold(True)
        self.title_label = QLabel()
        self.title_label.setFont(title_font)
        top_row.addWidget(self.title_label)
        top_row.addStretch()
        lang_label = QLabel("语言 / Language:")
        top_row.addWidget(lang_label)
        self.lang_combo = QComboBox()
        for code, name in i18n.available_langs():
            self.lang_combo.addItem(name, userData=code)
        self.lang_combo.currentIndexChanged.connect(self._on_lang_changed)
        top_row.addWidget(self.lang_combo)
        root.addLayout(top_row)

        # ---- URL（多行，支持批量）+ 检测按钮 ----
        self.url_label = QLabel()
        root.addWidget(self.url_label)
        url_row = QHBoxLayout()
        self.url_edit = QPlainTextEdit()
        self.url_edit.setMinimumHeight(58)
        self.url_edit.textChanged.connect(self._on_url_edited)
        url_row.addWidget(self.url_edit, stretch=1)
        url_btn_col = QVBoxLayout()
        url_btn_col.setSpacing(8)
        self.probe_btn = QPushButton()
        self.probe_btn.setMinimumHeight(30)
        self.probe_btn.clicked.connect(self._on_probe)
        url_btn_col.addWidget(self.probe_btn)
        url_btn_col.addStretch()
        url_row.addLayout(url_btn_col)
        root.addLayout(url_row)

        # ---- 类型选择（视频 / MP3）----
        type_row = QHBoxLayout()
        self.type_label = QLabel()
        type_row.addWidget(self.type_label)
        self.rb_video = QRadioButton()
        self.rb_audio = QRadioButton()
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
        self.quality_combo.setEnabled(False)
        self._quality_label = QLabel()
        opt_form.addRow(self._quality_label, self.quality_combo)

        self.format_combo = QComboBox()
        self.format_combo.setMinimumHeight(30)
        self.format_combo.addItems(FORMAT_OPTIONS)
        self.format_combo.setCurrentText("mp4")
        self._format_label = QLabel()
        opt_form.addRow(self._format_label, self.format_combo)

        # 字幕
        sub_row = QHBoxLayout()
        self.sub_check = QCheckBox()
        self.sub_check.toggled.connect(self._refresh_controls)
        self.sub_combo = QComboBox()
        self.sub_combo.setMinimumHeight(30)
        self.sub_combo.setEnabled(False)
        self._reset_sub_combo()
        sub_row.addWidget(self.sub_check)
        sub_row.addWidget(self.sub_combo, stretch=1)
        self._sub_label = QLabel()
        opt_form.addRow(self._sub_label, sub_row)
        root.addLayout(opt_form)

        # ---- 保存位置 ----
        save_row = QHBoxLayout()
        self.save_label = QLabel()
        save_row.addWidget(self.save_label)
        self.save_edit = QLineEdit()
        self.save_edit.setMinimumHeight(30)
        default_dir = os.path.join(os.path.expanduser("~"), "Downloads")
        if not os.path.isdir(default_dir):
            default_dir = os.path.expanduser("~")
        self.save_edit.setText(default_dir)
        save_row.addWidget(self.save_edit, stretch=1)
        self.browse_btn = QPushButton()
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

        self.stats_label = QLabel()
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
        self.action_btn = QPushButton()
        self.action_btn.setMinimumWidth(140)
        self.action_btn.setMinimumHeight(34)
        self.action_btn.clicked.connect(self._on_action)
        btn_row.addWidget(self.action_btn)
        root.addLayout(btn_row)

        root.addStretch()

    def _apply_lang_combo(self):
        """让语言下拉框显示当前语言（不触发回调）。"""
        self.lang_combo.blockSignals(True)
        cur = i18n.current_lang()
        for i in range(self.lang_combo.count()):
            if self.lang_combo.itemData(i) == cur:
                self.lang_combo.setCurrentIndex(i)
                break
        self.lang_combo.blockSignals(False)

    def _reset_sub_combo(self):
        """重置字幕下拉为「未检测」占位项。"""
        self.sub_combo.blockSignals(True)
        self.sub_combo.clear()
        self.sub_combo.addItem(tr("sub_none"), userData=None)
        self.sub_combo.blockSignals(False)

    # ----------------------------------------------------------- 多语言
    def _on_lang_changed(self):
        code = self.lang_combo.currentData()
        if code:
            i18n.set_language(code)  # 会触发 _retranslate

    def _retranslate(self):
        """语言切换后，重新设置所有控件文本。"""
        self.setWindowTitle(tr("app_title"))
        self.title_label.setText(tr("app_title"))
        self.url_label.setText(tr("url_label"))
        self.url_edit.setPlaceholderText(tr("url_placeholder"))
        self.probe_btn.setText(tr("probe_btn"))
        self.type_label.setText(tr("type_label"))
        self.rb_video.setText(tr("type_video"))
        self.rb_audio.setText(tr("type_audio"))
        self._quality_label.setText(tr("quality_label"))
        self._format_label.setText(tr("format_label"))
        self._sub_label.setText(tr("subtitle_label"))
        self.sub_check.setText(tr("sub_embed"))
        # 字幕占位项的文本也要更新
        if self.sub_combo.itemData(0) is None:
            self.sub_combo.setItemText(0, tr("sub_none"))
        self.save_label.setText(tr("save_label"))
        self.save_edit.setPlaceholderText(tr("save_placeholder"))
        self.browse_btn.setText(tr("browse_btn"))
        # best 选项的显示文本（第 0 项）
        if self.quality_combo.count() > 0 and self.quality_combo.itemData(0) == "best":
            self.quality_combo.setItemText(0, tr("best_label"))
        # 按钮文案（下载中不变，空闲时刷新）
        if self.worker is None and self.prober is None:
            self.action_btn.setText(tr("download_btn"))
        self._refresh_controls()

    # -------------------------------------------------------------- 状态
    def _refresh_controls(self):
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

        has_subs = self.sub_combo.count() > 0 and self.sub_combo.itemData(0) is not None
        sub_enabled = (not busy) and (not is_audio) and has_subs
        self.sub_check.setEnabled(sub_enabled)
        self.sub_combo.setEnabled(sub_enabled and self.sub_check.isChecked())
        if (is_audio or not has_subs) and self.sub_check.isChecked():
            self.sub_check.blockSignals(True)
            self.sub_check.setChecked(False)
            self.sub_check.blockSignals(False)

        if downloading:
            self.action_btn.setText(tr("cancel"))
            self.action_btn.setEnabled(True)
        else:
            self.action_btn.setText(tr("download_btn"))
            if is_audio:
                self.action_btn.setEnabled(not probing)
            else:
                self.action_btn.setEnabled(has_qualities and (not probing))

    # -------------------------------------------------------------- 交互
    def _on_url_edited(self):
        self.quality_combo.clear()
        self._reset_sub_combo()
        self.sub_check.blockSignals(True)
        self.sub_check.setChecked(False)
        self.sub_check.blockSignals(False)
        self.status_label.setText("")
        self.progress_bar.setValue(0)
        self.stats_label.setText(tr("ready"))
        self._refresh_controls()

    def _on_type_changed(self):
        self.progress_bar.setValue(0)
        self.stats_label.setText(tr("ready") if not self.prober else tr("detecting"))
        self._refresh_controls()

    def _on_browse(self):
        current = self.save_edit.text().strip()
        start_dir = current if current and os.path.isdir(current) else ""
        chosen = QFileDialog.getExistingDirectory(self, tr("choose_dir"), start_dir)
        if chosen:
            self.save_edit.setText(chosen)

    # -------------------------------------------------------- 检测分辨率
    def _on_probe(self):
        valid, invalid = _parse_urls(self.url_edit.toPlainText())
        if not valid:
            QMessageBox.warning(self, tr("tip"), tr("enter_url"))
            return

        self.quality_combo.clear()
        self._reset_sub_combo()
        self.sub_check.setChecked(False)
        self.stats_label.setText(tr("detected_first"))
        self.status_label.setText("")

        self.prober = ProbeWorker(url=valid[0])
        self.prober.info_ready.connect(self._on_probe_info)
        self.prober.probed.connect(self._on_probed)
        self.prober.subs_ready.connect(self._on_subs_ready)
        self.prober.failed.connect(self._on_probe_failed)
        self.prober.finished.connect(self._on_probe_thread_finished)
        self.prober.start()
        self._refresh_controls()

    def _on_probe_info(self, title):
        self.status_label.setText(tr("found", title=title))

    def _on_probed(self, resolutions):
        self.quality_combo.clear()
        self.quality_combo.addItem(tr("best_label"), userData="best")
        for res_name, size_bytes in resolutions:
            display = f"{res_name} ({_fmt_size(size_bytes)})"
            self.quality_combo.addItem(display, userData=res_name)
        self.quality_combo.setCurrentIndex(0)
        self.stats_label.setText(tr("detected_n", n=len(resolutions)))
        self._refresh_controls()

    def _on_subs_ready(self, langs):
        self.sub_combo.blockSignals(True)
        self.sub_combo.clear()
        if langs:
            for lang in langs:
                self.sub_combo.addItem(subtitle_display_name(lang), userData=lang)
            self.sub_combo.setCurrentIndex(0)
        else:
            self.sub_combo.addItem(tr("sub_none"), userData=None)
        self.sub_combo.blockSignals(False)
        self._refresh_controls()

    def _on_probe_failed(self, msg):
        self.quality_combo.clear()
        self.stats_label.setText(tr("detect_failed"))
        self.status_label.setText(msg)
        QMessageBox.critical(self, tr("detect_failed"), msg)
        self._refresh_controls()

    def _on_probe_thread_finished(self):
        self.prober = None
        self._refresh_controls()

    # ------------------------------------------------------------- 下载
    def _on_action(self):
        if self.worker is not None:
            self._cancel_download()
            return

        valid, invalid = _parse_urls(self.url_edit.toPlainText())
        if not valid:
            QMessageBox.warning(self, tr("tip"), tr("enter_url"))
            return

        audio_only = self.rb_audio.isChecked()
        if not audio_only and self.quality_combo.count() == 0:
            QMessageBox.warning(self, tr("tip"), tr("detect_first"))
            return

        save_dir = self.save_edit.text().strip()
        if not save_dir:
            QMessageBox.warning(self, tr("tip"), tr("choose_save"))
            return
        if not os.path.isdir(save_dir):
            ret = QMessageBox.question(self, tr("tip"), tr("mkdir_ask", dir=save_dir))
            if ret != QMessageBox.StandardButton.Yes:
                return
            try:
                os.makedirs(save_dir, exist_ok=True)
            except OSError as e:
                QMessageBox.critical(self, tr("error"), tr("mkdir_fail", err=str(e)))
                return

        quality = self.quality_combo.currentData() or "best"
        video_format = self.format_combo.currentText()

        subtitle_lang = None
        if (not audio_only) and self.sub_check.isChecked() and self.sub_combo.isEnabled():
            subtitle_lang = self.sub_combo.currentData()

        if len(valid) > 1:
            extra = tr("ignored", n=len(invalid)) if invalid else ""
            ret = QMessageBox.question(
                self, tr("confirm_batch_title"),
                tr("confirm_batch", n=len(valid), q=quality, extra=extra)
            )
            if ret != QMessageBox.StandardButton.Yes:
                return

        self.progress_bar.setValue(0)
        self.stats_label.setText(tr("getting_info") if len(valid) == 1
                                 else tr("prepare_batch", n=len(valid)))
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
            self.stats_label.setText(tr("cancelling"))
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
        self.stats_label.setText(tr("done"))
        self.status_label.setText(msg)
        QMessageBox.information(self, tr("complete"), msg)

    def _on_failed(self, msg):
        self.stats_label.setText(tr("failed"))
        self.status_label.setText(msg)
        QMessageBox.critical(self, tr("failed"), msg)

    def _on_thread_finished(self):
        self.worker = None
        self._refresh_controls()


def main():
    _bootstrap_stdio()
    _install_excepthook()

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
