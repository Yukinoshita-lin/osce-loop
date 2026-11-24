"""API 请求日志：按天追加 JSONL，提供近期明细与汇总。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..config import REQUEST_LOG_DIR


def _log_path(day: str, log_dir: Path | str | None = None) -> Path:
    return Path(log_dir or REQUEST_LOG_DIR) / f"{day}.jsonl"


def append_request_log(entry: dict[str, Any], *, log_dir: Path | str | None = None) -> None:
    path = _log_path(datetime.now().astimezone().strftime("%Y-%m-%d"), log_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def recent_requests(
    *, limit: int = 100, log_dir: Path | str | None = None, day: str | None = None
) -> list[dict[str, Any]]:
    date = day or datetime.now().astimezone().strftime("%Y-%m-%d")
    path = _log_path(date, log_dir)
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    rows = []
    for line in lines[-max(1, min(limit, 500)):]:
        try:
            rows.append(json.loads(line))
        except (TypeError, ValueError):
            continue
    return rows


def request_log_summary(*, days: int = 7, log_dir: Path | str | None = None) -> dict[str, Any]:
    today = datetime.now().astimezone()
    entries: list[dict[str, Any]] = []
    for offset in range(max(1, min(days, 31))):
        date = (today - timedelta(days=offset)).strftime("%Y-%m-%d")
        path = _log_path(date, log_dir)
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                entries.append(json.loads(line))
            except (TypeError, ValueError):
                continue
    durations = sorted(float(entry.get("duration_ms") or 0) for entry in entries)
    error_count = sum(1 for entry in entries if int(entry.get("status") or 0) >= 500)
    by_status: dict[str, int] = {}
    by_path: dict[str, int] = {}
    for entry in entries:
        status = int(entry.get("status") or 0)
        status_key = f"{status // 100}xx"
        by_status[status_key] = by_status.get(status_key, 0) + 1
        path = str(entry.get("path") or "")
        by_path[path] = by_path.get(path, 0) + 1
    return {
        "days": days,
        "total": len(entries),
        "errors": error_count,
        "by_status": by_status,
        "avg_duration_ms": round(sum(durations) / len(durations), 2) if durations else None,
        "p95_duration_ms": durations[int(len(durations) * 0.95) - 1] if durations else None,
        "top_paths": [
            {"path": path, "count": count}
            for path, count in sorted(by_path.items(), key=lambda item: item[1], reverse=True)[:5]
        ],
    }
