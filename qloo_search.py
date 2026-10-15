#!/usr/bin/env python3
"""qloo_search.py — taste-intelligence candidate discovery via the Qloo harness.

Qloo knows that fans of certain cuisines overlap with certain audiences. That is
a genuinely different signal from "restaurant is near the district someone named"
(OSM) or "a web page exists for this place" (Exa). It surfaces candidates that
fit the group's cultural taste profile, not just their geography.

Two rules that mirror exa_search.py:

1. THIS IS AN ENHANCEMENT, NEVER A DEPENDENCY. Every failure path returns [].
   No key, no harness installed, dead network, malformed JSON: []. The
   candidate list must survive this module being completely absent.

2. IT NEVER CLAIMS A PHONE NUMBER. Qloo finds names; OSM makes them callable.
   Results are merged on normalised name and the phone field is populated only
   from OSM data.

Integration method: `qloo exec <workflow>` is a local CLI command provided by
the @qloo/qloo-harness npm package. We call it as a subprocess so that:
  - no Python pip dependency is needed (standard library only on our side)
  - the Qloo API key lives inside the harness's private config or QLOO_API_KEY
    env var, never in our .env or source code
  - the exact same fallback pattern as Exa applies: any subprocess error -> []

Credential: set via `qloo setup --qloo` (harness stores it privately) OR via
the QLOO_API_KEY environment variable on the server. Never commit a key.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from envlite import env

# Timeout for a single qloo exec call. Keep it short: /decide is watched
# by six impatient people and this is an enhancement, not the critical path.
TIMEOUT = 12

# Maximum candidates to request from each Qloo workflow.
# Qloo recommends up to 20 results; we keep it lower by default to stay
# well inside the free tier and to keep latency low.
RECOMMEND_LIMIT = int(env("QLOO_RECOMMEND_LIMIT") or 15)
RANK_LIMIT = int(env("QLOO_RANK_LIMIT") or 10)


# ---------------------------------------------------------------------------
# Harness availability
# ---------------------------------------------------------------------------

def harness_available() -> bool:
    """True when `qloo` is on PATH and returns a usable version string."""
    try:
        result = subprocess.run(
            ["qloo", "--version"],
            capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0 and bool(result.stdout.strip())
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return False


# ---------------------------------------------------------------------------
# Low-level harness call
# ---------------------------------------------------------------------------

def _exec(workflow: str, payload: dict) -> list | dict:
    """Call `qloo exec <workflow>` with JSON payload on stdin.

    Returns the parsed JSON result (list or dict), or [] on any failure.
    The QLOO_API_KEY env var is forwarded to the subprocess so that server
    deployments can inject the credential without touching the harness config.
    """
    key = env("QLOO_API_KEY")
    if not key:
        print("[qloo] no QLOO_API_KEY — skipping Qloo (this is fine)")
        return []

    inp = json.dumps(payload)
    cmd = ["qloo", "exec", workflow]
    extra_env = {**os.environ, "QLOO_API_KEY": key}

    try:
        result = subprocess.run(
            cmd,
            input=inp,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            env=extra_env,
        )
    except FileNotFoundError:
        print("[qloo] harness not installed (npm install --global @qloo/qloo-harness)")
        return []
    except subprocess.TimeoutExpired:
        print(f"[qloo] {workflow} timed out after {TIMEOUT}s")
        return []
    except OSError as exc:
        print(f"[qloo] {workflow} subprocess error: {exc}")
        return []

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()[:200]
        print(f"[qloo] {workflow} exit {result.returncode}: {stderr or '(no stderr)'}")
        return []

    stdout = (result.stdout or "").strip()
    if not stdout:
        print(f"[qloo] {workflow} returned empty output")
        return []

    try:
        parsed = json.loads(stdout)
        return parsed
    except json.JSONDecodeError as exc:
        print(f"[qloo] {workflow} invalid JSON: {exc}")
        return []


# ---------------------------------------------------------------------------
# Workflow wrappers
# ---------------------------------------------------------------------------

def find_tags(concept: str, limit: int = 5) -> list[str]:
    """Resolve a free-text concept to Qloo tag IDs.

    Example: "cosy and lively" -> ["ambience:lively", "crowd:social"]

    Returns [] if the harness is unavailable or returns nothing useful.
    The caller is not expected to handle errors.
    """
    if not concept or not concept.strip():
        return []

    result = _exec("find_tags", {"concept": concept.strip(), "limit": limit})

    tags: list[str] = []
    if isinstance(result, list):
        for item in result:
            tag = _extract_tag_id(item)
            if tag and tag not in tags:
                tags.append(tag)
    elif isinstance(result, dict):
        for item in result.get("tags") or result.get("results") or []:
            tag = _extract_tag_id(item)
            if tag and tag not in tags:
                tags.append(tag)

    if tags:
        print(f"[qloo] find_tags({concept!r:.40}) -> {tags}")
    return tags


def _extract_tag_id(item) -> str:
    """Pull a tag identifier out of whatever shape the harness returns."""
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        return str(
            item.get("id") or item.get("tag_id") or item.get("name") or ""
        ).strip()
    return ""


def recommend(constraints: dict, limit: int = RECOMMEND_LIMIT) -> list[dict]:
    """Discover candidate restaurants from Qloo taste signals.

    Builds a structured request from the group's cuisine preferences, vibe
    words (soft constraints), and geographic scope, then calls qloo_recommend.

    Returns a list of dicts with at least {"name": str}. Phone is always None —
    phone numbers come from OSM, never from Qloo.
    """
    signals = _signals_from_constraints(constraints)
    tags = _tags_from_constraints(constraints)

    if not signals and not tags:
        # Nothing to search on. Don't call the API for no reason.
        return []

    payload = {
        "target_type": "restaurant",
        "signals": signals,
        "tags": tags,
        "filters": {"location": "Hong Kong"},
        "limit": limit,
    }
    result = _exec("recommend", payload)
    rows = _rows_from_result(result, source="qloo_recommend")
    if rows:
        print(f"[qloo] recommend -> {len(rows)} names")
    return rows


def rank(candidates: list[dict], constraints: dict, limit: int = RANK_LIMIT) -> list[dict]:
    """Re-rank an existing candidate list by Qloo taste affinity.

    Sends the candidate names to qloo_rank with the group's taste signals.
    Returns the candidates in the Qloo-preferred order (names re-joined to
    the original dicts so phone numbers from OSM are preserved), or the
    original list if Qloo is unavailable.

    The original list is ALWAYS the fallback — this function never discards
    candidates, it only reorders them.
    """
    if not candidates:
        return []

    signals = _signals_from_constraints(constraints)
    tags = _tags_from_constraints(constraints)

    if not signals and not tags:
        return candidates  # nothing to rank on; keep existing order

    names = [c.get("name", "") for c in candidates if c.get("name")]
    if not names:
        return candidates

    payload = {
        "options": names[:40],  # Qloo accepts up to ~40 options per call
        "option_type": "restaurant",
        "signals": signals,
        "tags": tags,
        "limit": limit,
    }
    result = _exec("rank", payload)
    ordered_names = _names_from_rank_result(result)
    if not ordered_names:
        return candidates  # fallback: keep existing order

    # Re-join Qloo's preferred order with the original dicts.
    # Candidates Qloo didn't mention appear at the end in their original order.
    by_norm = {_norm(c.get("name", "")): c for c in candidates}
    reordered: list[dict] = []
    seen: set[str] = set()

    for name in ordered_names:
        key = _norm(name)
        if not key or key in seen:
            continue
        match = by_norm.get(key) or next(
            (v for k, v in by_norm.items() if len(key) > 3 and (key in k or k in key)), None
        )
        if match:
            reordered.append(match)
            seen.add(_norm(match.get("name", "")))

    for c in candidates:
        if _norm(c.get("name", "")) not in seen:
            reordered.append(c)

    print(f"[qloo] rank reordered {len(reordered)} candidates")
    return reordered


def where_popular(entity: str, districts: list[str]) -> list[str]:
    """Find which districts a given entity type is most popular in.

    Used to upgrade a geometric fair-meeting-point to "fair AND culturally
    relevant". Returns districts in popularity order, or the original list
    if Qloo is unavailable.

    `entity` is a descriptive string like "hotpot restaurant".
    `districts` is the shortlist from places.meeting_districts().
    """
    if not entity or not districts:
        return districts

    payload = {
        "entity": entity,
        "within": "Hong Kong",
        "filters": {"districts": districts},
    }
    result = _exec("where_popular", payload)
    ordered = _districts_from_result(result)
    if not ordered:
        return districts  # fallback

    # Keep only districts that were in the original list, in Qloo order.
    original_set = {d.lower() for d in districts}
    filtered = [d for d in ordered if d.lower() in original_set]
    # Append any original districts that Qloo didn't mention, at the end.
    mentioned = {d.lower() for d in filtered}
    remainder = [d for d in districts if d.lower() not in mentioned]
    combined = filtered + remainder

    print(f"[qloo] where_popular({entity!r}) -> {combined}")
    return combined


# ---------------------------------------------------------------------------
# Constraint → Qloo signal helpers
# ---------------------------------------------------------------------------

def _signals_from_constraints(constraints: dict) -> list[str]:
    """Turn prefer_cuisines into Qloo signal strings."""
    signals: list[str] = []
    for cuisine in (constraints.get("prefer_cuisines") or []):
        c = str(cuisine or "").strip().lower()
        if c:
            signals.append(f"cuisine:{c}")
    return signals[:5]  # Qloo recommends a small, focused set


def _tags_from_constraints(constraints: dict) -> list[str]:
    """Resolve soft preferences to Qloo tag IDs (one find_tags call max)."""
    soft_items = constraints.get("soft") or []
    if not soft_items:
        return []

    # Concatenate soft preferences into a single concept query.
    phrases = [str(item.get("constraint") or "") for item in soft_items if item.get("constraint")]
    if not phrases:
        return []
    concept = ", ".join(phrases[:3])  # limit to 3 soft prefs for a focused query
    return find_tags(concept, limit=5)


# ---------------------------------------------------------------------------
# Result parsers (tolerant of whatever the harness actually returns)
# ---------------------------------------------------------------------------

def _rows_from_result(result, source: str) -> list[dict]:
    """Extract name-only rows from a qloo_recommend response."""
    items: list = []
    if isinstance(result, list):
        items = result
    elif isinstance(result, dict):
        items = result.get("results") or result.get("recommendations") or []

    rows: list[dict] = []
    seen: set[str] = set()
    for item in items:
        name = _name_from_item(item)
        if not name:
            continue
        key = _norm(name)
        if not key or key in seen:
            continue
        seen.add(key)
        rows.append({
            "name": name,
            "phone": None,   # rule: Qloo never supplies a phone number
            "area": str((item or {}).get("neighbourhood") or (item or {}).get("area") or "").strip()[:40],
            "cuisine": str((item or {}).get("cuisine") or "").strip()[:60],
            "why": str((item or {}).get("why_relevant") or (item or {}).get("reason") or "").strip()[:160],
            "source": source,
        })
    return rows


def _names_from_rank_result(result) -> list[str]:
    """Extract an ordered list of restaurant names from a qloo_rank response."""
    items: list = []
    if isinstance(result, list):
        items = result
    elif isinstance(result, dict):
        items = result.get("ranked") or result.get("results") or result.get("options") or []

    names: list[str] = []
    for item in items:
        name = _name_from_item(item)
        if name and name not in names:
            names.append(name)
    return names


def _districts_from_result(result) -> list[str]:
    """Extract district names from a qloo_where_popular response."""
    items: list = []
    if isinstance(result, list):
        items = result
    elif isinstance(result, dict):
        items = result.get("locations") or result.get("districts") or result.get("results") or []

    districts: list[str] = []
    for item in items:
        if isinstance(item, str) and item.strip():
            districts.append(item.strip())
        elif isinstance(item, dict):
            name = str(item.get("district") or item.get("location") or item.get("name") or "").strip()
            if name:
                districts.append(name)
    return districts


def _name_from_item(item) -> str:
    """Extract a restaurant name from whatever shape Qloo returns."""
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        return str(
            item.get("restaurant_name") or item.get("name") or item.get("entity") or ""
        ).strip()[:80]
    return ""


# ---------------------------------------------------------------------------
# Merge (same contract as exa_search.merge)
# ---------------------------------------------------------------------------

def merge(osm_rows: list[dict], qloo_rows: list[dict]) -> list[dict]:
    """Merge Qloo names into OSM rows, by normalised name.

    OSM rows are the spine (they carry phone numbers). A Qloo row that matches
    an OSM row only ever adds description. A Qloo row with no OSM match is
    appended with phone=None.

    This is identical in contract to exa_search.merge so callers can treat
    both interchangeably.
    """
    merged = [dict(row) for row in osm_rows]
    index = {_norm(row.get("name", "")): row for row in merged}

    for row in qloo_rows:
        key = _norm(row.get("name", ""))
        if not key:
            continue
        held = index.get(key)
        if held is None:
            held = next(
                (v for k, v in index.items() if len(key) > 4 and (key in k or k in key)),
                None,
            )

        if held is not None:
            if not held.get("cuisine") and row.get("cuisine"):
                held["cuisine"] = row["cuisine"]
            if not held.get("area") and row.get("area"):
                held["area"] = row["area"]
            if row.get("why") and not held.get("why"):
                held["why"] = row["why"]
            held["also_found_by"] = "qloo"
        else:
            fresh = dict(row)
            fresh["phone"] = None  # rule: never from Qloo
            merged.append(fresh)
            index[key] = fresh

    return sorted(
        merged,
        key=lambda r: (bool(r.get("phone")), bool(r.get("cuisine"))),
        reverse=True,
    )


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())
