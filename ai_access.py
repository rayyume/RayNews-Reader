"""Server-owned AI grants, user display preferences and account migration."""
import json

SERVICES = ("summary", "title_shortening", "translation", "daily_digest", "tts")
PREFERENCES = {
    "view_summary": "summary",
    "view_title_translation": "translation",
    "view_translation": "translation",
    "view_title_shortening": "title_shortening",
    "view_daily_digest": "daily_digest",
    "view_tts": "tts",
}
GENERATION_FLAGS = ("auto_summary_enabled", "auto_title_summary_enabled",
                    "auto_translate_title", "auto_translate_content")


def migrate_accounts(db, schema):
    """Serialize a one-time backfill, including users without settings rows."""
    if db.execute("SELECT 1 FROM app_state WHERE key='ai_access_v6'").fetchone() and all(
        db.execute("SELECT 1 FROM sqlite_master WHERE type=? AND name=?", (kind, name)).fetchone()
        for kind, name in (("table", "ai_permissions"), ("trigger", "ai_permissions_new_user"))
    ):
        return
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute("CREATE TABLE IF NOT EXISTS ai_permissions (user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE, "
                   + ",".join(key + " INTEGER NOT NULL DEFAULT 0 CHECK(" + key + " IN (0,1))" for key in SERVICES) + ")")
        if not db.execute("SELECT 1 FROM app_state WHERE key='ai_access_v6'").fetchone():
            db.execute("INSERT OR IGNORE INTO user_settings(user_id) SELECT id FROM users")
            columns = {row[1] for row in db.execute("PRAGMA table_info(user_settings)")}
            for key in PREFERENCES:
                if key not in columns:
                    db.execute("ALTER TABLE user_settings ADD COLUMN " + key + " INTEGER NOT NULL DEFAULT 0")
            for target, old in (("view_summary", "share_view_summary"),
                                ("view_title_translation", "share_view_title"),
                                ("view_title_shortening", "share_view_title"),
                                ("view_translation", "share_view_translation")):
                if old in columns:
                    db.execute("UPDATE user_settings SET " + target + "=CASE WHEN " + old + "=1 THEN 1 ELSE 0 END")
            db.execute("UPDATE user_settings SET view_daily_digest=1")
            db.execute("INSERT OR IGNORE INTO ai_permissions(user_id," + ",".join(SERVICES) + ") SELECT id,1,1,1,1,1 FROM users")
            generation = {}
            for key in GENERATION_FLAGS:
                generation[key] = key in columns and bool(db.execute("SELECT 1 FROM user_settings s JOIN users u ON u.id=s.user_id WHERE u.role='admin' AND s." + key + "=1 LIMIT 1").fetchone())
            db.execute("INSERT OR REPLACE INTO app_state(key,value,updated_at) VALUES('ai_generation',?,datetime('now'))", (json.dumps(generation),))
            # Rebuild from the new schema to remove credentials' associated
            # sharing health state without retaining dead columns indefinitely.
            table = schema[schema.index("CREATE TABLE IF NOT EXISTS user_settings ("):]
            table = table[:table.index("\n);") + 3].replace("IF NOT EXISTS user_settings", "user_settings_v6")
            db.execute(table)
            new_columns = [row[1] for row in db.execute("PRAGMA table_info(user_settings_v6)")]
            names = ",".join(new_columns)
            db.execute("INSERT INTO user_settings_v6(" + names + ") SELECT " + names + " FROM user_settings")
            db.execute("DROP TABLE user_settings")
            db.execute("ALTER TABLE user_settings_v6 RENAME TO user_settings")
            db.execute("DROP TABLE IF EXISTS ai_configs")
            db.execute("DELETE FROM notifications WHERE type IN ('share_suspended','share_restored')")
            db.execute("INSERT INTO app_state(key,value,updated_at) VALUES('ai_access_v6','1',datetime('now'))")
        db.execute("CREATE TRIGGER IF NOT EXISTS ai_permissions_new_user AFTER INSERT ON users BEGIN INSERT INTO ai_permissions(user_id) VALUES(NEW.id); END")
        db.commit()
    except Exception:
        db.rollback()
        raise


def permissions(user_id):
    from models import get_db
    db = get_db()
    user = db.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
    row = db.execute("SELECT * FROM ai_permissions WHERE user_id=?", (user_id,)).fetchone()
    return {key: bool(user and (user[0] == "admin" or (row and row[key]))) for key in SERVICES}


def access(user_id):
    from models import get_user_settings
    from tts_service import configured
    grants = permissions(user_id)
    settings = get_user_settings(user_id) or {}
    services = {key: key != "tts" or configured() for key in SERVICES}
    return {"permissions": grants, "services": services,
            **{key: bool(settings.get(key) and grants[service] and services[service]) for key, service in PREFERENCES.items()}}


def daily_enabled(user_id):
    return access(user_id)["view_daily_digest"]


def set_permissions(user_id, updates):
    if (not isinstance(updates, dict) or not updates or set(updates) - set(SERVICES)
            or any(type(value) is not bool for value in updates.values())):
        raise ValueError("权限必须为五项服务的布尔值")
    from models import get_db
    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    try:
        row = db.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            raise KeyError(user_id)
        if row[0] == "admin":
            raise ValueError("管理员始终具有全部权限")
        db.execute("INSERT OR IGNORE INTO ai_permissions(user_id) VALUES(?)", (user_id,))
        db.execute("INSERT OR IGNORE INTO user_settings(user_id) VALUES(?)", (user_id,))
        for service, value in updates.items():
            db.execute("UPDATE ai_permissions SET " + service + "=? WHERE user_id=?", (int(value), user_id))
            if not value:
                for preference, required in PREFERENCES.items():
                    if required == service:
                        db.execute("UPDATE user_settings SET " + preference + "=0 WHERE user_id=?", (user_id,))
        db.commit()
    except Exception:
        db.rollback()
        raise
    return permissions(user_id)


def generation_settings():
    from models import get_app_state
    stored = json.loads(get_app_state("ai_generation", "{}"))
    return {key: bool(stored.get(key)) for key in GENERATION_FLAGS}
