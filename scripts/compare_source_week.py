#!/usr/bin/env python3
"""Read-only replay of the latest seven days against existing source labels."""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fetcher import classified_source_history, detect_group_source, source_from_classified_history


def compare(db_path: Path) -> dict:
    conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        latest = conn.execute("SELECT MAX(timestamp) FROM articles").fetchone()[0]
        if latest is None:
            return {"error": "no articles"}
        start = int(latest) - 7 * 86400
        history = classified_source_history(conn, before_timestamp=start)
        rows = conn.execute(
            "SELECT a.id, a.title, a.group_source, a.source, a.feed_source, "
            "a.body_html, a.telegraph_url, a.timestamp "
            "FROM articles a JOIN source_categories sc "
            "ON sc.source = COALESCE(NULLIF(a.group_source, ''), a.source) "
            "WHERE sc.status IN ('manual', 'classified') "
            "AND a.timestamp >= ? AND a.timestamp <= ? "
            "ORDER BY a.timestamp, a.id", (start, latest)
        ).fetchall()
        result = {"start": start, "end": latest, "total": len(rows),
                  "matched": 0, "mismatched": 0, "no_evidence": 0,
                  "mismatches": []}
        for row in rows:
            content = row["body_html"] or ""
            prediction = source_from_classified_history(content, history)
            if not prediction:
                group, domain = detect_group_source(
                    content, row["telegraph_url"] or "", row["feed_source"] or ""
                )
                if domain or (group and group != (row["feed_source"] or "")):
                    prediction = (group, domain)
            if not prediction:
                result["no_evidence"] += 1
                continue
            actual = row["group_source"] or row["source"]
            if prediction[0] == actual:
                result["matched"] += 1
            else:
                result["mismatched"] += 1
                result["mismatches"].append({
                    "id": row["id"], "title": row["title"],
                    "previous": actual, "predicted": prediction[0],
                })
        return result
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("news_db", type=Path)
    args = parser.parse_args()
    print(json.dumps(compare(args.news_db), ensure_ascii=False, indent=2))
