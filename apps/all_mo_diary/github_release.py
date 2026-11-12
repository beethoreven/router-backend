"""Streams an uploaded font (.otf.gz from tools/reduce_font.py) to the fonts repository's GitHub
Release, checking and hashing it on the way through.

- Nothing is held in memory or on disk: the request body is read in 256 KiB chunks and each chunk is
  hashed, decompressed (only to hash and size the font inside) and forwarded to GitHub as it arrives.
  Render has 512 MB and the largest font is ~40 MB compressed.
- Checked before anything is published: the decompressed file starts like a font (OpenType/TrueType
  signature), the decompressed size is at most MAX_FONT_BYTES, and the gzip stream is complete. A file
  that fails any check is deleted from the release again.
- Every upload gets a new asset name (font id + hash prefix), so an existing file is never replaced
  under phones that installed it.
- The GitHub token (DIARY__GITHUB_TOKEN) should be a fine-grained token with Contents: read & write on
  the fonts repository only.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import zlib
from typing import Iterator

import requests

log = logging.getLogger(__name__)

API = "https://api.github.com"
UPLOADS = "https://uploads.github.com"
# One release per project: the fonts repo (general-fonts) is shared with other projects.
TAG = "all-mo-diary"
CHUNK = 256 * 1024
MAX_FONT_BYTES = 120 * 1024 * 1024
_SIGNATURES = (b"OTTO", b"\x00\x01\x00\x00", b"true", b"typ1")


class UploadError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"}


def _release_id(repo: str, token: str) -> int:
    """The release that holds the font files (tag "all-mo-diary"); created the first time."""
    resp = requests.get(f"{API}/repos/{repo}/releases/tags/{TAG}", headers=_headers(token), timeout=20)
    if resp.status_code == 200:
        return int(resp.json()["id"])
    if resp.status_code != 404:
        raise UploadError(f"GitHub 回應錯誤（{resp.status_code}），請確認 DIARY__FONTS_REPO 與 DIARY__GITHUB_TOKEN", 502)
    resp = requests.post(f"{API}/repos/{repo}/releases", headers=_headers(token), timeout=20,
                         data=json.dumps({"tag_name": TAG, "name": "全心日記", "body": "全心日記的字體檔（由後台上傳）"}))
    if resp.status_code not in (200, 201):
        raise UploadError(f"無法建立 GitHub Release（{resp.status_code}）", 502)
    return int(resp.json()["id"])


class _Hasher:
    """Hashes the compressed stream and the font inside it while chunks pass through."""

    def __init__(self) -> None:
        self.download_sha = hashlib.sha256()
        self.font_sha = hashlib.sha256()
        self.download_bytes = 0
        self.font_bytes = 0
        self._inflate = zlib.decompressobj(16 + zlib.MAX_WBITS)  # gzip wrapper
        self._head = b""

    def feed(self, chunk: bytes) -> None:
        self.download_sha.update(chunk)
        self.download_bytes += len(chunk)
        try:
            out = self._inflate.decompress(chunk, CHUNK * 8)
            while out:
                self._font(out)
                out = self._inflate.decompress(self._inflate.unconsumed_tail, CHUNK * 8) \
                    if self._inflate.unconsumed_tail else b""
        except zlib.error as exc:
            raise UploadError("不是 gzip 壓縮的檔案（請上傳字體瘦身工具產生的 .otf.gz）") from exc

    def _font(self, data: bytes) -> None:
        if len(self._head) < 4:
            self._head += data[:4 - len(self._head)]
            if len(self._head) == 4 and self._head not in _SIGNATURES:
                raise UploadError("解壓後不是字體檔（OTF/TTF）")
        self.font_sha.update(data)
        self.font_bytes += len(data)
        if self.font_bytes > MAX_FONT_BYTES:
            raise UploadError(f"字體檔解壓後超過 {MAX_FONT_BYTES // 1024 // 1024} MB", 413)

    def finish(self) -> None:
        if not self._inflate.eof:
            raise UploadError("檔案不完整（gzip 沒有結尾），請重新上傳")
        if len(self._head) < 4:
            raise UploadError("解壓後不是字體檔（OTF/TTF）")


class _Body:
    """The request body: reads `length` bytes from the incoming upload, checking and hashing them on
    the way. A file-like object with a length, so requests sends a plain Content-Length body."""

    def __init__(self, stream, length: int, hasher: "_Hasher") -> None:
        self._stream = stream
        self._remaining = length
        self._length = length
        self._hasher = hasher
        self.failure: UploadError | None = None

    def __len__(self) -> int:
        return self._length

    def read(self, size: int = -1) -> bytes:
        if self._remaining <= 0:
            return b""
        want = self._remaining if size is None or size < 0 else min(size, self._remaining)
        chunk = self._stream.read(min(want, CHUNK))
        if not chunk:
            # The browser stopped sending early: stop, so GitHub isn't left waiting for the rest.
            self.failure = UploadError("檔案沒有完整傳到後端，請重新上傳")
            raise self.failure
        self._remaining -= len(chunk)
        try:
            self._hasher.feed(chunk)
        except UploadError as exc:
            self.failure = exc
            raise
        return chunk


def upload(stream, length: int, font_id: int, repo: str, token: str) -> dict:
    """Uploads `length` bytes from `stream` as a new release asset. Returns the info fonts.set_file() takes."""
    if not repo or not token:
        raise UploadError("後端還沒設定字體的 GitHub repo（DIARY__FONTS_REPO、DIARY__GITHUB_TOKEN）", 503)
    if length <= 0:
        raise UploadError("沒有收到檔案")
    release = _release_id(repo, token)
    hasher = _Hasher()
    name = f"font-{font_id}-{secrets.token_hex(4)}.otf.gz"
    body = _Body(stream, length, hasher)
    # No Content-Length header here: requests takes it from len(body). Passing a generator with an
    # explicit Content-Length made requests add "Transfer-Encoding: chunked" as well, and GitHub drops
    # a request carrying both (seen as 「傳到 GitHub 時連線失敗」 on the first real upload).
    headers = {**_headers(token), "Content-Type": "application/gzip"}
    try:
        resp = requests.post(f"{UPLOADS}/repos/{repo}/releases/{release}/assets", params={"name": name},
                             headers=headers, data=body, timeout=(20, 600))
    except UploadError:
        raise  # the file failed a check part-way: sending stopped there
    except requests.RequestException as exc:
        if body.failure:
            raise body.failure from exc
        log.warning("font %s upload to GitHub failed: %r", font_id, exc)
        raise UploadError("傳到 GitHub 時連線失敗，請再試一次", 502) from exc
    failure = [body.failure] if body.failure else []
    if failure:
        _delete_asset(resp, repo, token)
        raise failure[0]
    if resp.status_code not in (200, 201):
        raise UploadError(f"GitHub 拒絕了這個檔案（{resp.status_code}）", 502)
    try:
        hasher.finish()
    except UploadError:
        _delete_asset(resp, repo, token)
        raise
    asset = resp.json()
    log.info("font %s uploaded as %s (%d bytes)", font_id, name, hasher.download_bytes)
    return {
        "file_name": name,
        "download_url": asset["browser_download_url"],
        "download_bytes": hasher.download_bytes,
        "download_sha256": hasher.download_sha.hexdigest(),
        "font_bytes": hasher.font_bytes,
        "font_sha256": hasher.font_sha.hexdigest(),
    }


def _delete_asset(resp, repo: str, token: str) -> None:
    try:
        asset_id = resp.json().get("id") if resp is not None and resp.status_code in (200, 201) else None
        if asset_id:
            requests.delete(f"{API}/repos/{repo}/releases/assets/{asset_id}", headers=_headers(token), timeout=20)
    except Exception:  # noqa: BLE001 — best effort; a stray asset is harmless (never listed)
        log.warning("could not delete rejected font asset")
