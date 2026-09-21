"""Tests for scripts/llm_cost.py — the read side of the usage log.

The synthetic JSONL exercises every classification path: metered calls across
several stages, a null `served`, a network `error`, an HTTP 400, a
finish_reason="length" (including one call that is BOTH a 400 and a length, to
prove the waste categories are not mutually exclusive), an unmetered record with
no token fields at all, and one malformed line. All numbers below are
hand-computed.
"""
import json
import os
import sqlite3
import sys

import pytest

SCRIPTS = os.path.join(os.path.dirname(__file__), "..", "scripts")
sys.path.insert(0, SCRIPTS)
import llm_cost


def _line(**kw):
    rec = {
        "ts": "2026-08-20T10:00:00+00:00",
        "run": "run1",
        "stage": "other",
        "requested": "m1",
        "base": "https://x/v1",
        "max_tokens": 1000,
        "prompt_chars": 1,
        "latency_s": 1.0,
        "status": 200,
        "served": None,
        "finish_reason": "stop",
        "prompt_tokens": None,
        "completion_tokens": None,
        "total_tokens": None,
    }
    rec.update(kw)
    return json.dumps(rec)


# Hand-computed expected numbers for the default fixture.
EXPECTED_GROUPS = {
    "thesis_batch": dict(calls=2, failed=0, unmetered=0, prompt=110, completion=220,
                         total=330, cached=50, latency_sum=3.0),
    "codegen": dict(calls=4, failed=3, unmetered=2, prompt=7, completion=17,
                    total=24, cached=0, latency_sum=4.2),
    "critique_thesis": dict(calls=2, failed=1, unmetered=1, prompt=7, completion=3,
                            total=10, cached=0, latency_sum=1.7),
    "thesis_single": dict(calls=1, failed=0, unmetered=0, prompt=40, completion=60,
                          total=100, cached=0, latency_sum=1.1),
}
EXPECTED_TOTAL = dict(calls=9, failed=4, unmetered=3, prompt=164, completion=300,
                      total=464, cached=50, latency_sum=10.0)


@pytest.fixture
def log_path(tmp_path):
    lines = [
        _line(ts="2026-08-20T10:00:00+00:00", stage="thesis_batch", requested="m1",
              prompt_tokens=100, completion_tokens=200, total_tokens=300,
              cached_tokens=50, latency_s=1.0),
        _line(ts="2026-08-20T10:05:00+00:00", stage="thesis_batch", requested="m1",
              prompt_tokens=10, completion_tokens=20, total_tokens=30, latency_s=2.0),
        _line(ts="2026-08-20T10:10:00+00:00", stage="codegen", requested="m1", served="m2",
              prompt_tokens=5, completion_tokens=15, total_tokens=20, latency_s=3.0),
        _line(ts="2026-08-20T10:15:00+00:00", stage="codegen", requested="m1",
              error="connection reset", latency_s=0.5,
              status=None, finish_reason=None),
        _line(ts="2026-08-20T10:20:00+00:00", stage="codegen", requested="m1",
              status=400, finish_reason=None, latency_s=0.4),
        _line(ts="2026-08-20T10:25:00+00:00", stage="critique_thesis", requested="m1",
              finish_reason="length", prompt_tokens=7, completion_tokens=3,
              total_tokens=10, latency_s=1.5),
        _line(ts="2026-08-20T10:30:00+00:00", stage="critique_thesis", requested="m1",
              latency_s=0.2,
              prompt_tokens=None, completion_tokens=None, total_tokens=None),
        "this is not json {",
        _line(ts="2026-08-20T10:35:00+00:00", stage="thesis_single", requested="m3",
              prompt_tokens=40, completion_tokens=60, total_tokens=100, latency_s=1.1),
        _line(ts="2026-08-20T10:40:00+00:00", stage="codegen", requested="m1",
              status=400, finish_reason="length", prompt_tokens=2, completion_tokens=2,
              total_tokens=4, latency_s=0.3),
    ]
    path = tmp_path / "usage.jsonl"
    path.write_text("\n".join(lines) + "\n")
    return path


