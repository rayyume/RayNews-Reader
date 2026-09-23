"""Shared source category metadata helpers for RayNews."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from datetime import datetime
from urllib.parse import urlsplit
import tldextract

from news_schema import ensure_article_source_columns as _ensure_article_source_columns


CATEGORY_ORDER = ["News", "Tech", "Biz", "Info"]
CATEGORY_NAMES = {
    "News": "政经新闻",
    "Tech": "科技动态",
    "Biz": "商业聚焦",
    "Info": "其他信息",
}

DEFAULT_CATEGORIES = [
    ("News", "政经新闻"),
    ("Tech", "科技动态"),
    ("Biz", "商业聚焦"),
    ("Info", "其他信息"),
]
UNCATEGORIZED = "Uncategorized"

VALID_STATUSES = {"pending", "review", "classified", "manual", "failed"}


def ensure_article_source_columns(conn: sqlite3.Connection) -> None:
    """Upgrade split source fields through the shared migration protocol."""
    _ensure_article_source_columns(conn)

# ─── Built-in publisher identity registry ───────────────────────────
# Format: "registrable domain" → "canonical publisher name".
# Categories are instance configuration and deliberately do not live here.
KNOWN_DOMAINS: dict[str, str] = {
    "zaobao.com": "联合早报",
    "jiemian.com": "界面新闻",
    "ifeng.com": "凤凰网",
    "thepaper.cn": "澎湃新闻",
    "bbc.com": "BBC",
    "bbc.co.uk": "BBC",
    "reuters.com": "路透社",
    "wsj.com": "华尔街日报",
    "ft.com": "金融时报",
    "nytimes.com": "纽约时报",
    "bloomberg.com": "彭博社",
    "cnn.com": "CNN",
    "theguardian.com": "卫报",
    "scmp.com": "南华早报",
    "dw.com": "德国之声",
    "rfi.fr": "法国国际广播",
    "nikkei.com": "日经新闻",
    "yicai.com": "第一财经",
    "apnews.com": "美联社",
    "aljazeera.com": "半岛电视台",
    "france24.com": "France 24",
    "huanqiu.com": "环球网",
    "guancha.cn": "观察者网",
    "cna.com.tw": "中央社",
    "ltn.com.tw": "自由时报",
    "udn.com": "联合报",
    "straitstimes.com": "海峡时报",
    "rthk.hk": "香港电台",

    "cnbeta.com": "cnBeta",
    "cnbeta.com.tw": "cnBeta",
    "sspai.com": "少数派",
    "ifanr.com": "爱范儿",
    "36kr.com": "36氪",
    "chiphell.com": "Chiphell",
    "macrumors.com": "MacRumors",
    "techcrunch.com": "TechCrunch",
    "theverge.com": "The Verge",
    "arstechnica.com": "Ars Technica",
    "wired.com": "Wired",
    "github.com": "GitHub",
    "ruanyifeng.com": "阮一峰",
    "zhihu.com": "知乎",
    "ithome.com": "IT之家",
    "solidot.org": "Solidot",
    "producthunt.com": "Product Hunt",
    "xiaohongshu.com": "小红书",
    "huggingface.co": "HuggingFace",
    "openai.com": "OpenAI",
    "anthropic.com": "Anthropic",
    "9to5mac.com": "9to5Mac",
    "oschina.net": "开源中国",
    "v2ex.com": "V2EX",
    "nodeseek.com": "NodeSeek",
    "hackernews.com": "Hacker News",
    "infoq.cn": "InfoQ",
    "geekpark.net": "极客公园",
    "pingwest.com": "品玩",
    "sohu.com": "搜狐",

    "gelonghui.com": "格隆汇",
    "jin10.com": "金十数据",
    "pedaily.cn": "投资界",
    "wallstreetcn.com": "华尔街见闻",
    "latepost.com": "晚点",
    "cls.cn": "财联社",
    "eastmoney.com": "东方财富",
    "sina.com.cn": "新浪财经",
    "fortunechina.com": "财富中文网",
    "hbr.org": "哈佛商业评论",
    "caixin.com": "财新",
    "fortune.com": "财富",
    "fastcompany.com": "Fast Company",
    "cnbc.com": "CNBC",
    "economist.com": "经济学人",
    "businessinsider.com": "商业内幕",
    "forbes.com": "福布斯",
    "barrons.com": "巴伦周刊",
    "stcn.com": "证券时报",
    "21jingji.com": "21世纪经济报道",
    "nbd.com.cn": "每日经济新闻",
    "10jqka.com.cn": "同花顺",
    "ce.cn": "中国经济网",
    "cnstock.com": "上海证券报",
    "cs.com.cn": "中证网",

    "uscreditcardguide.com": "美卡指南",
    "travelafterwork.com": "酒店圈儿",

    # ── 微信公众号 (域名为 mp.weixin.qq.com, 但会根据文章内容进一步识别) ──
    # 不在 KNOWN_DOMAINS 中注册 weixin 域名，因为不同公众号是不同的来源
}

# Domains to exclude from extraction (platform/aggregator domains)
_DOMAIN_EXCLUDE = {
    "telegra.ph", "t.me", "telegram.me", "telegram.org",
    "mp.weixin.qq.com", "weixin.qq.com",
    "x.com", "twitter.com", "facebook.com", "fb.com",
    "instagram.com", "youtube.com", "youtu.be",
    "reddit.com", "redd.it",
    "google.com", "bing.com", "baidu.com",
    "amazon.com", "apple.com",
    "rayyu.me", "localhost", "127.0.0.1",
}


_TLD_EXTRACTOR = tldextract.TLDExtract(suffix_list_urls=())


def _root_domain(host: str) -> str | None:
    """Normalize a hostname to its matchable root domain."""
    host = host.lower().strip().rstrip(".")
    if ":" in host:
        try:
            host = urlsplit("//" + host).hostname or host
        except ValueError:
            pass
    # WeChat hosts identify the publishing platform, not the official account.
    # Check before registrable-domain reduction, which turns them into qq.com.
    if host == "weixin.qq.com" or host.endswith(".weixin.qq.com"):
        return None
    extracted = _TLD_EXTRACTOR(host)
    root = extracted.top_domain_under_public_suffix or host
    return None if root in _DOMAIN_EXCLUDE else root


def extract_domain_from_url(value: str) -> str | None:
    """Return a matchable root domain from an HTTP(S) URL."""
    try:
        parsed = urlsplit((value or "").strip())
        host = (parsed.hostname or "").lower()
    except (TypeError, ValueError):
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not host:
        return None
    return _root_domain(host)


def extract_domains_from_html(html: str) -> list[str]:
    """Extract unique root domains from all href links in HTML.

    Strips subdomains (www.zaobao.com → zaobao.com), excludes
    platform/aggregator domains, and returns unique results in
    discovery order.
    """
    if not html:
        return []
    urls = re.findall(r'href=["\']?https?://([^/"\'<>\s]+)', html or "")
    seen = set()
    domains = []
    for host in urls:
        root = _root_domain(host)
        if not root:
            continue
        if root not in seen:
            seen.add(root)
            domains.append(root)
    return domains


def lookup_source_by_domain(domains: list[str]) -> tuple[str, str] | None:
    """Look up (source_name, category) from a list of domains.

    Returns the first match found, or None if no domain is known.
    """
    for domain in domains:
        if domain in KNOWN_DOMAINS:
            return KNOWN_DOMAINS[domain], UNCATEGORIZED
    return None


def weighted_len(text: str) -> int:
    """ASCII counts as 1, non-ASCII counts as 2."""
    return sum(1 if ord(ch) < 128 else 2 for ch in text)


def clamp_weighted(text: str, limit: int = 20) -> str:
    result = []
    total = 0
    for ch in (text or "").strip():
        weight = 1 if ord(ch) < 128 else 2
        if total + weight > limit:
            break
        result.append(ch)
        total += weight
    return "".join(result).strip()


def local_short_source_name(source: str) -> str:
    """Deterministic fallback cleanup for verbose source names."""
    text = (source or "").strip()
    text = re.sub(r"\s*-\s*Telegram\s+Channel\s*$", "", text, flags=re.I)
    text = re.sub(r"\s*\|\s*Telegram\s+Channel\s*$", "", text, flags=re.I)
    text = re.sub(r"\s*\(\s*Telegram\s+Channel\s*\)\s*$", "", text, flags=re.I)
    text = re.sub(r"\s*频道\s*$", "", text)
    text = re.sub(r"[\U0001F000-\U0010FFFF]", "", text).strip()
    text = re.sub(r"\s+", " ", text).strip()

    return clamp_weighted(text or source, 20)


def init_source_categories(conn: sqlite3.Connection) -> None:
    ensure_article_source_columns(conn)
    definitions_existed = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name = 'source_category_definitions'"
    ).fetchone() is not None
    conn.execute("""
        CREATE TABLE IF NOT EXISTS source_category_definitions (
            category TEXT PRIMARY KEY,
            label TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 1,
            is_system INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    if not definitions_existed:
        for index, (category, label) in enumerate(DEFAULT_CATEGORIES):
            conn.execute(
                "INSERT OR IGNORE INTO source_category_definitions "
                "(category, label, sort_order) VALUES (?, ?, ?)",
                (category, label, index),
            )
        conn.execute(
            "INSERT OR IGNORE INTO source_category_definitions "
            "(category, label, sort_order, is_system) VALUES (?, ?, ?, 1)",
            (UNCATEGORIZED, "待分类", len(DEFAULT_CATEGORIES)),
        )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS publisher_domains (
            domain TEXT PRIMARY KEY,
            group_source TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            is_builtin INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS source_categories (
            source TEXT PRIMARY KEY,
            category TEXT NOT NULL DEFAULT 'Info',
            label TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            confidence REAL,
            reason TEXT,
            sample_titles TEXT,
            suggested_category TEXT,
            suggested_label TEXT,
            suggested_confidence REAL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    category_columns = {row[1] for row in conn.execute("PRAGMA table_info(source_categories)")}
    for name, definition in (
        ("suggested_category", "TEXT"),
        ("suggested_label", "TEXT"),
        ("suggested_confidence", "REAL"),
    ):
        if name not in category_columns:
            conn.execute(f"ALTER TABLE source_categories ADD COLUMN {name} {definition}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_source_categories_status ON source_categories(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_source_categories_category ON source_categories(category)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS source_aliases (
            alias_source  TEXT PRIMARY KEY,
            target_source TEXT NOT NULL,
            created_at    TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_source_categories (
            user_id    INTEGER NOT NULL,
            source     TEXT NOT NULL,
            category   TEXT NOT NULL DEFAULT 'Info',
            label      TEXT NOT NULL DEFAULT '',
            status     TEXT NOT NULL DEFAULT 'manual',
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (user_id, source)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_source_aliases (
            user_id       INTEGER NOT NULL,
            alias_source  TEXT NOT NULL,
            target_source TEXT NOT NULL,
            created_at    TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (user_id, alias_source)
        )
    """)

    # The built-in registry records identity only. Per-deployment labels and
    # categories stay in the database and can be edited independently.
    for domain, source_name in KNOWN_DOMAINS.items():
        conn.execute(
            "INSERT OR IGNORE INTO publisher_domains "
            "(domain, group_source, is_builtin) VALUES (?, ?, 1)",
            (domain, source_name),
        )
    conn.commit()


def category_definitions(conn: sqlite3.Connection, include_disabled: bool = False) -> list[dict]:
    _ensure_source_tables(conn)
    where = "" if include_disabled else "WHERE enabled = 1"
    rows = conn.execute(
        f"SELECT category, label, sort_order, enabled, is_system "
        f"FROM source_category_definitions {where} ORDER BY sort_order, category"
    ).fetchall()
    return [dict(row) for row in rows]


def valid_category(conn: sqlite3.Connection, category: str) -> bool:
    return any(row["category"] == category for row in category_definitions(conn))


def save_category_definition(
    conn: sqlite3.Connection,
    category: str,
    label: str,
    sort_order: int,
    enabled: bool = True,
    migration_target: str | None = None,
) -> dict:
    category = (category or "").strip()
    label = (label or "").strip()
    if not category or len(category) > 40 or not label or len(label) > 40:
        raise ValueError("invalid category or label")
    if category == UNCATEGORIZED:
        raise ValueError("system category cannot be edited")
    if int(sort_order) < 0:
        raise ValueError("invalid sort order")
    existing = conn.execute(
        "SELECT enabled FROM source_category_definitions WHERE category = ?", (category,)
    ).fetchone()
    if existing and existing[0] and not enabled:
        if not migration_target or migration_target == category:
            raise ValueError("choose a migration target before disabling this category")
        target = conn.execute(
            "SELECT enabled FROM source_category_definitions WHERE category = ?",
            (migration_target,),
        ).fetchone()
        if not target or not target[0]:
            raise ValueError("invalid migration target")
        conn.execute(
            "UPDATE source_categories SET category = ? WHERE category = ?",
            (migration_target, category),
        )
        conn.execute(
            "UPDATE user_source_categories SET category = ? WHERE category = ?",
            (migration_target, category),
        )
    conn.execute(
        "INSERT INTO source_category_definitions (category, label, sort_order, enabled) "
        "VALUES (?, ?, ?, ?) ON CONFLICT(category) DO UPDATE SET "
        "label=excluded.label, sort_order=excluded.sort_order, enabled=excluded.enabled, "
        "updated_at=datetime('now')",
        (category, label, int(sort_order), 1 if enabled else 0),
    )
    conn.commit()
    row = conn.execute(
        "SELECT category, label, sort_order, enabled, is_system "
        "FROM source_category_definitions WHERE category = ?", (category,)
    ).fetchone()
    return dict(row)


def delete_category_definition(conn: sqlite3.Connection, category: str, target: str) -> int:
    if category == UNCATEGORIZED:
        raise ValueError("system category cannot be deleted")
    if category == target or not valid_category(conn, target):
        raise ValueError("invalid migration target")
    row = conn.execute(
        "SELECT is_system FROM source_category_definitions WHERE category = ?", (category,)
    ).fetchone()
    if not row:
        raise ValueError("category not found")
    if row[0]:
        raise ValueError("system category cannot be deleted")
    conn.execute("UPDATE source_categories SET category = ? WHERE category = ?", (target, category))
    conn.execute("UPDATE user_source_categories SET category = ? WHERE category = ?", (target, category))
    conn.execute("DELETE FROM source_category_definitions WHERE category = ?", (category,))
    conn.commit()
    return conn.total_changes


def save_publisher_domain(conn: sqlite3.Connection, domain: str, group_source: str, enabled: bool = True) -> dict:
    root = _root_domain(domain)
    group_source = (group_source or "").strip()
    if not root or not group_source or len(group_source) > 100:
        raise ValueError("invalid domain mapping")
    conn.execute(
        "INSERT INTO publisher_domains (domain, group_source, enabled, is_builtin) "
        "VALUES (?, ?, ?, 0) ON CONFLICT(domain) DO UPDATE SET "
        "group_source=excluded.group_source, enabled=excluded.enabled, is_builtin=0, "
        "updated_at=datetime('now')",
        (root, group_source, 1 if enabled else 0),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM publisher_domains WHERE domain = ?", (root,)).fetchone()
    return dict(row)


def publisher_source_for_domain(conn: sqlite3.Connection, domain: str) -> str | None:
    root = _root_domain(domain)
    if not root:
        return None
    row = conn.execute(
        "SELECT group_source FROM publisher_domains WHERE domain = ? AND enabled = 1",
        (root,),
    ).fetchone()
    return (row["group_source"] if isinstance(row, sqlite3.Row) else row[0]) if row else root


def ensure_article_sources(conn: sqlite3.Connection) -> int:
    """Insert pending source records for every distinct article source."""
    try:
        init_source_categories(conn)
        aliases = conn.execute(
            "SELECT alias_source, target_source FROM source_aliases"
        ).fetchall()
        for row in aliases:
            alias = row["alias_source"] if isinstance(row, sqlite3.Row) else row[0]
            target = row["target_source"] if isinstance(row, sqlite3.Row) else row[1]
            conn.execute(
                "UPDATE articles SET group_source = ?, source = ? "
                "WHERE group_source = ? OR (TRIM(group_source) = '' AND source = ?)",
                (target, target, alias, alias),
            )

        rows = conn.execute(
            "SELECT DISTINCT COALESCE(NULLIF(group_source, ''), NULLIF(feed_source, ''), source) AS source "
            "FROM articles "
            "WHERE COALESCE(NULLIF(group_source, ''), NULLIF(feed_source, ''), source) IS NOT NULL "
            "  AND TRIM(COALESCE(NULLIF(group_source, ''), NULLIF(feed_source, ''), source)) != ''"
        ).fetchall()
        inserted = 0
        for row in rows:
            source = row["source"] if isinstance(row, sqlite3.Row) else row[0]
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO source_categories
                    (source, category, label, status, reason)
                VALUES (?, ?, ?, 'pending', 'discovered')
                """,
                (source, UNCATEGORIZED, local_short_source_name(source)),
            )
            inserted += cur.rowcount
        conn.commit()
        return inserted
    except Exception:
        conn.rollback()
        raise


def _ensure_source_tables(conn: sqlite3.Connection) -> None:
    """Create the source tables if a fresh deployment hasn't seeded them yet.

    Cheap no-op once they exist, so it's safe on the read path — unlike the full
    init_source_categories(), which also seeds rows and commits.
    """
    required = {"source_categories", "source_category_definitions", "publisher_domains"}
    tables = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    if not required.issubset(tables):
        init_source_categories(conn)


MAINTENANCE_THROTTLE_SECONDS = 60
_maintenance_lock = threading.Lock()
_maintenance_last_run = 0.0


def maintain_source_categories(conn: sqlite3.Connection, force: bool = False) -> dict:
    """Run the write-heavy source bookkeeping: discover new sources, drop stale ones.

    Both steps scan the whole articles table, so they must stay off the read path of
    GET /sources — that request has to stay fast even while a fetch cycle holds the
    write lock. Call this after a fetch cycle instead. Throttled to one pass per
    MAINTENANCE_THROTTLE_SECONDS per process; pass force=True to bypass (write paths
    that just changed articles need their changes reflected immediately).
    """
    global _maintenance_last_run
    if not force:
        with _maintenance_lock:
            if time.monotonic() - _maintenance_last_run < MAINTENANCE_THROTTLE_SECONDS:
                return {"ran": False, "discovered": 0, "deleted": 0}
    discovered = ensure_article_sources(conn)
    deleted = cleanup_stale_source_categories(conn)
    with _maintenance_lock:
        _maintenance_last_run = time.monotonic()
    return {"ran": True, "discovered": discovered, "deleted": deleted}


def cleanup_stale_source_categories(conn: sqlite3.Connection) -> int:
    """Remove discovered source rows that no longer have articles."""
    table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'source_categories'"
    ).fetchone()
    if not table:
        init_source_categories(conn)
    rows = conn.execute(
        """
        SELECT sc.source, sc.status, COUNT(a.id) AS article_count
        FROM source_categories sc
        LEFT JOIN articles a ON COALESCE(NULLIF(a.group_source, ''), NULLIF(a.feed_source, ''), a.source) = sc.source
        GROUP BY sc.source
        HAVING article_count = 0
        """
    ).fetchall()
    deleted = 0
    for row in rows:
        source = row["source"] if isinstance(row, sqlite3.Row) else row[0]
        cur = conn.execute(
            """
            DELETE FROM source_categories
            WHERE source = ? AND status IN ('pending', 'failed', 'review')
            """,
            (source,),
        )
        deleted += cur.rowcount

    deleted += conn.execute(
        """
        DELETE FROM source_aliases
        WHERE target_source NOT IN (SELECT source FROM source_categories)
        """
    ).rowcount
    deleted += conn.execute(
        """
        DELETE FROM user_source_categories
        WHERE source NOT IN (
            SELECT DISTINCT COALESCE(NULLIF(group_source, ''), NULLIF(feed_source, ''), source)
            FROM articles
            WHERE COALESCE(NULLIF(group_source, ''), NULLIF(feed_source, ''), source) IS NOT NULL
              AND TRIM(COALESCE(NULLIF(group_source, ''), NULLIF(feed_source, ''), source)) != ''
        )
          AND status IN ('pending', 'failed')
        """
    ).rowcount
    deleted += conn.execute(
        """
        DELETE FROM user_source_aliases
        WHERE NOT EXISTS (
            SELECT 1 FROM source_categories
            WHERE source_categories.source = user_source_aliases.target_source
        )
          AND NOT EXISTS (
            SELECT 1 FROM user_source_categories
            WHERE user_source_categories.user_id = user_source_aliases.user_id
              AND user_source_categories.source = user_source_aliases.target_source
        )
        """
    ).rowcount
    if deleted:
        conn.commit()
    return deleted


def delete_source_metadata(conn: sqlite3.Connection, sources: list[str]) -> int:
    """Delete category and alias metadata connected to a source-label group.

    If ``conn`` is already in a transaction, all four source metadata tables
    must have been bootstrapped before that transaction began.  This helper then
    leaves the caller's commit/rollback boundary untouched.  A standalone call
    may bootstrap missing tables and owns its own transaction boundary.
    """
    normalized = list(dict.fromkeys(
        str(source).strip() for source in sources if str(source).strip()
    ))
    if not normalized:
        return 0
    owns_transaction = not conn.in_transaction
    if owns_transaction:
        _ensure_source_tables(conn)
    else:
        required_tables = {
            "source_categories",
            "source_aliases",
            "user_source_categories",
            "user_source_aliases",
        }
        existing_tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name IN (?, ?, ?, ?)",
                tuple(required_tables),
            ).fetchall()
        }
        if existing_tables != required_tables:
            raise RuntimeError(
                "bootstrap source metadata tables before starting the caller transaction"
            )
    placeholders = ",".join("?" * len(normalized))
    deleted = 0
    try:
        deleted += conn.execute(
            f"DELETE FROM source_aliases "
            f"WHERE alias_source IN ({placeholders}) OR target_source IN ({placeholders})",
            (*normalized, *normalized),
        ).rowcount
        deleted += conn.execute(
            f"DELETE FROM user_source_aliases "
            f"WHERE alias_source IN ({placeholders}) OR target_source IN ({placeholders})",
            (*normalized, *normalized),
        ).rowcount
        deleted += conn.execute(
            f"DELETE FROM source_categories WHERE source IN ({placeholders})",
            normalized,
        ).rowcount
        deleted += conn.execute(
            f"DELETE FROM user_source_categories WHERE source IN ({placeholders})",
            normalized,
        ).rowcount
        if owns_transaction:
            conn.commit()
        return deleted
    except Exception:
        if owns_transaction:
            conn.rollback()
        raise


def find_merge_target(conn: sqlite3.Connection, source: str, label: str) -> str | None:
    """Find an existing source whose source name or label matches label."""
    label = (label or "").strip()
    if not label:
        return None
    rows = conn.execute(
        """
        SELECT sc.source, sc.status, COUNT(a.id) AS article_count
        FROM source_categories sc
        LEFT JOIN articles a ON COALESCE(NULLIF(a.group_source, ''), NULLIF(a.feed_source, ''), a.source) = sc.source
        WHERE sc.source != ?
          AND (sc.source = ? OR sc.label = ?)
        GROUP BY sc.source
        ORDER BY
          CASE sc.status WHEN 'manual' THEN 0 WHEN 'classified' THEN 1 ELSE 2 END,
          article_count DESC,
          sc.source COLLATE NOCASE
        LIMIT 1
        """,
        (source, label, label),
    ).fetchone()
    if not rows:
        return None
    return rows["source"] if isinstance(rows, sqlite3.Row) else rows[0]


def find_user_merge_target(conn: sqlite3.Connection, user_id: int, source: str, label: str) -> str | None:
    label = (label or "").strip()
    if not label:
        return None
    rows = effective_source_rows(conn, user_id)
    candidates = [
        row for row in rows
        if row.get("source") != source
        and not row.get("alias_target")
        and (row.get("source") == label or row.get("label") == label)
    ]
    candidates.sort(key=lambda row: (
        0 if row.get("status") == "manual" else 1,
        -(row.get("article_count") or 0),
        row.get("source") or "",
    ))
    return candidates[0]["source"] if candidates else None


def promote_user_source_settings(conn: sqlite3.Connection, user_id: int) -> dict:
    """Promote legacy per-user source settings into shared administrator settings."""
    category_rows = conn.execute(
        """
        SELECT source, category, label
        FROM user_source_categories
        WHERE user_id = ?
        ORDER BY updated_at ASC
        """,
        (user_id,),
    ).fetchall()
    alias_rows = conn.execute(
        """
        SELECT alias_source, target_source
        FROM user_source_aliases
        WHERE user_id = ?
        ORDER BY created_at ASC
        """,
        (user_id,),
    ).fetchall()

    promoted_categories = 0
    for row in category_rows:
        source = row["source"] if isinstance(row, sqlite3.Row) else row[0]
        category = row["category"] if isinstance(row, sqlite3.Row) else row[1]
        label = row["label"] if isinstance(row, sqlite3.Row) else row[2]
        update_source_category(
            conn,
            source,
            category,
            label,
            status="manual",
            reason="administrator edited",
        )
        promoted_categories += 1

    promoted_aliases = 0
    for row in alias_rows:
        alias = row["alias_source"] if isinstance(row, sqlite3.Row) else row[0]
        target = row["target_source"] if isinstance(row, sqlite3.Row) else row[1]
        target_exists = conn.execute(
            "SELECT 1 FROM source_categories WHERE source = ?",
            (target,),
        ).fetchone()
        if alias != target and target_exists:
            merge_source(conn, alias, target)
            promoted_aliases += 1

    conn.execute("DELETE FROM user_source_categories WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM user_source_aliases WHERE user_id = ?", (user_id,))
    conn.commit()
    return {"categories": promoted_categories, "aliases": promoted_aliases}


def merge_source(conn: sqlite3.Connection, source: str, target_source: str,
                 user_id: int | None = None) -> dict:
    """Merge source into target_source. User merges are private to that user."""
    if source == target_source:
        raise ValueError("cannot merge a source into itself")
    target = conn.execute(
        "SELECT * FROM source_categories WHERE source = ?",
        (target_source,),
    ).fetchone()
    if not target:
        raise ValueError("target source not found")

    if user_id is not None:
        conn.execute(
            """
            INSERT INTO user_source_aliases (user_id, alias_source, target_source)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id, alias_source) DO UPDATE SET
                target_source = excluded.target_source
            """,
            (user_id, source, target_source),
        )
        conn.commit()
        return dict(target)

    conn.execute(
        "UPDATE articles SET group_source = ?, source = ? "
        "WHERE COALESCE(NULLIF(group_source, ''), NULLIF(feed_source, ''), source) = ?",
        (target_source, target_source, source),
    )
    conn.execute(
        """
        INSERT INTO source_aliases (alias_source, target_source)
        VALUES (?, ?)
        ON CONFLICT(alias_source) DO UPDATE SET
            target_source = excluded.target_source
        """,
        (source, target_source),
    )
    conn.execute("DELETE FROM source_categories WHERE source = ?", (source,))
    conn.commit()
    return dict(target)


def source_rows(conn: sqlite3.Connection) -> list[dict]:
    """Read-only snapshot of source metadata.

    Deliberately does no bookkeeping: the full-table discover/cleanup passes live in
    maintain_source_categories() and run after a fetch cycle. Keeping them here made
    every GET /sources scan the articles table, which timed out the frontend's cold
    start whenever a fetch cycle held the write lock. Sources discovered since the
    last maintenance pass still show up via the `unlinked` query below.
    """
    _ensure_source_tables(conn)
    rows = conn.execute(
        """
        WITH source_counts AS (
            SELECT group_source AS source,
                   COUNT(*) AS article_count,
                   MAX(timestamp) AS latest_timestamp
            FROM articles
            WHERE group_source IS NOT NULL AND group_source != ''
            GROUP BY group_source

            UNION ALL

            SELECT source,
                   COUNT(*) AS article_count,
                   MAX(timestamp) AS latest_timestamp
            FROM articles
            WHERE (group_source IS NULL OR group_source = '')
              AND source IS NOT NULL
              AND TRIM(source) != ''
            GROUP BY source
        ),
        source_stats AS (
            SELECT source,
                   SUM(article_count) AS article_count,
                   MAX(latest_timestamp) AS latest_timestamp
            FROM source_counts
            WHERE source IS NOT NULL AND TRIM(source) != ''
            GROUP BY source
        )
        SELECT sc.source, sc.category, sc.label, sc.status, sc.confidence,
               sc.reason, sc.sample_titles, sc.updated_at,
               sc.suggested_category, sc.suggested_label, sc.suggested_confidence,
               COALESCE(stats.article_count, 0) AS article_count,
               stats.latest_timestamp,
               0 AS is_unlinked
        FROM source_categories sc
        LEFT JOIN source_stats stats ON stats.source = sc.source

        UNION ALL

        SELECT stats.source, ?, NULL, 'pending', NULL,
               'unlinked', NULL, NULL, NULL, NULL, NULL,
               stats.article_count, stats.latest_timestamp,
               1 AS is_unlinked
        FROM source_stats stats
        LEFT JOIN source_categories sc ON sc.source = stats.source
        WHERE sc.source IS NULL
        """,
        (UNCATEGORIZED,),
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        is_unlinked = bool(item.pop("is_unlinked"))
        if is_unlinked:
            item["label"] = local_short_source_name(item["source"])
        result.append(item)
    result.sort(key=lambda r: (-(r.get("article_count") or 0), (r.get("source") or "").lower()))
    return result


def effective_source_rows(conn: sqlite3.Connection, user_id: int | None = None) -> list[dict]:
    """Return shared source rows overlaid with a user's manual categories/aliases."""
    rows = source_rows(conn)
    if user_id is None:
        return rows

    by_source = {row["source"]: dict(row) for row in rows}
    overrides = conn.execute(
        "SELECT source, category, label, status, updated_at FROM user_source_categories WHERE user_id = ?",
        (user_id,),
    ).fetchall()
    for row in overrides:
        source = row["source"]
        base = by_source.get(source, {
            "source": source,
            "article_count": 0,
            "latest_timestamp": None,
            "confidence": None,
            "reason": "",
            "sample_titles": None,
        })
        base.update({
            "category": row["category"],
            "label": row["label"],
            "status": row["status"],
            "updated_at": row["updated_at"],
            "user_override": True,
        })
        by_source[source] = base

    aliases = conn.execute(
        "SELECT alias_source, target_source FROM user_source_aliases WHERE user_id = ?",
        (user_id,),
    ).fetchall()
    for row in aliases:
        alias = row["alias_source"]
        target = row["target_source"]
        target_row = by_source.get(target)
        alias_row = by_source.get(alias)
        if not target_row:
            continue
        if alias_row:
            target_row["article_count"] = (target_row.get("article_count") or 0) + (alias_row.get("article_count") or 0)
            by_source[alias] = {
                **alias_row,
                "category": target_row.get("category", UNCATEGORIZED),
                "label": target_row.get("label") or target,
                "status": "manual",
                "alias_target": target,
                "user_override": True,
            }
        target_row["has_aliases"] = True

    return sorted(by_source.values(), key=lambda row: (-(row.get("article_count") or 0), row.get("source") or ""))


def source_aliases_for_target(
    conn: sqlite3.Connection,
    target_source: str,
    user_id: int | None = None,
) -> list[str]:
    if user_id is None:
        rows = conn.execute(
            "SELECT alias_source FROM source_aliases WHERE target_source = ?",
            (target_source,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT alias_source FROM user_source_aliases WHERE user_id = ? AND target_source = ?",
            (user_id, target_source),
        ).fetchall()
    return [row["alias_source"] if isinstance(row, sqlite3.Row) else row[0] for row in rows]


def recent_titles_for_source(conn: sqlite3.Connection, source: str, limit: int = 8) -> list[str]:
    rows = conn.execute(
        """
        SELECT title FROM articles
        WHERE COALESCE(NULLIF(group_source, ''), NULLIF(feed_source, ''), source) = ?
          AND title IS NOT NULL AND TRIM(title) != ''
        ORDER BY timestamp DESC
        LIMIT ?
        """,
        (source, limit),
    ).fetchall()
    return [row["title"] if isinstance(row, sqlite3.Row) else row[0] for row in rows]


def update_source_category(
    conn: sqlite3.Connection,
    source: str,
    category: str,
    label: str,
    status: str = "manual",
    confidence: float | None = None,
    reason: str | None = None,
    sample_titles: list[str] | None = None,
    user_id: int | None = None,
) -> dict:
    if not valid_category(conn, category):
        raise ValueError("invalid category")
    if status not in VALID_STATUSES:
        raise ValueError("invalid status")
    label = clamp_weighted(label or local_short_source_name(source), 20)
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    if user_id is not None:
        conn.execute(
            """
            INSERT INTO user_source_categories
                (user_id, source, category, label, status, updated_at)
            VALUES (?, ?, ?, ?, 'manual', ?)
            ON CONFLICT(user_id, source) DO UPDATE SET
                category = excluded.category,
                label = excluded.label,
                status = excluded.status,
                updated_at = excluded.updated_at
            """,
            (user_id, source, category, label, now),
        )
        conn.commit()
        row = conn.execute(
            "SELECT source, category, label, status, updated_at FROM user_source_categories WHERE user_id = ? AND source = ?",
            (user_id, source),
        ).fetchone()
        return dict(row)

    conn.execute(
        """
        INSERT INTO source_categories
            (source, category, label, status, confidence, reason, sample_titles, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source) DO UPDATE SET
            category = excluded.category,
            label = excluded.label,
            status = excluded.status,
            confidence = excluded.confidence,
            reason = excluded.reason,
            sample_titles = excluded.sample_titles,
            suggested_category = NULL,
            suggested_label = NULL,
            suggested_confidence = NULL,
            updated_at = excluded.updated_at
        """,
        (
            source,
            category,
            label,
            status,
            confidence,
            reason,
            json.dumps(sample_titles or [], ensure_ascii=False),
            now,
        ),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM source_categories WHERE source = ?", (source,)).fetchone()
    return dict(row)
