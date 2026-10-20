"""Transactional invalidation versions for shared AI results (no content history)."""
from functools import wraps
import threading

_active_lock = threading.Lock()
_active = {}


def track_generation(kind):
    def decorate(function):
        @wraps(function)
        def run(article, *args, **kwargs):
            article_id = article['id'] if isinstance(article, dict) else article
            key = (int(article_id), kind)
            with _active_lock:
                _active[key] = _active.get(key, 0) + 1
            try:
                return function(article, *args, **kwargs)
            finally:
                with _active_lock:
                    count = _active[key] - 1
                    if count:
                        _active[key] = count
                    else:
                        _active.pop(key, None)
        return run
    return decorate


def result_state(article_id, kind, result):
    if result.get(kind):
        return 'available'
    with _active_lock:
        if _active.get((article_id, kind)):
            return 'generating'
    if result.get(kind + '_error'):
        return 'failed'
    return 'absent'


def ensure_result_versions(conn):
    """Install in the caller's schema transaction; triggers cover every writer."""
    conn.execute('CREATE TABLE IF NOT EXISTS ai_result_version_state ('
                 'id INTEGER PRIMARY KEY CHECK(id=1), epoch TEXT NOT NULL, sequence INTEGER NOT NULL DEFAULT 0)')
    conn.execute("INSERT OR IGNORE INTO ai_result_version_state(id,epoch) VALUES(1,lower(hex(randomblob(16))))")
    conn.execute('CREATE TABLE IF NOT EXISTS ai_result_versions ('
                 'article_id INTEGER PRIMARY KEY, version INTEGER NOT NULL UNIQUE)')
    fields = ('summary', 'translation', 'summary_error', 'summary_error_at', 'title_summary',
              'title_summary_error', 'translation_updated_at', 'title_translation', 'translated_title_summary',
              'title_translation_error', 'title_translation_error_at', 'title_summary_error_at',
              'translated_title_summary_error', 'translated_title_summary_error_at')
    columns = {row[1] for row in conn.execute("PRAGMA table_info(ai_results)")}
    fields = tuple(field for field in fields if field in columns)
    for event, reference, condition in (
        ('INSERT', 'NEW', ''),
        ('UPDATE', 'NEW', 'WHEN ' + ' OR '.join(f'OLD.{field} IS NOT NEW.{field}' for field in fields)),
        ('DELETE', 'OLD', ''),
    ):
        # No timestamp comparisons: writes in the same millisecond, including
        # two writes of the same article, receive distinct committed versions.
        if event == 'UPDATE':
            existing = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='ai_result_version_UPDATE'"
            ).fetchone()
            if existing and any(f'OLD.{field}' not in existing[0] for field in fields):
                conn.execute('DROP TRIGGER ai_result_version_UPDATE')
        conn.execute(f'''CREATE TRIGGER IF NOT EXISTS ai_result_version_{event}
            AFTER {event} ON ai_results {condition}
            BEGIN
                UPDATE ai_result_version_state SET sequence=sequence+1 WHERE id=1;
                INSERT INTO ai_result_versions(article_id,version)
                SELECT {reference}.article_id, sequence FROM ai_result_version_state WHERE id=1
                ON CONFLICT(article_id) DO UPDATE SET version=excluded.version;
            END''')


def read_updates(conn, cursor, limit=500):
    # Baseline and rows must see the same snapshot under concurrent writers.
    conn.execute('BEGIN')
    epoch, head = conn.execute('SELECT epoch,sequence FROM ai_result_version_state WHERE id=1').fetchone()
    if not cursor:
        return {'items': [], 'cursor': f'{epoch}|{head}', 'has_more': False}
    try:
        cursor_epoch, raw_sequence = cursor.split('|')
        sequence = int(raw_sequence)
        if sequence < 0 or str(sequence) != raw_sequence:
            raise ValueError
    except (ValueError, AttributeError):
        raise ValueError('invalid cursor') from None
    if cursor_epoch != epoch or sequence > head:
        return {'items': [], 'cursor': f'{epoch}|{head}', 'has_more': False, 'reset': True}
    rows = conn.execute('SELECT article_id,version FROM ai_result_versions '
                        'WHERE version>? ORDER BY version LIMIT ?', (sequence, limit + 1)).fetchall()
    items = [{'id': row[0], 'version': row[1]} for row in rows[:limit]]
    return {'items': items, 'cursor': f"{epoch}|{items[-1]['version'] if items else head}",
            'has_more': len(rows) > limit}
