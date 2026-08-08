# -*- coding: utf-8 -*-
"""多语言支持（轻量纯 Python 实现，无外部依赖）。

- _TEXTS：翻译表，key 为英文标识，值为 {lang: 文本}。
- tr(key)：返回当前语言对应文本。
- set_language(lang)：切换语言，通知所有已注册的回调（窗口重译）。
- load/save：语言偏好持久化到配置文件。

新增语言：往 _LANGS 加一项，给每个 key 补一列即可。
"""

import os
import json


# 支持的语言（代码 → 显示名）。显示名用各自语言书写，方便用户识别。
_LANGS = {
    "zh": "中文",
    "en": "English",
}

# 默认语言（首次启动 / 配置缺失时）
_DEFAULT_LANG = "zh"

_current_lang = _DEFAULT_LANG
_listeners = []  # 语言切换回调列表（窗口注册 _retranslate）


# 翻译表。key 用稳定的英文标识，值是 {语言代码: 该语言文本}。
# 带占位符的用 {x} 标记，调用时 .format(...) 填充。
_TEXTS = {
    # ---- 窗口标题 / 标题 ----
    "app_title": {"zh": "YouTube 视频下载器", "en": "YouTube Video Downloader"},

    # ---- URL 区 ----
    "url_label": {"zh": "YouTube 链接（每行一个，支持批量）", "en": "YouTube URLs (one per line, batch supported)"},
    "url_placeholder": {"zh": "https://www.youtube.com/watch?v=...\nhttps://youtu.be/...", "en": "https://www.youtube.com/watch?v=...\nhttps://youtu.be/..."},
    "probe_btn": {"zh": "检测分辨率", "en": "Detect Quality"},

    # ---- 类型 / 选项 ----
    "type_label": {"zh": "类型:", "en": "Type:"},
    "type_video": {"zh": "视频", "en": "Video"},
    "type_audio": {"zh": "仅音频 (MP3)", "en": "Audio only (MP3)"},
    "quality_label": {"zh": "画质:", "en": "Quality:"},
    "format_label": {"zh": "格式:", "en": "Format:"},
    "best_label": {"zh": "best (最高画质)", "en": "best (highest)"},
    "subtitle_label": {"zh": "字幕:", "en": "Subtitle:"},
    "sub_embed": {"zh": "嵌入字幕", "en": "Embed subtitle"},
    "sub_none": {"zh": "（未检测到字幕）", "en": "(no subtitles found)"},

    # ---- 保存位置 ----
    "save_label": {"zh": "保存位置:", "en": "Save to:"},
    "save_placeholder": {"zh": "选择保存目录", "en": "Choose save folder"},
    "browse_btn": {"zh": "浏览…", "en": "Browse…"},
    "choose_dir": {"zh": "选择保存位置", "en": "Choose save location"},

    # ---- 状态文本 ----
    "ready": {"zh": "准备就绪", "en": "Ready"},
    "cancel": {"zh": "取消", "en": "Cancel"},
    "download_btn": {"zh": "开始下载", "en": "Start Download"},
    "getting_info": {"zh": "正在获取视频信息…", "en": "Getting video info…"},
    "cancelling": {"zh": "正在取消…", "en": "Cancelling…"},
    "done": {"zh": "下载完成！", "en": "Download complete!"},
    "failed": {"zh": "下载失败", "en": "Download failed"},
    "detecting": {"zh": "正在检测可用分辨率…", "en": "Detecting available qualities…"},
    "detect_failed": {"zh": "检测失败", "en": "Detection failed"},
    "found": {"zh": "已找到: {title}", "en": "Found: {title}"},
    "detected_n": {"zh": "检测到 {n} 种分辨率，请选择画质。", "en": "{n} qualities detected, pick one."},
    "detected_first": {"zh": "正在检测可用分辨率…（第 1 个链接）", "en": "Detecting… (1st link)"},
    "saved_to": {"zh": "已保存到: {path}", "en": "Saved to: {path}"},
    "prepare_batch": {"zh": "准备下载 {n} 个视频…", "en": "Preparing {n} videos…"},

    # ---- 提示弹窗 ----
    "tip": {"zh": "提示", "en": "Notice"},
    "error": {"zh": "错误", "en": "Error"},
    "confirm_batch_title": {"zh": "确认批量下载", "en": "Confirm batch download"},
    "enter_url": {"zh": "请输入有效的 YouTube 链接（每行一个）。", "en": "Please enter valid YouTube URLs (one per line)."},
    "detect_first": {"zh": "请先点击「检测分辨率」选择画质。", "en": "Please click 'Detect Quality' first to pick a quality."},
    "choose_save": {"zh": "请选择保存位置。", "en": "Please choose a save location."},
    "mkdir_ask": {"zh": "目录不存在，是否创建？\n{dir}", "en": "Folder does not exist. Create it?\n{dir}"},
    "mkdir_fail": {"zh": "无法创建目录：\n{err}", "en": "Cannot create folder:\n{err}"},
    "confirm_batch": {"zh": "共 {n} 个有效链接，将依次下载（统一画质：{q}）。{extra}", "en": "{n} valid links will be downloaded sequentially (quality: {q}). {extra}"},
    "ignored": {"zh": "\n\n无效行 {n} 个已忽略。", "en": "\n{n} invalid line(s) ignored."},
    "complete": {"zh": "完成", "en": "Complete"},

    # ---- 下载阶段提示（downloader.py worker emit）----
    "stage_video": {"zh": "正在下载视频流", "en": "Downloading video stream"},
    "stage_audio": {"zh": "正在下载音频流", "en": "Downloading audio stream"},
    "stage_merge": {"zh": "正在合并视频和音频", "en": "Merging video and audio"},
    "stage_mp3": {"zh": "正在转换为 MP3", "en": "Converting to MP3"},
    "stage_meta": {"zh": "正在写入元数据", "en": "Writing metadata"},
    "retry": {"zh": "{prefix}网络异常，正在重试（第 {n}/{max} 次）…", "en": "{prefix}Network error, retrying ({n}/{max})…"},
    "need_ffmpeg_mp3": {"zh": "下载 MP3 需要 ffmpeg，但系统未检测到 ffmpeg。\n\n请安装 ffmpeg 并确保它在系统 PATH 中。\n下载地址：https://ffmpeg.org/download.html", "en": "MP3 requires ffmpeg, which was not found.\n\nInstall ffmpeg and add it to PATH.\nhttps://ffmpeg.org/download.html"},
    "no_ytdlp": {"zh": "未找到 yt-dlp，请先运行: pip install yt-dlp", "en": "yt-dlp not found. Run: pip install yt-dlp"},
    "no_urls": {"zh": "没有可下载的链接。", "en": "No URLs to download."},
    "cancelled": {"zh": "已取消下载", "en": "Cancelled"},
    "cancelled_partial": {"zh": "已取消下载（已完成 {done}/{total}）", "en": "Cancelled ({done}/{total} done)"},
    "all_failed": {"zh": "全部下载失败（共 {n} 个）：\n{detail}", "en": "All failed ({n}):\n{detail}"},
    "partial": {"zh": "部分完成 {ok}/{total}，失败 {fail} 个：\n{detail}", "en": "Partial {ok}/{total}, {fail} failed:\n{detail}"},
    "no_res": {"zh": "未检测到可用分辨率。", "en": "No available qualities detected."},
    "probe_failed": {"zh": "检测失败: {err}", "en": "Detection failed: {err}"},
    "downgrade": {"zh": "（未检测到 ffmpeg，已降级为单流下载，画质可能略低）", "en": "(ffmpeg not found, single-stream fallback, quality may be lower)"},
    "worker_err": {"zh": "发生错误: {err}", "en": "Error: {err}"},
    "retry_one": {"zh": "{prefix}网络异常，正在重试（第 {n}/{max} 次）…", "en": "{prefix}Network error, retrying ({n}/{max})…"},
}


