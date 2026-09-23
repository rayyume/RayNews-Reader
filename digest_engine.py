"""Event-level ranking for the shared daily digest.

Every eligible article enters grouping. AI-derived signals improve event identity
and impact estimates, but a missing signal never removes an article from the run.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher


SIGNAL_VERSION = 1
MIN_EVENT_SCORE = 55
MAX_DIGEST_EVENTS = 60
RELATIVE_EVENT_FLOOR = 30


def _plain(value: object, limit: int = 160) -> str:
    text = re.sub(r"<[^>]*>", " ", str(value or ""))
    return " ".join(text.split())[:limit]


def _key(value: object) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]", "", _plain(value).lower())


def _bounded(value: object, maximum: int, default: int = 0) -> int:
    try:
        return max(0, min(maximum, int(value)))
    except (TypeError, ValueError):
        return default


def normalize_signals(article: dict, raw: dict | None) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    entities = raw.get("entities") if isinstance(raw.get("entities"), list) else []
    topics = raw.get("topics") if isinstance(raw.get("topics"), list) else []
    return {
        "event": _plain(raw.get("event") or article.get("title"), 100),
        "entities": [_plain(item, 40) for item in entities[:5] if _plain(item, 40)],
        "action": _plain(raw.get("action"), 55),
        "topics": [_plain(item, 30) for item in topics[:3] if _plain(item, 30)],
        "impact": _bounded(raw.get("impact"), 5, 2),
        "novelty": _bounded(raw.get("novelty"), 3, 1),
        "evidence": _bounded(raw.get("evidence"), 3, 1),
        "material_update": bool(raw.get("material_update")),
    }


def same_event(left: dict, right: dict) -> bool:
    left_event, right_event = _key(left.get("event")), _key(right.get("event"))
    if not left_event or not right_event:
        return False
    left_entities = {_key(item) for item in left.get("entities", []) if _key(item)}
    right_entities = {_key(item) for item in right.get("entities", []) if _key(item)}
    shared_entities = left_entities & right_entities
    if left_entities and right_entities and not shared_entities:
        return False
    if left_event == right_event:
        return len(left_event) >= 6 or bool(shared_entities)
    event_similarity = SequenceMatcher(None, left_event, right_event).ratio()
    action_similarity = SequenceMatcher(
        None, _key(left.get("action")), _key(right.get("action"))
    ).ratio()
    # A broad shared topic or publisher name alone cannot join unrelated events.
    return bool(shared_entities and (
        event_similarity >= 0.64 or
        (action_similarity >= 0.70 and event_similarity >= 0.45)
    )) or (event_similarity >= 0.88 and min(len(left_event), len(right_event)) >= 8)


def group_events(articles: list[dict]) -> list[dict]:
    groups: list[dict] = []
    for article in articles:
        signal = article["digest_signals"]
        group = next((item for item in groups if same_event(item["signal"], signal)), None)
        if group is None:
            group = {"signal": signal, "articles": []}
            groups.append(group)
        group["articles"].append(article)
        # Prefer the article with the clearest facts as the representative.
        representative = max(
            group["articles"],
            key=lambda item: (
                item["digest_signals"]["evidence"],
                bool(item.get("summary")),
                int(item.get("ingested_at") or 0),
            ),
        )
        group["representative"] = representative
        group["signal"] = representative["digest_signals"]
    return groups


def rank_events(groups: list[dict], previous: list[dict], *, cutoff: int,
                max_events: int = MAX_DIGEST_EVENTS, min_score: int = MIN_EVENT_SCORE,
                min_events: int = 0) -> list[dict]:
    for group in groups:
        signal = group["signal"].copy()
        for field in ("impact", "novelty", "evidence"):
            signal[field] = max(article["digest_signals"][field] for article in group["articles"])
        signal["material_update"] = any(
            article["digest_signals"]["material_update"] for article in group["articles"]
        )
        group["signal"] = signal
        publishers = {
            _plain(article.get("source"), 100)
            for article in group["articles"] if article.get("source")
        }
        first_seen = min(int(item.get("ingested_at") or cutoff) for item in group["articles"])
        age_hours = max(0, (cutoff - first_seen) / 3600)
        freshness = 5 if age_hours <= 6 else 3 if age_hours <= 24 else 0
        group["score"] = (
            signal["impact"] * 10
            + min(3, max(0, len(publishers) - 1)) * 6
            + signal["novelty"] * 5
            + signal["evidence"] * 4
            + freshness
        )
        group["publisher_count"] = len(publishers)
        group["reason"] = ""
        if any(same_event(signal, old) for old in previous) and not signal["material_update"]:
            group["reason"] = "already covered"
        elif group["score"] < min_score and not (
            signal["impact"] >= 4 and signal["evidence"] >= 2
        ):
            group["reason"] = "below importance threshold"
    eligible = sorted(
        (group for group in groups if not group["reason"]),
        key=lambda group: (
            -group["score"], -group["publisher_count"],
            -int(group["representative"].get("ingested_at") or 0),
        ),
    )
    selected = eligible[:max_events]
    for group in eligible[max_events:]:
        group["reason"] = "daily limit"
    # On a high-volume day, absolute scores can be conservative across the
    # board. Fill a small editorial floor from the strongest remaining events,
    # while still excluding yesterday's unchanged events and weak evidence.
    if len(selected) < min(min_events, max_events):
        relative = sorted(
            (group for group in groups
             if group["reason"] == "below importance threshold"
             and group["score"] >= RELATIVE_EVENT_FLOOR
             and any(not article.get("digest_signal_fallback")
                     for article in group["articles"])),
            key=lambda group: (
                -group["score"], -group["publisher_count"],
                -int(group["representative"].get("ingested_at") or 0),
            ),
        )
        for group in relative[:min(min_events, max_events) - len(selected)]:
            group["reason"] = ""
            group["selection_basis"] = "relative ranking"
            selected.append(group)
    return sorted(selected, key=lambda group: (
        -group["score"], -group["publisher_count"],
        -int(group["representative"].get("ingested_at") or 0),
    ))


def render_digest(events: list[dict], definitions: list[dict], written: dict[int, dict]) -> str:
    sections: dict[str, list[str]] = {}
    for event in events:
        article = event["representative"]
        category = article.get("category") or "Uncategorized"
        title = _plain(written.get(article["id"], {}).get("headline") or article.get("title"), 55)
        sentence = _plain(
            written.get(article["id"], {}).get("sentence")
            or article.get("summary") or article.get("title"), 125
        )
        title = re.sub(r"([\\*\[\]`])", r"\\\1", title)
        sentence = re.sub(r"([\\*\[\]`])", r"\\\1", sentence)
        link = article.get("url") or ""
        sections.setdefault(category, []).append(f"**{title}：** {sentence} [🔗]({link})")
    lines = []
    rendered_categories = set()
    for definition in definitions:
        entries = sections.get(definition["category"], [])
        if entries:
            lines.append(f"## {definition['label']}")
            lines.extend(f"{number}. {entry}" for number, entry in enumerate(entries, 1))
            rendered_categories.add(definition["category"])
    for category, entries in sections.items():
        if category in rendered_categories:
            continue
        lines.append(f"## {'待分类' if category == 'Uncategorized' else category}")
        lines.extend(f"{number}. {entry}" for number, entry in enumerate(entries, 1))
    return "\n".join(lines) if lines else "今日无高质量新闻可总结。"
