"""讀請求內容的共用工具:大小上限與 gzip 解壓(防壓縮炸彈)。"""

from __future__ import annotations

import gzip
import io
import json
import zlib


class PayloadError(ValueError):
    def __init__(self, message: str, status: int = 400, code: str = "bad_request"):
        super().__init__(message)
        self.status = status
        self.code = code


def gunzip_limited(data: bytes, limit: int) -> bytes:
    """解壓 gzip,解出來超過 limit 位元組就停止並拒絕(SPEC 第 10 節:伺服器解壓上限)。"""
    out = io.BytesIO()
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as f:
            while True:
                chunk = f.read(64 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                if out.tell() > limit:
                    raise PayloadError("解壓後超過大小上限", 413, "too_large")
    except (OSError, EOFError, zlib.error) as exc:
        raise PayloadError("不是正確的 gzip 內容") from exc
    return out.getvalue()


def read_raw(request, max_bytes: int) -> bytes:
    """讀請求內容的原始位元組(有上限)。"""
    length = request.content_length
    if length is None or length > max_bytes:
        raise PayloadError("內容太大或沒有標明大小", 413, "too_large")
    raw = request.get_data(cache=False)
    if len(raw) > max_bytes:
        raise PayloadError("內容太大", 413, "too_large")
    return raw


def decode_json(raw: bytes, gzipped: bool, max_decompressed: int | None = None):
    """原始位元組 → JSON。gzipped 時先解壓(有上限)。"""
    if gzipped:
        if max_decompressed is None:
            raise PayloadError("這支 API 不接受壓縮的內容")
        raw = gunzip_limited(raw, max_decompressed)
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PayloadError("內容不是正確的 JSON") from exc


def is_gzipped(request) -> bool:
    return request.headers.get("Content-Encoding", "").lower() == "gzip"


def read_json(request, max_bytes: int, max_decompressed: int | None = None):
    """讀 JSON 內容。帶 Content-Encoding: gzip 時先解壓(有上限)。"""
    return decode_json(read_raw(request, max_bytes), is_gzipped(request), max_decompressed)
