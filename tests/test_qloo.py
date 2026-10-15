"""Qloo boundary tests. Every subprocess is intercepted; no credentials are used."""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import qloo_search as q
from harness import Suite

CONSTRAINTS = {"prefer_cuisines": ["hotpot"], "avoid_cuisines": ["seafood"],
               "soft": [{"constraint": "cosy", "who": "PRIVATE NAME", "quote": "PRIVATE MESSAGE"}],
               "coming_from": [{"who": "PRIVATE NAME", "place": "PRIVATE ORIGIN"}]}


def response(value, code=0):
    return subprocess.CompletedProcess([], code, json.dumps(value), "private stderr")


def contract_payloads() -> list[dict]:
    """Capture real wrapper requests for offline validation by the published harness."""
    calls = []
    def capture(workflow, payload):
        calls.append({"workflow": workflow, "payload": payload})
        return []
    with patch.object(q, "_exec", side_effect=capture):
        q.find_tags("cosy")
        q.recommend(CONSTRAINTS)
        q.rank([{"name": f"Restaurant {i}"} for i in range(60)], CONSTRAINTS)
        q.where_popular("Samsen", "Hong Kong")
        q.recommend({"soft": [{"constraint": "lively"}]})
    return calls


def run() -> Suite:
    s = Suite("qloo", expect_at_least=50)
    with patch.object(q, "_command", side_effect=lambda *args: ["qloo", *args]):
        # Saved harness credentials work even without an environment key.
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(q.subprocess, "run", return_value=response({"status": "ok", "results": []})) as proc:
            q._exec("recommend", {"target_type": "place"})
            s.eq("saved credentials still invoke the harness", proc.call_count, 1)
            s.eq("payload travels on stdin", json.loads(proc.call_args.kwargs["input"]), {"target_type": "place"})
        with patch.dict(os.environ, {"QLOO_API_KEY": "test-only"}), \
             patch.object(q.subprocess, "run", return_value=response([])) as proc:
            q._exec("rank", {})
            s.eq("environment credential inherited", proc.call_args.kwargs["env"]["QLOO_API_KEY"], "test-only")
        for label, result in [("auth", response({}, 4)), ("bad JSON", subprocess.CompletedProcess([], 0, "<html>", "")),
                              ("null", response(None)), ("scalar", response(1)),
                              ("ambiguous", response({"status": "needs_input", "results": [{"name": "Wrong"}]})),
                              ("error", response({"error": {"code": "QLOO_AUTH"}}))]:
            with patch.object(q.subprocess, "run", return_value=result):
                s.eq(f"{label} falls back", q._exec("recommend", {}), [])
        for error in (FileNotFoundError(), OSError(), subprocess.TimeoutExpired("qloo", 12)):
            with patch.object(q.subprocess, "run", side_effect=error):
                s.eq(f"{type(error).__name__} falls back", q._exec("recommend", {}), [])
        for ready in (True, False):
            with patch.object(q.subprocess, "run", return_value=response({"qloo": {"ready": ready}})):
                s.eq(f"readiness {ready}", q.readiness(), ready)
        for malformed in ([], None, {"qloo": None}, {"qloo": {"ready": False}}):
            with patch.object(q.subprocess, "run", return_value=response(malformed)):
                s.eq("malformed readiness fails closed", q.readiness(), False)
        with patch.object(q.subprocess, "run", return_value=response({"key": "SECRET"}, 1)):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                q._exec("recommend", {})
            s.check("subprocess output is not logged", "SECRET" not in output.getvalue() and "private stderr" not in output.getvalue())

    calls = contract_payloads()
    for call in calls:
        serial = json.dumps(call)
        s.check(f"{call['workflow']} excludes private attribution", "PRIVATE" not in serial)
    s.eq("tag query uses documented field", calls[0]["payload"]["query"], "cosy")
    s.eq("recommend uses place entity type", calls[1]["payload"]["target_type"], "place")
    s.eq("cuisine and vibe are tag signals", calls[1]["payload"]["signal_tags"], ["hotpot", "cosy"])
    s.eq("recommend filters to Hong Kong", calls[1]["payload"]["filter_location"], "Hong Kong")
    s.eq("cuisine exclusions preserved", calls[1]["payload"]["exclude_tags"], ["seafood"])
    s.eq("rank respects ten-option maximum", len(calls[2]["payload"]["options"]), 10)
    s.eq("rank uses aggregate HK audience", calls[2]["payload"]["signal_location"], "Hong Kong")
    s.eq("rank sends no unsupported limit", "limit" in calls[2]["payload"], False)
    s.eq("heatmap has no unsupported filters", "filters" in calls[3]["payload"], False)
    with patch.object(q, "_exec") as proc:
        s.eq("empty preferences make no discovery request", q.recommend({}), [])
        s.eq("blank tag query makes no request", q.find_tags("  "), [])
        s.eq("empty candidates make no request", q.rank([], CONSTRAINTS), [])
        s.eq("empty inputs never invoke CLI", proc.call_count, 0)

    for malformed in (None, 1, {"results": None}, {"results": "bad"}, {"results": {}}, {"status": "needs_input", "results": ["Wrong"]}):
        s.eq("malformed results are empty", q._rows_from_result(malformed, "qloo"), [])
    rows = q._rows_from_result({"results": ["海底撈", {"name": "Samsen", "phone": "FAKE", "entity_id": "id-1"}, None]}, "qloo")
    s.eq("Chinese names and string rows survive", [r["name"] for r in rows], ["海底撈", "Samsen"])
    s.check("Qloo never supplies phones", all(r["phone"] is None for r in rows))
    s.eq("entity identity retained", rows[1]["qloo_entity_id"], "id-1")
    pool = [{"name": "大家樂", "phone": "111"}, {"name": "海底撈", "phone": "222"},
            {"name": "海底撈", "phone": "333"}] + [{"name": f"Venue {i}"} for i in range(20)]
    with patch.object(q, "_exec", return_value={"results": [{"name": "海底撈"}, {"name": "海底撈"}, {"name": "Unknown"}]}):
        ranked = q.rank(pool, CONSTRAINTS)
    s.eq("Chinese result ranks both branches", [r.get("phone") for r in ranked[:2]], ["222", "333"])
    s.eq("rank preserves candidate count", len(ranked), len(pool))
    s.eq("rank preserves all original objects", sorted(map(id, ranked)), sorted(map(id, pool)))
    with patch.object(q, "_exec", return_value=[]):
        s.check("failed ranking retains original list", q.rank(pool, CONSTRAINTS) is pool)
    original = [{"name": "Samsen Wan Chai", "phone": "111"}, {"name": "海底撈", "phone": "222"}]
    merged = q.merge(original, [{"name": "Samsen", "phone": "FAKE"}, {"name": "海底撈", "why": "taste match"}])
    s.eq("different branch never inherits phone", next(r for r in merged if r["name"] == "Samsen")["phone"], None)
    s.eq("exact Chinese match keeps real phone", next(r for r in merged if r["name"] == "海底撈")["phone"], "222")
    s.eq("merge does not mutate OSM input", original[1].get("why"), None)
    for value, expected in [("bad", 10), (0, 1), (99, 20), (None, 10)]:
        s.eq("invalid limits are bounded", q._limit(value), expected)
    with patch.dict(os.environ, {"QLOO_RECOMMEND_LIMIT": "7", "QLOO_RANK_LIMIT": "3"}), \
         patch.object(q, "_exec", return_value=[]) as proc:
        q.recommend(CONSTRAINTS)
        s.eq("recommend reads configuration after import", proc.call_args.args[1]["limit"], 7)
        q.rank(pool, CONSTRAINTS)
        s.eq("rank reads configuration after import", len(proc.call_args.args[1]["options"]), 3)
    return s


if __name__ == "__main__":
    if "--payloads" in sys.argv:
        print(json.dumps(contract_payloads()))
    else:
        summary = run().summary()
        print(json.dumps(summary, indent=2))
        raise SystemExit(0 if summary["ok"] else 1)
