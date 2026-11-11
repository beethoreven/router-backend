"""匯出格式的欄位檢查與逐版轉換(不需要資料庫)。"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from apps.mama_fufood import export_formats
from tests.mama_fufood.conftest import load_sample

CUR = export_formats.CURRENT

SAMPLES = sorted((Path(export_formats.__file__).parent / "samples").glob("v*.json"))


def _record(sample: dict) -> dict:
    return sample["babies"][0]["records"][0]


def test_every_format_version_has_a_sample():
    versions = {int(p.stem[1:]) for p in SAMPLES}
    assert versions == set(export_formats.DEFINITIONS), "每個格式版本都要保留一份樣本檔"


@pytest.mark.parametrize("path", SAMPLES, ids=lambda p: p.stem)
def test_every_sample_converts_to_latest_and_passes(path):
    """所有舊樣本一路轉到最新版,並通過最新版的檢查(SPEC 第 4 節「匯入」)。"""
    version = int(path.stem[1:])
    sample = load_sample(version)
    assert export_formats.validate(sample, version) == []
    if version < export_formats.CURRENT:
        sample, missing = export_formats.convert(sample)
        assert missing == []
    assert sample["format_version"] == export_formats.CURRENT
    assert export_formats.validate(sample) == []


@pytest.mark.parametrize(("mutate", "message"), [
    (lambda d: _record(d).update(id="6F1C2A9E-3B4D-4E8F-9A1B-2C3D4E5F6A7B"), "小寫的 UUID"),
    (lambda d: _record(d).update(id="6f1c2a9e-3b4d-1e8f-9a1b-2c3d4e5f6a7b"), "UUID 第 4 版"),
    (lambda d: _record(d).update(prepared_amount=1000), "0~999.99"),
    (lambda d: _record(d).update(eaten_amount=1.005), "最多 2 位小數"),
    (lambda d: _record(d).update(eaten_amount=-1), "0~999.99"),
    (lambda d: _record(d).update(eaten_amount=True), "必須是數字"),
    (lambda d: _record(d).update(photos=[f"photos/9d8e7f6a-5b4c-4d3e-8f2a-1b0c9d8e7f6{i}.jpg" for i in range(4)]),
     "最多 3 個"),
    (lambda d: _record(d).update(photos=["../../etc/passwd"]), "照片路徑"),
    (lambda d: _record(d).update(time="2026-10-23T03:30:15Z"), "時間格式"),
    (lambda d: _record(d).update(time="2026-02-30T03:30:00Z"), "不是存在的日期時間"),
    (lambda d: _record(d).update(tz_offset="+15:00"), "超出範圍"),
    (lambda d: _record(d).update(tz_offset="+08:10"), "超出範圍"),
    (lambda d: _record(d).update(allergy="maybe"), "只能是"),
    (lambda d: _record(d).update(reaction=None, allergy=None, note="a" * 501), "最多 500 個字"),
    (lambda d: _record(d).update(extra=1), "不認得的欄位"),
    (lambda d: _record(d).pop("note"), "缺少這個欄位"),
    (lambda d: _record(d).update(note=None), "不能是 null"),
    (lambda d: d["babies"][1].update(name="小寶"), "name 不能重複"),
    (lambda d: d["babies"][1].update(seq=1), "seq 不能重複"),
    (lambda d: d["babies"][0].update(name=""), "至少 1 個字"),
    (lambda d: d["babies"][0].update(name="字" * 21), "最多 20 個字"),
    (lambda d: d["settings"].update(week_start="saturday"), "只能是"),
    (lambda d: d.update(babies=[]), "至少要有 1 個"),
    (lambda d: d["babies"][0]["records"].append(copy.deepcopy(_record(d))), "在檔案裡重複出現"),
    (lambda d: d.update(format_version=CUR + 1), f"必須是 {CUR}"),
])
def test_invalid_files_are_rejected(mutate, message):
    sample = load_sample(CUR)
    mutate(sample)
    errors = export_formats.validate(sample, CUR)
    assert any(message in e for e in errors), errors


def test_names_count_what_the_user_sees():
    """一個 emoji(包含組合起來的家庭 emoji)算一個字。"""
    sample = load_sample(CUR)
    sample["babies"][0]["name"] = "👨‍👩‍👧" * 20
    assert export_formats.validate(sample, CUR) == []
    sample["babies"][0]["name"] = "👨‍👩‍👧" * 21
    assert any("最多 20 個字" in e for e in export_formats.validate(sample, CUR))


def test_nullable_choices_and_empty_records_are_fine():
    sample = load_sample(CUR)
    _record(sample).update(allergy=None, reaction=None)
    assert export_formats.validate(sample, CUR) == []


def test_convert_runs_every_step_in_order(monkeypatch):
    """逐版轉換:一版一版往上轉(例如 1→2→3),每一步都要有轉換程式。"""
    calls = []

    def one_to_two(data):
        calls.append(1)
        return data, []

    def two_to_three(data):
        calls.append(2)
        return data, [{"path": "settings.consent", "field": "consent"}]

    monkeypatch.setattr(export_formats, "CURRENT", CUR + 2)
    monkeypatch.setitem(export_formats.CONVERTERS, CUR, one_to_two)
    monkeypatch.setitem(export_formats.CONVERTERS, CUR + 1, two_to_three)
    data, missing = export_formats.convert(load_sample(CUR))
    assert calls == [1, 2]
    assert data["format_version"] == CUR + 2
    assert missing == [{"path": "settings.consent", "field": "consent"}]


def test_convert_without_a_converter_fails_loudly(monkeypatch):
    monkeypatch.setattr(export_formats, "CURRENT", CUR + 1)
    with pytest.raises(ValueError, match=f"缺少第 {CUR} 版轉第 {CUR + 1} 版"):
        export_formats.convert(load_sample(CUR))


def test_v0_to_v1_adds_an_empty_digestive_field():
    """第 0 版(上架前)沒有「腹瀉或嘔吐」:轉成第 1 版時每筆紀錄補上 null(沒填),其他欄位不變。"""
    old = load_sample(0)
    new, missing = export_formats.convert(copy.deepcopy(old))
    assert missing == [] and new["format_version"] == 1
    for old_baby, new_baby in zip(old["babies"], new["babies"]):
        for before, after in zip(old_baby["records"], new_baby["records"]):
            assert after["digestive"] is None
            assert {k: v for k, v in after.items() if k != "digestive"} == before
            assert list(after).index("digestive") == list(after).index("reaction") + 1
    assert export_formats.validate(new, 1) == []
    # 第 1 版一定要有這個欄位(值可以是 null),也只能是固定的幾個值。
    del new["babies"][0]["records"][0]["digestive"]
    assert any("缺少這個欄位" in e for e in export_formats.validate(new, 1))
    _record(new).update(digestive="fever")
    assert any("只能是" in e for e in export_formats.validate(new, 1))

