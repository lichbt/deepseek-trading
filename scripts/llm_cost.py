#!/usr/bin/env python3
"""Token-cost report over auto_research's per-call usage.jsonl.

Reads one JSON object per line (streamed, no full-memory materialisation) and
reports where the LLM token budget goes: per-stage / per-model / per-day /
per-run totals, tokens wasted on calls that produced nothing, and per-strategy
cost backed by pipeline.db `strategies.created_at`.

Stdlib only, on purpose: this must run against a 200k-line file in seconds and
never pull pandas/numpy into a hot path.
"""
import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOG = REPO_ROOT / ".auto-research-logs" / "usage.jsonl"
DEFAULT_DB = REPO_ROOT / "pipeline.db"

DIMS = ("stage", "model", "day", "run")

WASTE_HEADER = "WASTE (a call may fall in more than one category; rows are not mutually exclusive)"


def _num(value):
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return value
    return 0


def _parse_ts(ts):
    try:
        return datetime.strptime(str(ts)[:19], "%Y-%m-%dT%H:%M:%S")
    except Exception:
        return None


def _is_wasted(rec):
    if "error" in rec:
        return True
    status = rec.get("status")
    if status is not None and status >= 400:
        return True
    if rec.get("finish_reason") == "length":
        return True
    return False


def _is_metered(rec):
    return rec.get("total_tokens") is not None


def _group_key(rec, by):
    if by == "stage":
        return rec.get("stage") or "other"
    if by == "model":
        served = rec.get("served")
        if served is not None:
            return str(served)
        return "%s?" % (rec.get("requested") or "unknown")
    if by == "day":
        ts = rec.get("ts") or ""
        return ts[:10] or "unknown"
    if by == "run":
        return rec.get("run") or "unknown"
    raise ValueError("unknown --by %r" % by)


def _new_bucket():
    return {
        "calls": 0,
        "failed": 0,
        "unmetered": 0,
        "prompt": 0,
        "completion": 0,
        "total": 0,
        "cached": 0,
        "latency_sum": 0.0,
    }


def _load_strategy_count(db_path, min_bound, max_bound):
    """Count strategies created inside the ACTUAL window, to the second.

    This used to compare substr(created_at,1,10) — a DATE prefix — which made
    any sub-day window count the whole day: a 69-second window reported 155
    strategies and a "251 tokens per strategy" that meant nothing. Both
    timestamps are UTC ISO and sort lexicographically once the usage log's
    "+00:00" suffix is trimmed, so compare the first 19 chars of each.
    """
    if min_bound is None or max_bound is None:
        return None
    path = Path(db_path)
    if not path.is_file():
        return None
    try:
        con = sqlite3.connect("file:%s?mode=ro" % str(path.resolve()), uri=True)
    except Exception:
        return None
    try:
        cur = con.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='strategies'"
        )
        if not cur.fetchone()[0]:
            return None
        cur = con.execute(
            "SELECT COUNT(*) FROM strategies "
            "WHERE substr(created_at,1,19) >= ? AND substr(created_at,1,19) <= ?",
            (min_bound, max_bound),
        )
        return cur.fetchone()[0]
    except Exception:
        return None
    finally:
        try:
            con.close()
        except Exception:
            pass


