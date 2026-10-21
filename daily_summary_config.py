"""Persistent, server-owned daily digest scheduling and delivery choices."""
import json
import os
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _clock_env(name, default, maximum):
    try:
        value = int(os.environ.get(name, default))
        return value if 0 <= value <= maximum else default
    except (TypeError, ValueError):
        return default


DEFAULT_HOUR = _clock_env("DAILY_SUMMARY_HOUR", 21, 23)
DEFAULT_MINUTE = _clock_env("DAILY_SUMMARY_MINUTE", 0, 59)
CONFIG_KEY = "daily_summary_config"
DELIVERY_METHODS = {"inapp", "email", "both"}
_TIME = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]\Z")


def get_config():
    from models import get_app_state
    config = {"enabled": True, "generate_at": f"{DEFAULT_HOUR:02d}:{DEFAULT_MINUTE:02d}",
              "delivery_method": "both"}
    try:
        stored = json.loads(get_app_state(CONFIG_KEY, "{}"))
    except (ValueError, TypeError):
        stored = {}
    if isinstance(stored, dict):
        if type(stored.get("enabled")) is bool:
            config["enabled"] = stored["enabled"]
        if isinstance(stored.get("generate_at"), str) and _TIME.fullmatch(stored["generate_at"]):
            config["generate_at"] = stored["generate_at"]
        if isinstance(stored.get("delivery_method"), str) and stored["delivery_method"] in DELIVERY_METHODS:
            config["delivery_method"] = stored["delivery_method"]
    timezone = os.environ.get("TZ", "Asia/Shanghai").strip() or "Asia/Shanghai"
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        timezone = "Asia/Shanghai"
    return {**config, "timezone": timezone}


def save_config(data):
    from models import get_db
    if not isinstance(data, dict) or set(data) - {"enabled", "generate_at", "delivery_method"}:
        raise ValueError("不支持的每日总结配置项")
    if "enabled" in data and type(data["enabled"]) is not bool:
        raise ValueError("每日总结开关必须为布尔值")
    if "generate_at" in data and (not isinstance(data["generate_at"], str) or not _TIME.fullmatch(data["generate_at"])):
        raise ValueError("生成时间应为 00:00 至 23:59（HH:MM）")
    if "delivery_method" in data and (not isinstance(data["delivery_method"], str) or data["delivery_method"] not in DELIVERY_METHODS):
        raise ValueError("请选择网页内通知、邮件或两者均开启")
    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    try:
        config = get_config()
        config.pop("timezone")  # The deployment's TZ remains the source of truth.
        config.update(data)
        db.execute("INSERT INTO app_state(key,value,updated_at) VALUES(?,?,datetime('now')) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                   (CONFIG_KEY, json.dumps(config)))
        db.commit()
    except Exception:
        db.rollback()
        raise
    return get_config()
