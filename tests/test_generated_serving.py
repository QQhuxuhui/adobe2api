"""/generated 静态服务按实际字节声明 Content-Type（落盘统一 .png，但 Leonardo 图多为 JPEG）。"""
from fastapi.testclient import TestClient

import app as app_module


def _write(tmp_path, name, data, monkeypatch):
    monkeypatch.setattr(app_module, "GENERATED_DIR", tmp_path)
    (tmp_path / name).write_bytes(data)
    return TestClient(app_module.app)


def test_jpeg_bytes_named_png_served_as_jpeg(tmp_path, monkeypatch):
    client = _write(tmp_path, "job1.png", b"\xff\xd8\xff\xe0JFIF....", monkeypatch)
    r = client.get("/generated/job1.png")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"


def test_png_bytes_served_as_png(tmp_path, monkeypatch):
    client = _write(tmp_path, "job2.png", b"\x89PNG\r\n\x1a\nrest", monkeypatch)
    r = client.get("/generated/job2.png")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"


def test_sniffer_unit():
    from app import _sniff_generated_media_type
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "x"
        p.write_bytes(b"\xff\xd8\xff\xe0")
        assert _sniff_generated_media_type(p) == "image/jpeg"
        p.write_bytes(b"\x89PNG\r\n\x1a\n")
        assert _sniff_generated_media_type(p) == "image/png"
        p.write_bytes(b"RIFF\x00\x00\x00\x00WEBPVP8 ")
        assert _sniff_generated_media_type(p) == "image/webp"
        p.write_bytes(b"unknown-bytes")
        assert _sniff_generated_media_type(p) is None
