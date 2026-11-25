"""舊格式轉換(無狀態,不需要資料庫)。"""

from __future__ import annotations

import copy
import gzip
import json

from apps.mama_fufood import export_formats
from apps.mama_fufood import routes as public
from tests.mama_fufood.conftest import gz, load_sample, pass_headers

CUR = export_formats.CURRENT
NEXT = CUR + 1


def _post(client, data, headers=None):
    return client.post("/api/v1/convert", data=gz(data), headers={
        **(headers if headers is not None else pass_headers()),
        "Content-Type": "application/json", "Content-Encoding": "gzip", "Accept-Encoding": "gzip"})


def _fake_v2(monkeypatch, missing=None):
    """假裝現在多了下一版:設定多了一個必填欄位,目前這版轉下一版時給預設值。"""
    v2 = copy.deepcopy(export_formats.DEFINITIONS[CUR])
    v2["format_version"] = NEXT
    v2["root"]["fields"]["format_version"]["const"] = NEXT
    v2["root"]["fields"]["settings"]["fields"]["calendar_size"] = {"type": "enum", "values": ["normal", "large"]}

    def one_to_two(data):
        data = copy.deepcopy(data)
        data["settings"]["calendar_size"] = "normal"
        return data, list(missing or [])

    monkeypatch.setitem(export_formats.DEFINITIONS, NEXT, v2)
    monkeypatch.setitem(export_formats.CONVERTERS, CUR, one_to_two)
    monkeypatch.setattr(export_formats, "CURRENT", NEXT)


def test_needs_login(client):
    assert _post(client, load_sample(CUR), headers={}).status_code == 401
    assert public.convert_quota.size() == 0


def test_current_format_needs_no_conversion(client):
    resp = _post(client, load_sample(CUR))
    assert resp.status_code == 409
    assert public.convert_quota.used("user-1") == 0


def test_converts_old_format_stepwise(client, monkeypatch):
    _fake_v2(monkeypatch)
    resp = _post(client, load_sample(CUR))
    assert resp.status_code == 200
    assert resp.headers["Content-Encoding"] == "gzip"
    body = json.loads(gzip.decompress(resp.data))
    assert body["format_version"] == NEXT and body["missing"] == []
    assert body["data"]["settings"]["calendar_size"] == "normal"
    assert export_formats.validate(body["data"], NEXT) == []


def test_missing_required_fields_are_reported(client, monkeypatch):
    _fake_v2(monkeypatch, missing=[{"path": "settings.consent", "field": "consent"}])
    body = json.loads(gzip.decompress(_post(client, load_sample(CUR)).data))
    assert body["missing"] == [{"path": "settings.consent", "field": "consent"}]


def test_invalid_old_file_is_rejected_before_counting(client, monkeypatch):
    _fake_v2(monkeypatch)
    sample = load_sample(CUR)
    sample["babies"][0]["records"][0]["eaten_amount"] = 5000
    resp = _post(client, sample)
    assert resp.status_code == 422
    assert public.convert_quota.used("user-1") == 0


def test_lone_surrogates_are_rejected_before_counting(client):
    """落單的代理字元(JSON 的 "\\ud800" 寫法)轉不成 UTF-8,回應寫不出去:先擋下,不扣次數,也不是 500。"""
    old = load_sample(0)
    old["babies"][0]["name"] = "小\ud800寶"
    old["babies"][0]["records"][0]["note"] = "備\udfff註"
    body = gzip.compress(json.dumps(old).encode("ascii"))  # 標準的 json.dumps 才送得出落單的代理字元
    resp = client.post("/api/v1/convert", data=body, headers={
        **pass_headers(), "Content-Type": "application/json", "Content-Encoding": "gzip", "Accept-Encoding": "gzip"})
    assert resp.status_code == 422, resp.get_json()
    assert resp.get_json()["code"] == "invalid_file"
    assert public.convert_quota.used("user-1") == 0


def test_real_old_file_still_converts(client):
    resp = _post(client, load_sample(0))
    assert resp.status_code == 200
    assert json.loads(gzip.decompress(resp.data))["format_version"] == CUR


def test_five_per_day(client, monkeypatch):
    _fake_v2(monkeypatch)
    for _ in range(5):
        assert _post(client, load_sample(CUR)).status_code == 200
    assert _post(client, load_sample(CUR)).status_code == 429


def test_converter_bug_gives_the_quota_back(client, monkeypatch):
    _fake_v2(monkeypatch)

    def broken(_data):
        raise RuntimeError("bug")

    monkeypatch.setitem(export_formats.CONVERTERS, CUR, broken)
    resp = _post(client, load_sample(CUR))
    assert resp.status_code == 500
    assert public.convert_quota.used("user-1") == 0


def test_uncompressed_or_oversized_bodies(client, monkeypatch):
    _fake_v2(monkeypatch)
    plain = client.post("/api/v1/convert", data=json.dumps(load_sample(CUR)), headers={
        **pass_headers(), "Content-Type": "application/json"})
    assert plain.status_code == 200
    bomb = gzip.compress(b"[" + b"0," * (17 * 1024 * 1024) + b"0]")
    resp = client.post("/api/v1/convert", data=bomb, headers={
        **pass_headers(), "Content-Type": "application/json", "Content-Encoding": "gzip"})
    assert resp.status_code == 413