def aggregate(log_path, by="stage", since=None, hours=None, db=None):
    by = by if by in DIMS else "stage"
    now = datetime.now(timezone.utc)
    if since:
        def keep(ts):
            return str(ts)[:10] >= since
    elif hours is not None:
        cutoff = now - timedelta(hours=hours)

        def keep(ts):
            dt = _parse_ts(ts)
            return dt is not None and dt.replace(tzinfo=timezone.utc) >= cutoff
    else:
        def keep(ts):
            return True

    buckets = {}
    waste = {
        "error": {"calls": 0, "total": 0},
        "status_ge_400": {"calls": 0, "total": 0},
        "finish_reason_length": {"calls": 0, "total": 0},
    }
    lines_read = 0
    malformed = 0
    out_of_window = 0
    min_ts = None
    max_ts = None

    with open(log_path, "r") as fh:
        for line in fh:
            lines_read += 1
            line = line.strip()
            if not line:
                malformed += 1
                continue
            try:
                rec = json.loads(line)
            except Exception:
                malformed += 1
                continue
            if not isinstance(rec, dict):
                malformed += 1
                continue
            ts = rec.get("ts")
            if not isinstance(ts, str) or not ts:
                malformed += 1
                continue
            if not keep(ts):
                out_of_window += 1
                continue

            if min_ts is None or ts < min_ts:
                min_ts = ts
            if max_ts is None or ts > max_ts:
                max_ts = ts

            key = _group_key(rec, by)
            b = buckets.get(key)
            if b is None:
                b = buckets[key] = _new_bucket()

            b["calls"] += 1
            if _is_wasted(rec):
                b["failed"] += 1
            if not _is_metered(rec):
                b["unmetered"] += 1
            b["prompt"] += _num(rec.get("prompt_tokens"))
            b["completion"] += _num(rec.get("completion_tokens"))
            b["total"] += _num(rec.get("total_tokens"))
            b["cached"] += _num(rec.get("cached_tokens"))
            latency = rec.get("latency_s")
            if latency is not None:
                b["latency_sum"] += _num(latency)

            if "error" in rec:
                waste["error"]["calls"] += 1
                waste["error"]["total"] += _num(rec.get("total_tokens"))
            status = rec.get("status")
            if status is not None and status >= 400:
                waste["status_ge_400"]["calls"] += 1
                waste["status_ge_400"]["total"] += _num(rec.get("total_tokens"))
            if rec.get("finish_reason") == "length":
                waste["finish_reason_length"]["calls"] += 1
                waste["finish_reason_length"]["total"] += _num(rec.get("total_tokens"))

    grand = _new_bucket()
    for b in buckets.values():
        for field in ("calls", "failed", "unmetered", "prompt", "completion", "total", "cached"):
            grand[field] += b[field]
        grand["latency_sum"] += b["latency_sum"]
    grand_total_tokens = grand["total"]

    groups = []
    for key, b in buckets.items():
        groups.append(
            {
                "key": key,
                "calls": b["calls"],
                "failed_calls": b["failed"],
                "unmetered_calls": b["unmetered"],
                "prompt_tokens": b["prompt"],
                "completion_tokens": b["completion"],
                "total_tokens": b["total"],
                "cached_tokens": b["cached"],
                "pct_total_tokens": (100.0 * b["total"] / grand_total_tokens) if grand_total_tokens else 0.0,
                "avg_latency_s": (b["latency_sum"] / b["calls"]) if b["calls"] else 0.0,
            }
        )
    groups.sort(key=lambda g: (-g["total_tokens"], g["key"]))

    # Second granularity, not date: see _load_strategy_count.
    min_bound = (min_ts or "")[:19] or None
    max_bound = (max_ts or "")[:19] or None
    strategy_count = _load_strategy_count(db, min_bound, max_bound)
    strategies = None
    if strategy_count is not None:
        strategies = {
            "strategies_created": strategy_count,
            "total_tokens": grand_total_tokens,
            "tokens_per_strategy": (grand_total_tokens / strategy_count) if strategy_count else None,
        }

    total_row = {
        "key": "TOTAL",
        "calls": grand["calls"],
        "failed_calls": grand["failed"],
        "unmetered_calls": grand["unmetered"],
        "prompt_tokens": grand["prompt"],
        "completion_tokens": grand["completion"],
        "total_tokens": grand["total"],
        "cached_tokens": grand["cached"],
        "pct_total_tokens": 100.0,
        "avg_latency_s": (grand["latency_sum"] / grand["calls"]) if grand["calls"] else 0.0,
    }

    return {
        "window": {"min_ts": min_ts, "max_ts": max_ts},
        "lines_read": lines_read,
        "malformed_skipped": malformed,
        "out_of_window": out_of_window,
        "by": by,
        "groups": groups,
        "total": total_row,
        "waste": {
            "error": {"calls": waste["error"]["calls"], "total_tokens": waste["error"]["total"]},
            "status_ge_400": {"calls": waste["status_ge_400"]["calls"], "total_tokens": waste["status_ge_400"]["total"]},
            "finish_reason_length": {"calls": waste["finish_reason_length"]["calls"], "total_tokens": waste["finish_reason_length"]["total"]},
        },
        "strategies": strategies,
    }


def _fmt_num(v):
    return "%d" % int(v)


def _fmt_float(v):
    return "%.3f" % v


