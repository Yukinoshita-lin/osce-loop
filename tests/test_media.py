"""媒体完整性：内容哈希、上传哈希与历史回填。"""
from __future__ import annotations

import asyncio
import hashlib
import io

import pytest
from starlette.datastructures import UploadFile

from app import db
from app.domain.media import backfill_media_hashes, file_sha256, media_path_from_url


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "media.db")
    db.init_db(connection)
    yield connection
    connection.close()


def test_file_sha256_and_path_guard(tmp_path):
    path = tmp_path / "clip.wav"
    path.write_bytes(b"RIFFdemo")
    assert file_sha256(path) == "sha256:" + hashlib.sha256(b"RIFFdemo").hexdigest()
    media_dir = tmp_path / "media"
    target = media_dir / "uploads" / "clip.wav"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"x")
    assert media_path_from_url("/media/uploads/clip.wav", media_dir) == target.resolve()
    assert media_path_from_url("/media/../secret.txt", media_dir) is None


def test_backfill_media_hashes(conn, tmp_path):
    media_dir = tmp_path / "media"
    target = media_dir / "demo.wav"
    media_dir.mkdir()
    target.write_bytes(b"RIFFbackfill")
    for attempt_id in ("att_a", "att_b"):
        conn.execute(
            "INSERT INTO attempts(attempt_id, bundle_id, candidate_code, candidate_name, session_id, risk_level, "
            "media_url, duration_ms, integrity_gap, appeal, status, ab_group, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (attempt_id, "bnd", "C1", "考生", "SES", "L1", "/media/demo.wav", 1000, 0, 0, "scored", "ai_assisted", db.now_iso()),
        )
    conn.commit()
    expected = "sha256:" + hashlib.sha256(b"RIFFbackfill").hexdigest()
    assert backfill_media_hashes(conn, media_dir=media_dir) == 2
    rows = conn.execute("SELECT media_sha256 FROM attempts").fetchall()
    assert all(row["media_sha256"] == expected for row in rows)
    assert backfill_media_hashes(conn, media_dir=media_dir) == 0


def test_save_media_computes_hash(tmp_path, monkeypatch):
    from app import api

    monkeypatch.setattr(api, "UPLOAD_DIR", tmp_path)
    upload = UploadFile(file=io.BytesIO(b"RIFFupload"), filename="consult.wav")
    result = asyncio.run(api._save_media(upload))
    expected = "sha256:" + hashlib.sha256(b"RIFFupload").hexdigest()
    assert result["sha256"] == expected
    assert result["size_bytes"] == len(b"RIFFupload")
    assert (tmp_path / result["stored_name"]).read_bytes() == b"RIFFupload"
