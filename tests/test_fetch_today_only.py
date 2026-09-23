"""Bootstrap fetches today's messages; later incremental runs retain late arrivals."""
from datetime import datetime, timedelta, timezone

import fetcher


def _iso(dt_cst):
    return dt_cst.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _msg(id_, dt_cst):
    return {"id": id_, "datetime": _iso(dt_cst)}


def _now_cst():
    return datetime.now(fetcher.CST)


def _today_at(hour):
    now = _now_cst()
    return now.replace(hour=hour, minute=0, second=0, microsecond=0)


def _yesterday_at(hour):
    return _today_at(hour) - timedelta(days=1)


def _mock_pages(monkeypatch, pages_by_before):
    """pages_by_before: dict mapping the `before` argument fetch_telegram_page() is
    called with -> list of msg dicts (oldest-first) for that page. The first page is
    keyed by "".
    """
    calls = []

    def fake_fetch_telegram_page(before=""):
        calls.append(before)
        return "html" if before in pages_by_before else None

    def fake_parse_messages(html):
        before = calls[-1]
        return pages_by_before.get(before, [])

    monkeypatch.setattr(fetcher, "fetch_telegram_page", fake_fetch_telegram_page)
    monkeypatch.setattr(fetcher, "parse_messages", fake_parse_messages)
    return calls


def test_incremental_run_keeps_pre_today_messages_on_a_mixed_page(monkeypatch):
    # Oldest-first page: id=105 is yesterday, 106/107 are today.
    page1 = [_msg(105, _yesterday_at(20)), _msg(106, _today_at(8)), _msg(107, _today_at(9))]
    # Next page (before=105) is entirely yesterday's — newest on it predates today.
    page2 = [_msg(102, _yesterday_at(10)), _msg(103, _yesterday_at(11)), _msg(104, _yesterday_at(12))]
    calls = _mock_pages(monkeypatch, {"": page1, "105": page2})

    result, highest_observed_id = fetcher.fetch_all_new_messages({"last_seen_id": 100})

    assert sorted(m["id"] for m in result) == [102, 103, 104, 105, 106, 107]
    assert highest_observed_id == 107
    # Continues until the cursor is reached, even across a date boundary.
    assert calls == ["", "105", "102"]


def test_incremental_run_stops_immediately_once_caught_up(monkeypatch):
    page1 = [_msg(198, _today_at(6)), _msg(199, _today_at(7)), _msg(200, _today_at(8))]
    calls = _mock_pages(monkeypatch, {"": page1, "198": [_msg(1, _yesterday_at(1))] * 3})

    result, highest_observed_id = fetcher.fetch_all_new_messages({"last_seen_id": 200})

    assert result == []
    assert highest_observed_id == 200  # nothing new beyond the cursor — unchanged
    assert calls == [""]  # never requests a second page once caught up


def test_first_ever_run_still_scopes_to_today_only(monkeypatch):
    # last_seen_id=0 (never fetched before) must behave the same as any other run.
    page1 = [_msg(9, _yesterday_at(23)), _msg(10, _today_at(0)), _msg(11, _today_at(1))]
    # Newest on page (id 11) is still today, so it pages once more (anchor=9); that
    # next page isn't registered, so fetch_telegram_page returns None and the loop
    # exits via the "no html" branch.
    calls = _mock_pages(monkeypatch, {"": page1})

    result, highest_observed_id = fetcher.fetch_all_new_messages({"last_seen_id": 0})

    assert [m["id"] for m in result] == [10, 11]
    assert highest_observed_id == 11
    assert calls == ["", "9"]


def test_same_page_new_and_duplicate_ids_are_not_double_counted(monkeypatch):
    # A page can include ids already collected from a previous page (overlap); they
    # must not be appended twice.
    page1 = [_msg(20, _today_at(8)), _msg(21, _today_at(9)), _msg(22, _today_at(10))]
    page2 = [_msg(21, _today_at(9)), _msg(22, _today_at(10)), _msg(23, _today_at(11))]
    calls = _mock_pages(monkeypatch, {"": page1, "20": page2, "21": []})

    result, highest_observed_id = fetcher.fetch_all_new_messages({"last_seen_id": 0})

    ids = [m["id"] for m in result]
    assert ids == sorted(set(ids))  # no duplicates
    assert 20 in ids and 23 in ids
    assert highest_observed_id == 23


# ─── P2 fix: cursor still advances past backlog that's entirely discarded ──

def test_only_pre_today_backlog_is_kept_for_next_digest(monkeypatch):
    page1 = [_msg(101, _yesterday_at(20)), _msg(102, _yesterday_at(21)), _msg(103, _yesterday_at(22))]
    _mock_pages(monkeypatch, {"": page1})

    result, highest_observed_id = fetcher.fetch_all_new_messages({"last_seen_id": 100})

    assert [m["id"] for m in result] == [101, 102, 103]
    assert highest_observed_id == 103


def test_run_advances_cursor_past_backlog_that_is_entirely_discarded(tmp_path, monkeypatch):
    monkeypatch.setattr(fetcher, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(fetcher, "OUTPUT_FILE", tmp_path / "news.json")
    monkeypatch.setattr(fetcher, "STATE_FILE", tmp_path / "fetcher_state.json")
    monkeypatch.setattr(fetcher, "DB_FILE", tmp_path / "news.db")
    monkeypatch.setattr(fetcher, "PROGRESS_FILE", tmp_path / "fetch_progress.json")
    fetcher.save_state({"last_seen_id": 100})

    page1 = [_msg(101, _yesterday_at(20)), _msg(102, _yesterday_at(21)), _msg(103, _yesterday_at(22))]
    calls = _mock_pages(monkeypatch, {"": page1})

    fetcher.run()

    state = fetcher.load_state()
    assert state["last_seen_id"] == 103

    # A second run must not re-fetch the same discarded backlog: the cursor already
    # covers ids up to 103, so the page's messages are all <= last_id and the
    # "caught up" stop condition fires after a single page request.
    fetcher.run()
    assert calls == ["", ""]


def test_run_advances_last_seen_id_to_the_highest_kept_today_message(tmp_path, monkeypatch):
    monkeypatch.setattr(fetcher, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(fetcher, "OUTPUT_FILE", tmp_path / "news.json")
    monkeypatch.setattr(fetcher, "STATE_FILE", tmp_path / "fetcher_state.json")
    monkeypatch.setattr(fetcher, "DB_FILE", tmp_path / "news.db")
    monkeypatch.setattr(fetcher, "PROGRESS_FILE", tmp_path / "fetch_progress.json")
    fetcher.save_state({"last_seen_id": 100})

    # Page mixes a stale (yesterday) high id with today's ids — the discarded
    # yesterday id must not influence the new cursor.
    page1 = [_msg(105, _yesterday_at(20)), _msg(106, _today_at(8)), _msg(107, _today_at(9))]
    _mock_pages(monkeypatch, {"": page1})
    monkeypatch.setattr(
        fetcher, "process_message",
        lambda msg, orig_id: {
            "id": orig_id, "title": f"t{orig_id}", "source": "s",
            "feed_source": "s", "timestamp": orig_id,
        },
    )

    fetcher.run()

    state = fetcher.load_state()
    assert state["last_seen_id"] == 107
