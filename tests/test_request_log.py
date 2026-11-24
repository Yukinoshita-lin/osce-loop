"""API 请求日志：追加、近期明细与多日汇总。"""
from __future__ import annotations

from datetime import datetime, timedelta

from app.domain.request_log import append_request_log, recent_requests, request_log_summary


def _entry(path="/api/overview", status=200, duration=10.0):
    return {
        "at": "2026-01-01T00:00:00+08:00",
        "method": "GET",
        "path": path,
        "status": status,
        "duration_ms": duration,
        "user": "admin",
        "role": "admin",
        "ip": "127.0.0.1",
    }


def test_append_and_recent_returns_latest_entries(tmp_path):
    for index in range(5):
        append_request_log(_entry(path=f"/api/p{index}", duration=index), log_dir=tmp_path)

    rows = recent_requests(limit=3, log_dir=tmp_path)
    assert [row["path"] for row in rows] == ["/api/p2", "/api/p3", "/api/p4"]


def test_recent_returns_empty_when_no_log(tmp_path):
    assert recent_requests(log_dir=tmp_path / "missing") == []


def test_recent_skips_broken_lines(tmp_path):
    path = tmp_path / f"{datetime.now().astimezone().strftime('%Y-%m-%d')}.jsonl"
    path.write_text('{"path": "/api/a", "status": 200}\nnot-json\n', encoding="utf-8")
    rows = recent_requests(log_dir=tmp_path)
    assert len(rows) == 1
    assert rows[0]["path"] == "/api/a"


def test_summary_counts_status_errors_and_percentiles(tmp_path):
    for duration in range(1, 11):
        status = 500 if duration >= 9 else 200
        append_request_log(_entry(status=status, duration=duration), log_dir=tmp_path)
    append_request_log(_entry(path="/api/slow", status=404, duration=20), log_dir=tmp_path)

    summary = request_log_summary(days=1, log_dir=tmp_path)
    assert summary["total"] == 11
    assert summary["errors"] == 2
    assert summary["by_status"] == {"2xx": 8, "4xx": 1, "5xx": 2}
    assert summary["avg_duration_ms"] == 6.82
    assert summary["p95_duration_ms"] == 10.0
    assert summary["top_paths"][0] == {"path": "/api/overview", "count": 10}


def test_summary_reads_multiple_days(tmp_path):
    today = datetime.now().astimezone()
    for offset in range(2):
        day = (today - timedelta(days=offset)).strftime("%Y-%m-%d")
        (tmp_path / f"{day}.jsonl").write_text(
            '{"path": "/api/x", "status": 200, "duration_ms": 5}\n', encoding="utf-8"
        )
    assert request_log_summary(days=7, log_dir=tmp_path)["total"] == 2
    assert request_log_summary(days=1, log_dir=tmp_path)["total"] == 1


def test_summary_without_logs_is_empty(tmp_path):
    summary = request_log_summary(days=7, log_dir=tmp_path / "missing")
    assert summary["total"] == 0
    assert summary["errors"] == 0
    assert summary["avg_duration_ms"] is None
    assert summary["p95_duration_ms"] is None
    assert summary["top_paths"] == []
