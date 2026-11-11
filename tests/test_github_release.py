"""Font upload streaming to the GitHub Release: hashing on the way through, and the checks."""

from __future__ import annotations

import gzip
import hashlib
import io

import pytest

from apps.diary import github_release


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
        sent["bodies"].append(b"".join(data))  # consume the generator like requests would
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
    assert fake_github["headers"]["Content-Length"] == str(len(gz))
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
