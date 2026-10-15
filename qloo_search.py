#!/usr/bin/env python3
"""Optional Qloo discovery/ranking through @qloo/qloo-harness 0.1.26.

Send only aggregate cuisine/vibe concepts and public venue names. The harness
resolves concepts to real tags; never invent IDs. Qloo supplies no phone numbers.
Unavailable/ambiguous workflows fall back to OSM/Exa. Credentials may be saved
by qloo setup or inherited through QLOO_API_KEY.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import unicodedata
from pathlib import Path

from envlite import env

TIMEOUT = 12


def _limit(value, default=10, maximum=20) -> int:
    try:
        return max(1, min(maximum, int(value)))
    except (TypeError, ValueError):
        return default


RECOMMEND_LIMIT = 15
RANK_LIMIT = 10


def _command(*args: str) -> list[str]:
    executable = shutil.which("qloo") or "qloo"
    # npm's Windows shim points here. Run Node directly, avoiding a shell.
    if os.name == "nt" and executable.lower().endswith((".cmd", ".bat")):
        entry = Path(executable).parent / "node_modules/@qloo/qloo-harness/dist/bin.js"
        if not entry.is_file():
            raise FileNotFoundError("Qloo npm entry point is missing; reinstall the harness")
        return [shutil.which("node") or "node", str(entry), *args]
    return [executable, *args]


def harness_available() -> bool:
    try:
        result = subprocess.run(_command("--version"), capture_output=True,
                                text=True, encoding="utf-8", timeout=5)
        return result.returncode == 0 and bool(result.stdout.strip())
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        return False


def readiness() -> bool:
    """Read saved/env credential readiness without a Qloo API request."""
    try:
        result = subprocess.run(_command("setup", "--status", "--json"),
                                capture_output=True, text=True, encoding="utf-8", timeout=5)
        report = json.loads(result.stdout)
        return result.returncode == 0 and report.get("qloo", {}).get("ready") is True
    except (OSError, subprocess.TimeoutExpired, ValueError, AttributeError):
        return False


def _exec(workflow: str, payload: dict) -> list | dict:
    """Pipe JSON; let the harness read its saved or env credential."""
    try:
        result = subprocess.run(
            _command("exec", workflow), input=json.dumps(payload),
            capture_output=True, text=True, encoding="utf-8", timeout=TIMEOUT,
            env=os.environ.copy(),
        )
        if result.returncode != 0:
            # stdout/stderr may contain credentials or data; do not echo them.
            print(f"[qloo] {workflow} failed (exit {result.returncode}); using fallback")
            return []
        parsed = json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError, UnicodeError):
        print(f"[qloo] {workflow} unavailable; using fallback")
        return []
    if not isinstance(parsed, (list, dict)):
        return []
    if isinstance(parsed, dict) and (parsed.get("error") or
            parsed.get("status", "ok") not in ("ok", "partial")):
        print(f"[qloo] {workflow} returned no usable result; using fallback")
        return []
    return parsed


def _items(result, *keys) -> list:
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        if result.get("error") or result.get("status", "ok") not in ("ok", "partial"):
            return []
        for key in ("results", *keys):
            if isinstance(result.get(key), list):
                return result[key]
    return []


def find_tags(concept: str, limit: int = 5) -> list[dict]:
    """Return tag candidates for inspection, rather than auto-selecting a match."""
    if not concept or not concept.strip():
        return []
    result = _exec("find_tags", {"query": concept.strip(), "limit": _limit(limit, 5)})
    return [item for item in _items(result) if isinstance(item, dict) and item.get("id")]


def _concepts(values) -> list[str]:
    if not isinstance(values, list):
        return []
    return list(dict.fromkeys(value.strip()[:120] for value in values
                             if isinstance(value, str) and value.strip()))[:10]


def _taste_tags(constraints: dict) -> list[str]:
    soft = constraints.get("soft") or []
    phrases = [item.get("constraint") for item in soft if isinstance(item, dict)]
    return _concepts(_concepts(constraints.get("prefer_cuisines")) + _concepts(phrases))


def recommend(constraints: dict, limit: int | None = None) -> list[dict]:
    tags = _taste_tags(constraints)
    if not tags:
        return []
    # Read limits after bot.main has loaded .env, rather than at import time.
    requested = env("QLOO_RECOMMEND_LIMIT") if limit is None else limit
    payload = {"target_type": "place", "signal_tags": tags,
               "filter_location": "Hong Kong", "limit": _limit(requested, RECOMMEND_LIMIT)}
    cuisines = _concepts(constraints.get("prefer_cuisines"))
    excluded = _concepts(constraints.get("avoid_cuisines"))
    if cuisines:
        payload["include_tags"] = cuisines
    if excluded:
        payload["exclude_tags"] = excluded
    return _rows_from_result(_exec("recommend", payload), "qloo_recommend")


def rank(candidates: list[dict], constraints: dict, limit: int | None = None) -> list[dict]:
    """Rank up to ten options by HK audience affinity with cuisine filters.

    Rank does not accept taste-tag signals. Cuisine concepts are include/exclude
    filters; Hong Kong is the aggregate audience signal. Preserve every candidate,
    including duplicate names at different branches.
    """
    if not candidates or not _taste_tags(constraints):
        return candidates
    options = list(dict.fromkeys(c.get("name") for c in candidates if c.get("name")))
    requested = env("QLOO_RANK_LIMIT") if limit is None else limit
    options = options[:_limit(requested, RANK_LIMIT, 10)]
    if not options:
        return candidates
    payload = {"options": options, "option_type": "place", "signal_location": "Hong Kong"}
    cuisines = _concepts(constraints.get("prefer_cuisines"))
    excluded = _concepts(constraints.get("avoid_cuisines"))
    if cuisines:
        payload["include_tags"] = cuisines
    if excluded:
        payload["exclude_tags"] = excluded
    ordered = _items(_exec("rank", payload))
    if not ordered:
        return candidates
    selected = set(options)
    used: set[int] = set()
    reordered = []
    for item in ordered:
        name = _name_from_item(item)
        entity_id = item.get("entity_id") if isinstance(item, dict) else None
        for index, candidate in enumerate(candidates):
            if index in used or candidate.get("name") not in selected:
                continue
            same_id = entity_id and entity_id == candidate.get("qloo_entity_id")
            if same_id or (name and _norm(name) == _norm(candidate.get("name", ""))):
                reordered.append(candidate)
                used.add(index)
    return reordered + [c for index, c in enumerate(candidates) if index not in used]


def where_popular(entity: str, within: str = "Hong Kong", limit: int = 10) -> list[dict]:
    """Exploratory heatmap helper, outside the bot's meeting-point selection.

    Return heatmap cells intact, not invented district names. The input must be
    a public entity name/UUID rather than an invented cuisine entity.
    """
    if not entity or not within:
        return []
    result = _exec("where_popular", {"entity": entity, "within": within, "limit": _limit(limit)})
    return [item for item in _items(result) if isinstance(item, dict)]


def _name_from_item(item) -> str:
    if isinstance(item, str):
        return item.strip()[:200]
    if isinstance(item, dict):
        name = item.get("name") or item.get("restaurant_name")
        return name.strip()[:200] if isinstance(name, str) else ""
    return ""


def _rows_from_result(result, source: str) -> list[dict]:
    rows = []
    seen = set()
    for item in _items(result, "recommendations"):
        name = _name_from_item(item)
        key = _norm(name)
        if not key or key in seen:
            continue
        seen.add(key)
        detail = item if isinstance(item, dict) else {}
        rows.append({"name": name, "phone": None,
                     "area": str(detail.get("neighbourhood") or detail.get("area") or "")[:40],
                     "cuisine": str(detail.get("cuisine") or "")[:60],
                     "why": str(detail.get("why_relevant") or detail.get("reason") or "")[:160],
                     "qloo_entity_id": detail.get("entity_id"), "source": source})
    return rows


def merge(osm_rows: list[dict], qloo_rows: list[dict]) -> list[dict]:
    """Join exact normalized names; never infer a phone via a fuzzy branch match."""
    merged = [dict(row) for row in osm_rows]
    for row in qloo_rows:
        key = _norm(row.get("name", ""))
        if not key:
            continue
        matches = [held for held in merged if _norm(held.get("name", "")) == key]
        if matches:
            for held in matches:
                for field in ("cuisine", "area", "why", "qloo_entity_id"):
                    if not held.get(field) and row.get(field):
                        held[field] = row[field]
                held["also_found_by"] = "qloo"
        else:
            merged.append({**row, "phone": None})
    return sorted(merged, key=lambda r: (bool(r.get("phone")), bool(r.get("cuisine"))), reverse=True)


def _norm(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKC", text).casefold() if c.isalnum())