def _groups_by_key(report):
    return {g["key"]: g for g in report["groups"]}


def test_stage_group_aggregation_is_exact(log_path):
    report = llm_cost.aggregate(log_path, by="stage", db=str(log_path.parent / "no.db"))
    groups = _groups_by_key(report)
    for stage, exp in EXPECTED_GROUPS.items():
        g = groups[stage]
        assert g["calls"] == exp["calls"]
        assert g["failed_calls"] == exp["failed"]
        assert g["unmetered_calls"] == exp["unmetered"]
        assert g["prompt_tokens"] == exp["prompt"]
        assert g["completion_tokens"] == exp["completion"]
        assert g["total_tokens"] == exp["total"]
        assert g["cached_tokens"] == exp["cached"]
        assert g["avg_latency_s"] == pytest.approx(exp["latency_sum"] / exp["calls"])

    t = report["total"]
    assert t["calls"] == EXPECTED_TOTAL["calls"]
    assert t["failed_calls"] == EXPECTED_TOTAL["failed"]
    assert t["unmetered_calls"] == EXPECTED_TOTAL["unmetered"]
    assert t["prompt_tokens"] == EXPECTED_TOTAL["prompt"]
    assert t["completion_tokens"] == EXPECTED_TOTAL["completion"]
    assert t["total_tokens"] == EXPECTED_TOTAL["total"]
    assert t["cached_tokens"] == EXPECTED_TOTAL["cached"]
    assert t["avg_latency_s"] == pytest.approx(EXPECTED_TOTAL["latency_sum"] / EXPECTED_TOTAL["calls"])


def test_groups_sorted_by_total_tokens_descending(log_path):
    report = llm_cost.aggregate(log_path, by="stage", db=str(log_path.parent / "no.db"))
    totals = [g["total_tokens"] for g in report["groups"]]
    assert totals == sorted(totals, reverse=True)
    assert totals[0] == 330 and totals[-1] == 10


def test_waste_categories_reported_separately(log_path):
    report = llm_cost.aggregate(log_path, by="stage", db=str(log_path.parent / "no.db"))
    w = report["waste"]
    assert w["error"] == {"calls": 1, "total_tokens": 0}
    assert w["status_ge_400"] == {"calls": 2, "total_tokens": 4}
    assert w["finish_reason_length"] == {"calls": 2, "total_tokens": 14}


def test_unmetered_not_folded_into_token_sums(log_path):
    report = llm_cost.aggregate(log_path, by="stage", db=str(log_path.parent / "no.db"))
    g = _groups_by_key(report)["critique_thesis"]
    assert g["unmetered_calls"] == 1
    assert g["total_tokens"] == 10
    assert g["prompt_tokens"] == 7
    assert g["completion_tokens"] == 3
    assert report["total"]["unmetered_calls"] == 3


def test_malformed_line_counted_not_fatal(log_path):
    report = llm_cost.aggregate(log_path, by="stage", db=str(log_path.parent / "no.db"))
    assert report["malformed_skipped"] == 1
    assert report["lines_read"] == 10
    assert report["by"] == "stage"


def test_model_grouping_uses_served_with_question_fallback(log_path):
    report = llm_cost.aggregate(log_path, by="model", db=str(log_path.parent / "no.db"))
    keys = {g["key"] for g in report["groups"]}
    assert "m2" in keys              # served model, never marked with "?"
    assert "m1?" in keys             # served null -> requested, marked "?"
    assert "m3?" in keys
    for g in report["groups"]:
        if g["key"] == "m2":
            assert not g["key"].endswith("?")
    falls_back = [g for g in report["groups"] if g["key"] == "m1?"]
    assert falls_back[0]["calls"] == 7


def test_missing_log_file_exits_1(tmp_path):
    missing = tmp_path / "nope.jsonl"
    assert llm_cost.main(["--log", str(missing)]) == 1


