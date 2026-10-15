"""Synthetic, read-only query comparison; never opens a deployment database.

Run: python scripts/benchmark_source_cleanup.py
"""
import ast
import inspect
import json
from pathlib import Path
import sqlite3
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import source_categories

OLD_QUERY = """
SELECT sc.source, sc.status, COUNT(a.id) AS article_count
FROM source_categories sc
LEFT JOIN articles a ON COALESCE(NULLIF(a.group_source, ''), NULLIF(a.feed_source, ''), a.source) = sc.source
GROUP BY sc.source HAVING article_count = 0
"""


def main():
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE articles(id INTEGER PRIMARY KEY,group_source TEXT,feed_source TEXT,source TEXT,body_html TEXT)')
    conn.execute('CREATE INDEX idx_group_source ON articles(group_source)')
    conn.execute('CREATE INDEX idx_feed_source ON articles(feed_source)')
    conn.execute('CREATE INDEX idx_source ON articles(source)')
    conn.execute('CREATE TABLE source_categories(source TEXT PRIMARY KEY,status TEXT)')
    conn.executemany('INSERT INTO source_categories VALUES(?,?)', [(f'source-{i}', 'pending') for i in range(68)])
    conn.executemany('INSERT INTO articles VALUES(?,?,?,?,?)', (
        (i, '' if i % 3 else f'source-{i%60}', f'source-{i%60}', f'source-{i%60}', 'synthetic text ' * 80)
        for i in range(1,85648)))
    conn.commit()
    tree = ast.parse(inspect.getsource(source_categories.cleanup_stale_source_categories))
    new_query = next(node.value for node in ast.walk(tree) if isinstance(node,ast.Constant)
                     and isinstance(node.value,str) and 'WITH effective_sources AS MATERIALIZED' in node.value)
    output = {'articles':85647,'sources':68,'body_bytes_per_row':len('synthetic text '*80),'sqlite_version':sqlite3.sqlite_version,'runs':3}
    results = {}
    for label,query in [('old',OLD_QUERY),('new',new_query)]:
        durations = []
        for _ in range(3):
            start = time.perf_counter()
            results[label] = sorted(conn.execute(query).fetchall())
            durations.append(round((time.perf_counter()-start)*1000,3))
        output[label] = {'duration_ms':durations,'median_ms':statistics.median(durations),
                         'plan':[row[3] for row in conn.execute('EXPLAIN QUERY PLAN '+query)]}
    output['equivalent'] = results['old'] == results['new']
    assert output['equivalent']
    print(json.dumps(output,ensure_ascii=False,indent=2))
    conn.close()


if __name__ == '__main__':
    main()