def available_langs():
    """返回支持的 (代码, 显示名) 列表。"""
    return list(_LANGS.items())


def current_lang():
    return _current_lang


def set_language(lang, persist=True):
    """切换语言并通知所有监听者（窗口会重新翻译界面）。persist=True 时写入配置。"""
    global _current_lang
    if lang not in _LANGS or lang == _current_lang:
        return
    _current_lang = lang
    if persist:
        save_lang(lang)
    for cb in list(_listeners):
        try:
            cb()
        except Exception:
            pass


def on_language_change(cb):
    """注册语言切换回调（通常传窗口的 _retranslate）。"""
    _listeners.append(cb)
    def _unregister():
        try:
            _listeners.remove(cb)
        except ValueError:
            pass
    return _unregister


def tr(key, **kwargs):
    """取当前语言的文本。支持 {占位符} 格式化。"""
    entry = _TEXTS.get(key)
    if not entry:
        return key
    text = entry.get(_current_lang) or entry.get(_DEFAULT_LANG) or key
    if kwargs:
        try:
            text = text.format(**kwargs)
        except Exception:
            pass
    return text


# ---- 配置持久化 ----
_CONFIG_PATH = os.path.join(os.path.expanduser("~"), "YouTubeDownloader_lang.json")


def load_lang():
    """从配置文件读取语言偏好，失败返回默认语言。"""
    global _current_lang
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            lang = json.load(f).get("lang", _DEFAULT_LANG)
        if lang in _LANGS:
            _current_lang = lang
    except Exception:
        pass
    return _current_lang


def save_lang(lang):
    """写入语言偏好到配置文件。"""
    try:
        with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump({"lang": lang}, f, ensure_ascii=False)
    except Exception:
        pass