def test_json_output_is_valid_and_complete(log_path, capsys):
    rc = llm_cost.main(["--log", str(log_path), "--by", "stage", "--json",
                        "--db", str(log_path.parent / "no.db")])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["by"] == "stage"
    assert payload["total"]["total_tokens"] == 464
    assert payload["malformed_skipped"] == 1
    assert payload["strategies"] is None


def _make_db(path, rows):
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE strategies (id TEXT PRIMARY KEY, created_at TEXT, status TEXT)"
    )
    for cid, created in rows:
        con.execute("INSERT INTO strategies (id, created_at, status) VALUES (?, ?, ?)",
                    (cid, created, "proposed"))
    con.commit()
    con.close()


def test_per_strategy_counts_strategies_in_window(tmp_path):
    """The window is the REAL span of the log, to the second — not its date.

    Regression: this joined on substr(created_at,1,10), so a 69-second window
    counted every strategy of the whole day (155) and reported a confident
    "251 tokens per strategy" that measured nothing.
    """
    log = tmp_path / "usage.jsonl"
    log.write_text("\n".join([
        _line(ts="2026-08-20T10:00:00+00:00", stage="thesis_batch",
              prompt_tokens=80, completion_tokens=20, total_tokens=100),
        _line(ts="2026-08-20T10:30:00+00:00", stage="codegen",
              prompt_tokens=30, completion_tokens=20, total_tokens=50),
    ]) + "\n")

    db = tmp_path / "strat.db"
    _make_db(db, [
        ("in_at_start", "2026-08-20T10:00:00.000000"),   # boundary: inclusive
        ("in_middle",   "2026-08-20T10:15:30.123456"),
        ("in_at_end",   "2026-08-20T10:30:00.999999"),   # boundary: inclusive
        ("before",      "2026-08-20T09:59:59.000000"),   # same DAY, outside window
        ("after",       "2026-08-20T10:30:01.000000"),   # same DAY, outside window
        ("prev_day",    "2026-08-19T13:00:00.000000"),
        ("next_day",    "2026-08-22T12:00:00.000000"),
    ])

    report = llm_cost.aggregate(log, by="stage", db=str(db))
    s = report["strategies"]
    # The four same-day rows outside 10:00:00..10:30:00 must NOT be counted.
    assert s["strategies_created"] == 3
    assert s["total_tokens"] == 150
    assert s["tokens_per_strategy"] == 50.0


def test_per_strategy_degrades_when_table_absent(tmp_path):
    log = tmp_path / "usage.jsonl"
    log.write_text(_line(stage="thesis_batch", prompt_tokens=10, completion_tokens=0,
                         total_tokens=10) + "\n")
    db = tmp_path / "empty.db"
    _make_db(db, [])
    # drop strategies so only the table-name guard triggers
    con = sqlite3.connect(db)
    con.execute("DROP TABLE strategies")
    con.commit()
    con.close()

    report = llm_cost.aggregate(log, by="stage", db=str(db))
    assert report["strategies"] is None


def test_since_filter_is_inclusive_on_date_prefix(tmp_path):
    log = tmp_path / "usage.jsonl"
    log.write_text("\n".join([
        _line(ts="2026-08-19T23:59:59+00:00", stage="thesis_batch",
              prompt_tokens=1, completion_tokens=1, total_tokens=2),
        _line(ts="2026-08-20T00:00:00+00:00", stage="thesis_batch",
              prompt_tokens=1, completion_tokens=1, total_tokens=2),
        _line(ts="2026-08-21T00:00:00+00:00", stage="thesis_batch",
              prompt_tokens=10, completion_tokens=10, total_tokens=20),
    ]) + "\n")
    report = llm_cost.aggregate(log, by="stage", since="2026-08-20",
                                db=str(log.parent / "no.db"))
    assert report["total"]["total_tokens"] == 22
    assert report["out_of_window"] == 1