"""转写完整性：规范化摘要、状态判定与历史回填。"""
from __future__ import annotations

from app import db
from app.domain.integrity import (
    backfill_transcript_hashes,
    transcript_digest,
    transcript_integrity,
)
from app.seed import seed_if_empty


def test_digest_is_canonical_across_key_order():
    first = [{"text": "询问胸痛", "start_ms": 0, "speaker": "candidate"}]
    second = [{"speaker": "candidate", "start_ms": 0, "text": "询问胸痛"}]
    digest = transcript_digest(first)
    assert digest == transcript_digest(second)
    assert digest.startswith("sha256:")


def test_integrity_reports_all_states():
    utterances = [{"text": "您好", "start_ms": 0}]
    digest = transcript_digest(utterances)
    assert transcript_integrity(digest, utterances) == "ok"
    assert transcript_integrity(digest, None) == "missing"
    assert transcript_integrity(None, utterances) == "unrecorded"
    assert transcript_integrity(digest, [{"text": "被改写", "start_ms": 0}]) == "mismatch"


def test_backfill_fills_missing_transcript_hashes(tmp_path):
    conn = db.connect(tmp_path / "integrity.db")
    db.init_db(conn)
    seed_if_empty(conn)
    total = conn.execute("SELECT COUNT(*) AS n FROM transcripts").fetchone()["n"]
    assert total > 0
    conn.execute("UPDATE transcripts SET sha256 = NULL")
    conn.commit()
    assert backfill_transcript_hashes(conn) == total
    remaining = conn.execute(
        "SELECT COUNT(*) AS n FROM transcripts WHERE sha256 IS NULL OR sha256 = ''"
    ).fetchone()["n"]
    assert remaining == 0
    conn.close()
