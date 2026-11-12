"""Font upload streaming to the GitHub Release: hashing on the way through, and the checks."""

from __future__ import annotations

import gzip
import hashlib
import io

import pytest

from apps.all_mo_diary import github_release


class _Resp:
    def __init__(self, status, data):
        self.status_code = status
        self._data = data

    def json(self):
        return self._data


@pytest.fixture
def fake_github(monkeypatch):
    sent = {"bodies": [], "deleted": []}

    def get(url, headers, timeout):
        return _Resp(200, {"id": 7})

    def post(url, params=None, headers=None, data=None, timeout=None):
        parts = []
        while chunk := data.read(8192):  # read the body the way http.client does
            parts.append(chunk)
        sent["bodies"].append(b"".join(parts))
        sent["headers"] = headers
        return _Resp(201, {"id": 99, "browser_download_url": f"https://example/{params['name']}"})

    def delete(url, headers, timeout):
        sent["deleted"].append(url)
        return _Resp(204, {})

    monkeypatch.setattr(github_release.requests, "get", get)
    monkeypatch.setattr(github_release.requests, "post", post)
    monkeypatch.setattr(github_release.requests, "delete", delete)
    return sent


def _upload(data: bytes):
    return github_release.upload(io.BytesIO(data), len(data), 3, "owner/fonts", "token")


def test_a_gzipped_font_is_streamed_and_hashed(fake_github):
    font = b"OTTO" + bytes(range(256)) * 5000
    gz = gzip.compress(font)
    info = _upload(gz)
    assert fake_github["bodies"][0] == gz
    assert "Content-Length" not in fake_github["headers"]  # requests sets it from len(body)
    assert info["download_sha256"] == hashlib.sha256(gz).hexdigest()
    assert info["font_sha256"] == hashlib.sha256(font).hexdigest()
    assert info["font_bytes"] == len(font) and info["download_bytes"] == len(gz)
    assert info["file_name"].startswith("font-3-") and info["file_name"].endswith(".otf.gz")


def test_not_gzip_is_refused(fake_github):
    with pytest.raises(github_release.UploadError, match="gzip"):
        _upload(b"OTTO" + b"x" * 1000)


def test_gzip_but_not_a_font_is_refused_and_removed(fake_github):
    with pytest.raises(github_release.UploadError, match="不是字體檔"):
        _upload(gzip.compress(b"<html>not a font</html>" * 100))


def test_truncated_gzip_is_refused_and_removed(fake_github):
    gz = gzip.compress(b"OTTO" + b"y" * 100000)
    with pytest.raises(github_release.UploadError, match="不完整"):
        _upload(gz[: len(gz) // 2])
    assert fake_github["deleted"], "the incomplete asset must be deleted from the release"


def test_not_configured():
    with pytest.raises(github_release.UploadError) as exc:
        github_release.upload(io.BytesIO(b"x"), 1, 1, "", "")
    assert exc.value.status == 503


def test_real_http_request_has_one_plain_content_length(monkeypatch):
    """Against a real HTTP server (not a mocked requests): GitHub drops a request that carries both
    Content-Length and Transfer-Encoding, which is what the first real upload did."""
    import http.server
    import threading

    seen = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            seen["length"] = self.headers.get("Content-Length")
            seen["te"] = self.headers.get("Transfer-Encoding")
            seen["body"] = self.rfile.read(int(self.headers["Content-Length"]))
            out = b'{"id": 99, "browser_download_url": "https://example/x"}'
            self.send_response(201)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(github_release, "UPLOADS", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setattr(github_release, "_release_id", lambda repo, token: 7)
    font = b"OTTO" + bytes(range(256)) * 4000
    gz = gzip.compress(font)
    try:
        info = _upload(gz)
    finally:
        server.shutdown()
    assert seen["te"] is None
    assert seen["length"] == str(len(gz))
    assert seen["body"] == gz
    assert info["font_sha256"] == hashlib.sha256(font).hexdigest()