def _render_text(report):
    out = []
    w = report["window"]
    out.append("window: %s .. %s" % (w["min_ts"] or "none", w["max_ts"] or "none"))
    out.append("lines read: %d   malformed skipped: %d   out of window: %d" % (
        report["lines_read"], report["malformed_skipped"], report["out_of_window"]))
    out.append("")
    out.append("BY %s" % report["by"])

    header = ["group", "calls", "failed", "unmetered", "prompt", "completion",
              "total", "cached", "%total", "avg_lat_s"]
    rows = [header]
    for g in report["groups"]:
        rows.append([
            g["key"],
            _fmt_num(g["calls"]),
            _fmt_num(g["failed_calls"]),
            _fmt_num(g["unmetered_calls"]),
            _fmt_num(g["prompt_tokens"]),
            _fmt_num(g["completion_tokens"]),
            _fmt_num(g["total_tokens"]),
            _fmt_num(g["cached_tokens"]),
            "%.1f" % g["pct_total_tokens"],
            _fmt_float(g["avg_latency_s"]),
        ])
    t = report["total"]
    rows.append([
        t["key"],
        _fmt_num(t["calls"]),
        _fmt_num(t["failed_calls"]),
        _fmt_num(t["unmetered_calls"]),
        _fmt_num(t["prompt_tokens"]),
        _fmt_num(t["completion_tokens"]),
        _fmt_num(t["total_tokens"]),
        _fmt_num(t["cached_tokens"]),
        "%.1f" % t["pct_total_tokens"],
        _fmt_float(t["avg_latency_s"]),
    ])

    widths = [max(len(r[i]) for r in rows) for i in range(len(header))]
    for i, r in enumerate(rows):
        cells = []
        for j, cell in enumerate(r):
            if j == 0:
                cells.append(cell.ljust(widths[j]))
            else:
                cells.append(cell.rjust(widths[j]))
        out.append("  ".join(cells))
        if i == 0:
            out.append("  ".join("-" * widths[j] for j in range(len(header))))

    out.append("")
    out.append(WASTE_HEADER)
    waste_rows = [
        ("error", report["waste"]["error"]),
        ("status>=400", report["waste"]["status_ge_400"]),
        ("finish_reason=length", report["waste"]["finish_reason_length"]),
    ]
    w_header = ["category", "calls", "total_tokens"]
    w_widths = [max(len(x) for x in [w_header[0]] + [name for name, _ in waste_rows]),
                max(len(w_header[1]), len(str(max((r["calls"] for _, r in waste_rows), default=0)))),
                max(len(w_header[2]), len(str(max((r["total_tokens"] for _, r in waste_rows), default=0))))]
    out.append("  ".join(w_header[i].ljust(w_widths[i]) if i == 0 else w_header[i].rjust(w_widths[i]) for i in range(3)))
    for name, r in waste_rows:
        out.append("  ".join([
            name.ljust(w_widths[0]),
            _fmt_num(r["calls"]).rjust(w_widths[1]),
            _fmt_num(r["total_tokens"]).rjust(w_widths[2]),
        ]))

    out.append("")
    out.append("PER-STRATEGY")
    s = report["strategies"]
    if s is None:
        out.append("n/a")
    else:
        out.append("strategies in window: %d" % s["strategies_created"])
        out.append("total_tokens: %d" % s["total_tokens"])
        if s["tokens_per_strategy"] is None:
            out.append("tokens per strategy: n/a")
        else:
            out.append("tokens per strategy: %.2f" % s["tokens_per_strategy"])
    return "\n".join(out)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Report LLM token cost from usage.jsonl")
    parser.add_argument("--log", default=str(DEFAULT_LOG))
    window = parser.add_mutually_exclusive_group()
    window.add_argument("--since", metavar="DATE", help="YYYY-MM-DD, inclusive, UTC")
    window.add_argument("--hours", type=float, metavar="N", help="last N hours")
    parser.add_argument("--by", choices=DIMS, default="stage")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    log_path = args.log
    if not Path(log_path).is_file():
        print("llm_cost: log file not found: %s" % log_path, file=sys.stderr)
        return 1

    report = aggregate(log_path, by=args.by, since=args.since, hours=args.hours, db=args.db)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=False))
    else:
        print(_render_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())