from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT: Path = Path(__file__).resolve().parents[1]


def describe(data: object) -> str:
    """One-line summary of a response body for diagnostics."""
    if isinstance(data, list):
        return f"list[{len(data)}]"
    if isinstance(data, dict):
        keys: str = ", ".join(sorted(data.keys())[:6])
        if len(data.keys()) > 6:
            keys += ", ..."
        return f"dict{{{keys}}}"
    return repr(data)[:80]


def history_line(age_hours: float, pad: int = 0, **fields: object) -> str:
    """Build a history JSONL line with a timestamp age_hours in the past; extra
    keyword arguments become record fields (e.g. a network block)."""
    ts: datetime = datetime.now(timezone.utc) - timedelta(hours=age_hours)
    record: dict[str, object] = {"timestamp": ts.isoformat(), "cpu": {"avg": 1.0}}
    if pad:
        record["pad"] = "x" * pad
    record.update(fields)
    return json.dumps(record) + "\n"
