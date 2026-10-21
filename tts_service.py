"""Server-owned Kokoro configuration and deterministic, faithful listening text."""
from __future__ import annotations

import base64
import json
import math
import os
import re
import time
import uuid
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from ai_service import validate_ai_endpoint_base_url
from network_safety import assert_ai_endpoint_url, safe_post

NORMALIZATION_DEFAULTS = {
    "normalize": True, "unit_normalization": False, "url_normalization": True,
    "email_normalization": True, "optional_pluralization_normalization": True,
    "phone_normalization": True, "replace_remaining_symbols": True,
}
DEFAULT_CONFIG = {
    "enabled": False, "endpoint": "", "model": "kokoro",
    "voice": "zf_001(0.5)+zf_022(0.5)", "lang_code": "z", "speed": 1.3,
    "response_format": "mp3", "stream": True, "volume_multiplier": None,
    "return_download_link": False, "download_format": None,
    "normalization_options": None,
    "auth_type": "basic", "username": "", "password": "", "authorization": "",
    "revision": "initial",
}
CONFIG_KEY = "system_tts_config"


class TTSError(Exception):
    def __init__(self, code, message, status=502, admin_detail=""):
        super().__init__(message)
        self.code, self.status, self.admin_detail = code, status, admin_detail


def get_config():
    from models import get_app_state
    try:
        stored = json.loads(get_app_state(CONFIG_KEY, "{}"))
    except (ValueError, TypeError):
        stored = {}
    config = {**DEFAULT_CONFIG, **(stored if isinstance(stored, dict) else {})}
    # Older configurations may select formats that are no longer supported.
    config["response_format"] = "mp3"
    if config["download_format"] is not None:
        config["download_format"] = "mp3"
    return config


def configured(config=None):
    config = config if config is not None else get_config()
    credentials = (bool(config.get("username") and config.get("password"))
                   if config.get("auth_type") == "basic" else bool(config.get("authorization")))
    return bool(config.get("enabled") and config.get("endpoint") and config.get("model")
                and config.get("voice") and credentials)


def public_config(config):
    return {**{k: v for k, v in config.items() if k not in {"password", "authorization", "revision"}},
            "has_password": bool(config.get("password")),
            "has_authorization": bool(config.get("authorization")), "available": configured(config)}


def save_config(data):
    from models import get_db
    allowed = set(DEFAULT_CONFIG) - {"revision"}
    clears = {"clear_password", "clear_authorization"}
    if not isinstance(data, dict) or set(data) - allowed - clears:
        raise ValueError("不支持的语音配置项")
    for key in {"enabled", "stream", "return_download_link", *clears} & data.keys():
        if type(data[key]) is not bool:
            raise ValueError("开关值必须为布尔值")
    for key in allowed - {"enabled", "speed", "stream", "return_download_link",
                          "volume_multiplier", "download_format", "normalization_options", "lang_code"}:
        if key in data and (not isinstance(data[key], str) or len(data[key]) > 4096):
            raise ValueError("语音配置格式无效")
        if key in data and any(ord(c) < 32 or ord(c) == 127 for c in data[key]):
            raise ValueError("语音配置不能包含控制字符")
    for key in ("download_format", "lang_code"):
        if key in data and data[key] is not None and (
            not isinstance(data[key], str) or len(data[key]) > 512 or
            any(ord(c) < 32 or ord(c) == 127 for c in data[key])
        ):
            raise ValueError("语音配置格式无效")
    if "normalization_options" in data and data["normalization_options"] is not None:
        options = data["normalization_options"]
        if not isinstance(options, dict) or set(options) != set(NORMALIZATION_DEFAULTS) or any(
            type(value) is not bool for value in options.values()
        ):
            raise ValueError("文本规范化选项无效")
    # Serialize read/merge/write so saving unrelated fields cannot overwrite a
    # concurrent credential rotation. Empty secret fields preserve saved values.
    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    try:
        config = get_config()
        for key, value in data.items():
            if key in allowed and (key not in {"password", "authorization"} or value):
                config[key] = value.strip() if isinstance(value, str) and key != "password" else value
        for key in ("password", "authorization"):
            if data.get("clear_" + key):
                config[key] = ""
        if config["auth_type"] not in {"basic", "authorization"}:
            raise ValueError("请选择 Basic Auth 或自定义 Authorization")
        if ":" in config["username"]:
            raise ValueError("Basic Auth 用户名不能包含冒号")
        speed = config["speed"]
        if type(speed) not in {int, float} or not math.isfinite(speed) or not 0.25 <= speed <= 4:
            raise ValueError("合成语速应在 0.25 到 4 之间")
        volume = config["volume_multiplier"]
        if volume is not None and (type(volume) not in {int, float} or not math.isfinite(volume) or volume < 0):
            raise ValueError("音量倍数必须是非负数字")
        if config["model"] not in {"kokoro", "tts-1", "tts-1-hd"}:
            raise ValueError("不支持的语音模型")
        if config["response_format"] != "mp3":
            raise ValueError("语音播放仅支持 MP3 格式")
        if config["download_format"] not in {None, "mp3"}:
            raise ValueError("语音下载仅支持 MP3 格式")
        for key in ("endpoint", "model", "voice"):
            if len(config[key]) > 512 or (config["enabled"] and not config[key]):
                raise ValueError("请填写服务地址、模型和音色")
        if config["endpoint"]:
            config["endpoint"] = validate_ai_endpoint_base_url(assert_ai_endpoint_url(config["endpoint"])).rstrip("/")
        if config["enabled"] and not configured(config):
            raise ValueError("启用语音服务前请填写完整的鉴权信息")
        config["revision"] = uuid.uuid4().hex
        db.execute("INSERT INTO app_state(key,value,updated_at) VALUES(?,?,datetime('now')) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                   (CONFIG_KEY, json.dumps(config, ensure_ascii=False)))
        db.commit()
        return config
    except Exception:
        db.rollback()
        raise


_ACTION = re.compile(r"^(?:点击(?:这里|此处)?(?:查看|阅读|下载|打开)?(?:原文|全文|更多)?|查看原文|阅读原文|阅读全文|返回顶部|分享(?:到.*)?|订阅|关注我们|read more|click here|share|subscribe)[\s。.!！›»→]*$", re.I)
_URL = re.compile(r"(?:https?://|www\.)[^\s<>\u3000-\u303f\uff00-\uffef]+", re.I)


def _plain_line(text):
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = _URL.sub("", text)
    text = re.sub(r"\[\d{1,3}\]|【\d{1,3}】|\[\^\w+\]", "", text)
    text = re.sub(r"[\U0001f000-\U0001faff\u2600-\u27bf\ufe0f\u200b-\u200f\ufeff]", "", text)
    text = re.sub(r"(?m)^\s*(?:#{1,6}\s*|>\s*|[-*+]\s+)", "", text)
    text = text.replace("**", "").replace("__", "").replace("`", "")
    text = re.sub(r"(?<!\w)[*_](?=\S)|(?<=\S)[*_](?!\w)", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\(\s*\)|（\s*）", "", text).strip()
    if re.fullmatch(r"(?:\[|【|（|\()?\s*(?:图片|配图|链接|image|photo|link|图\s*\d+)\s*(?:\]|】|）|\))?", text, re.I):
        return ""
    if not re.search(r"[\w\u4e00-\u9fff]", text) or _ACTION.fullmatch(text):
        return ""
    return text


def listening_lines(html):
    """Drop page mechanics, retain meaningful anchors, quotes, figures and facts."""
    html = re.sub(r"```[\s\S]*?```", "\n此处包含代码，可查看原文。\n", html or "")
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.select("script,style,noscript,template,svg,canvas,img,picture,video,audio,iframe,button,input,nav,footer,[hidden],[aria-hidden='true']"):
        tag.decompose()
    for tag in soup.select("pre"):
        tag.replace_with("\n此处包含代码，可查看原文。\n")
    for tag in soup.select("sup a, a[role='doc-noteref']"):
        tag.decompose()
    for tag in soup.select("a"):
        if _ACTION.fullmatch(tag.get_text(" ", strip=True)):
            tag.decompose()
    for caption in soup.select("figcaption"):
        text = caption.get_text(" ", strip=True)
        if re.match(r"^(?:图片|图源|图片来源|摄影|Photo|Image|Credit)[：:\s]", text, re.I) or re.fullmatch(r"图\s*\d+[：:\s]*", text):
            caption.decompose()
    for table in soup.select("table"):
        rows = [[_plain_line(cell.get_text(" ", strip=True)) for cell in row.find_all(["th", "td"], recursive=False)]
                for row in table.find_all("tr")]
        simple = rows and len(rows) <= 10 and all(len(row) == 2 and all(row) for row in rows) and not table.select("[rowspan],[colspan]")
        text = "\n".join("，".join(row) + "。" for row in rows) if simple else "详细数据见原文表格。"
        table.replace_with("\n" + text + "\n")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    # Boundaries only at block elements: inline links/emphasis stay in the same sentence.
    for tag in soup.find_all(["p", "div", "li", "h1", "h2", "h3", "h4", "blockquote", "section", "article"]):
        tag.insert_before("\n")
        tag.insert_after("\n")
    lines = []
    for raw in soup.get_text().splitlines():
        line = _plain_line(raw)
        if line and (not lines or line != lines[-1]):
            lines.append(line)
    return lines


def reading_content(article, result):
    """Use the same permission-filtered variants and title order as the reader."""
    selected = dict(article)
    title_kind = "original"
    required = set()
    for kind, preferences in (
        ("translated_title_summary", ("view_title_translation", "view_title_shortening")),
        ("title_translation", ("view_title_translation",)),
        ("title_summary", ("view_title_shortening",)),
    ):
        if result.get(kind):
            selected["title"] = result[kind]
            title_kind = kind
            required.update(preferences)
            break
    translation = result.get("translation") or ""
    translated_title, html = "", ""
    if translation.lstrip().startswith("{"):
        try:
            payload = json.loads(translation)
            html, translated_title = payload.get("html") or "", payload.get("title") or ""
        except (ValueError, TypeError):
            pass
    elif translation.lstrip().startswith("<"):
        html = translation
    has_translation = isinstance(html, str) and bool(listening_lines(html))
    if has_translation:
        selected["body_html"] = html
        required.add("view_translation")
    selection = {"title_kind": title_kind, "has_translation": has_translation,
                 "required_preferences": sorted(required)}
    aliases = [article.get("title", ""), result.get("title_translation", ""), translated_title]
    return selected, selection, aliases


def listening_text(title, body, summary="", *, title_aliases=()):
    body_lines = listening_lines(body)
    title = " ".join(listening_lines(title))
    titles = {" ".join(listening_lines(value)).rstrip("。.!！") for value in (title, *title_aliases) if value}
    # Embedded full titles must not be read again after the displayed short title.
    while body_lines and body_lines[0].rstrip("。.!！") in titles:
        body_lines.pop(0)
    if not body_lines:
        raise TTSError("TTS_EMPTY_CONTENT", "这篇文章暂无可供语音阅读的内容", 422)
    def sentences(lines):
        return "\n\n".join(line if re.search(r"[。！？.!?；;：:]([”’\"』」）)]*)$", line) else line + "。" for line in lines)
    parts = [sentences([title])] if title else []
    summary_lines = listening_lines(summary)
    if summary_lines:
        parts += ["内容摘要。", sentences(summary_lines), "以下是正文。"]
    parts.append(sentences(body_lines))
    text = "\n\n".join(parts)
    if len(text) > 100000:
        raise TTSError("TTS_TEXT_TOO_LONG", "这篇文章过长，暂时无法生成完整语音", 422)
    return text, bool(summary_lines)


def synthesize(config, text, target: Path, *, max_bytes=64 * 1024 * 1024, on_progress=None):
    """Spool validated MP3 bytes and publish flushed progress to live listeners."""
    if not configured(config):
        raise TTSError("TTS_UNAVAILABLE", "语音服务暂未开放", 409)
    if config.get("response_format", "mp3") != "mp3" or config.get("download_format") not in {None, "mp3"}:
        raise TTSError("TTS_INVALID_FORMAT", "语音仅支持 MP3 格式", 400)
    headers = {"Content-Type": "application/json", "Accept": "audio/mpeg"}
    if config["auth_type"] == "basic":
        credentials = base64.b64encode((config["username"] + ":" + config["password"]).encode()).decode()
        headers["Authorization"] = "Basic " + credentials
    else:
        headers["Authorization"] = config["authorization"]
    started = time.monotonic()
    try:
        timeout = max(10, min(600, int(os.environ.get("TTS_REQUEST_TIMEOUT_SECONDS", "300"))))
    except (ValueError, TypeError):
        timeout = 300
    try:
        payload = {"model": config["model"], "input": text, "voice": config["voice"],
                   "speed": config["speed"], "response_format": "mp3",
                   "stream": config.get("stream", True),
                   "return_download_link": config.get("return_download_link", False)}
        if config.get("lang_code"):
            payload["lang_code"] = config["lang_code"]
        for key in ("volume_multiplier", "download_format", "normalization_options"):
            if config.get(key) is not None:
                payload[key] = config[key]
        with safe_post(validate_ai_endpoint_base_url(config["endpoint"]).rstrip("/") + "/audio/speech",
                       headers=headers, json=payload, timeout=(10, timeout), stream=True,
                       max_redirects=0, allow_private=os.environ.get("AI_ALLOW_PRIVATE_ENDPOINTS", "").lower() in {"1", "true", "yes", "on"}) as response:
            if response.status_code != 200:
                raise TTSError("TTS_UPSTREAM_ERROR", "语音服务暂时不可用，请稍后重试", 502,
                               "上游返回 HTTP " + str(response.status_code) + ("，请检查鉴权配置" if response.status_code in {401, 403} else ""))
            size, prefix = 0, b""
            with target.open("wb") as output:
                # Small reads avoid waiting for a large buffer before first playback.
                for chunk in response.iter_content(4096):
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > max_bytes:
                        raise TTSError("TTS_AUDIO_TOO_LARGE", "生成的语音过大，请稍后重试", 502)
                    if time.monotonic() - started > timeout:
                        raise requests.Timeout()
                    prefix = (prefix + chunk)[:4096]
                    if len(prefix) >= 3 and not (prefix.startswith(b"ID3") or
                            (prefix[0] == 0xff and prefix[1] & 0xe0 == 0xe0)):
                        raise TTSError("TTS_INVALID_AUDIO", "语音服务返回的音频无效，请重试", 502)
                    output.write(chunk)
                    if on_progress and size >= 16:
                        output.flush()
                        on_progress(size)
            # Reject HTML/JSON masquerading as successful audio, independent of MIME.
            if size < 16 or not (prefix.startswith(b"ID3") or (prefix[0] == 0xff and prefix[1] & 0xe0 == 0xe0)):
                raise TTSError("TTS_INVALID_AUDIO", "语音服务返回的音频无效，请重试", 502)
    except requests.Timeout:
        raise TTSError("TTS_TIMEOUT", "语音生成超时，请重试", 504) from None
    except TTSError:
        raise
    except (requests.RequestException, ValueError, OSError):
        raise TTSError("TTS_UPSTREAM_ERROR", "语音服务暂时不可用，请稍后重试", 502) from None
