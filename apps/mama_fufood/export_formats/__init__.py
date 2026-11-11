"""匯出格式:欄位定義、檢查與逐版轉換。

- 每個格式版本一份欄位定義(`v{N}.json`),跟著後端程式發布,部署時寫入 formats 表。
- 格式版本 = App 的主版本號 x。目前是第 1 版(App 1.x);第 0 版是上架前的格式(App 0.x)。
- 舊格式轉新格式採**逐版轉換**(1→2→3):每次改格式只新增一支
  「上一版轉這一版」的函式,登記在 CONVERTERS。
- 每個格式版本保留一份樣本檔(`samples/v{N}.json`),測試會把所有舊樣本一路轉到
  最新版並通過檢查(tests/test_formats.py)。

升格式版本(升 x)時要一起改:App 匯出時寫入的格式版本(lib/services/export_format.dart)、
新增 v{N}.json、CONVERTERS 加一支「第 N-1 版轉第 N 版」、新增 samples/v{N}.json,
App 的測試樣本(test/fixtures/)也換成同一份。
"""

from __future__ import annotations

import copy
import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable

from shared.textutil import graphemes

_DIR = Path(__file__).resolve().parent

DEFINITIONS: dict[int, dict] = {
    int(p.stem[1:]): json.loads(p.read_text(encoding="utf-8"))
    for p in sorted(_DIR.glob("v*.json"))
}
CURRENT = max(DEFINITIONS)

def _v0_to_v1(data: dict) -> tuple[dict, list[dict]]:
    """第 0 版(上架前,App 0.x)→ 第 1 版(App 1.0.0):每筆紀錄加上 digestive(腹瀉或嘔吐)。

    第 0 版沒有這個欄位,所以一律是 null(沒填),不納入判斷;使用者之後可以自己補。
    不是法律相關的欄位,不需要使用者在匯入時補資料。
    """
    data = copy.deepcopy(data)
    for baby in data.get("babies", []):
        records = []
        for record in baby.get("records", []):
            converted = {}
            for key, value in record.items():
                converted[key] = value
                if key == "reaction":
                    converted["digestive"] = None
            records.append(converted)
        baby["records"] = records
    return data, []


# from_version → 把 from_version 的 data.json 轉成 from_version + 1 的函式。
# 函式回傳 (轉換後的資料, 缺資料的必填欄位清單)。
CONVERTERS: dict[int, Callable[[dict], tuple[dict, list[dict]]]] = {0: _v0_to_v1}

_UUID4 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_UTC_MINUTE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:00Z$")
_UTC_MILLIS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
_TZ = re.compile(r"^([+-])(\d{2}):(\d{2})$")
_PHOTO = re.compile(r"^photos/[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\.jpg$")
_MAX_ERRORS = 50


class _Checker:
    def __init__(self):
        self.errors: list[str] = []
        self.seen: dict[str, set] = {}

    def fail(self, path: str, message: str) -> None:
        if len(self.errors) < _MAX_ERRORS:
            self.errors.append(f"{path or '（最上層）'}：{message}")

    def check(self, spec: dict, value, path: str, key: str) -> None:
        if value is None:
            if not spec.get("nullable"):
                self.fail(path, "不能是 null")
            return
        kind = spec["type"]
        handler = getattr(self, f"_{kind}")
        handler(spec, value, path)
        if spec.get("unique_in_file") and isinstance(value, str):
            bucket = self.seen.setdefault(key, set())
            if value in bucket:
                self.fail(path, "在檔案裡重複出現")
            bucket.add(value)

    def _object(self, spec, value, path):
        if not isinstance(value, dict):
            self.fail(path, "必須是物件")
            return
        fields = spec["fields"]
        for name in value:
            if name not in fields:
                self.fail(f"{path}.{name}".lstrip("."), "不認得的欄位")
        for name, sub in fields.items():
            sub_path = f"{path}.{name}".lstrip(".")
            if name not in value:
                if sub.get("required", True):
                    self.fail(sub_path, "缺少這個欄位")
                continue
            self.check(sub, value[name], sub_path, name)

    def _array(self, spec, value, path):
        if not isinstance(value, list):
            self.fail(path, "必須是陣列")
            return
        if len(value) < spec.get("min", 0):
            self.fail(path, f"至少要有 {spec['min']} 個")
        if "max" in spec and len(value) > spec["max"]:
            self.fail(path, f"最多 {spec['max']} 個")
        for i, item in enumerate(value):
            self.check(spec["items"], item, f"{path}[{i}]", path.rsplit(".", 1)[-1])
        for key in spec.get("unique", []):
            values = [item.get(key) for item in value if isinstance(item, dict)]
            if len(values) != len(set(map(str, values))):
                self.fail(path, f"{key} 不能重複")

    def _int(self, spec, value, path):
        if isinstance(value, bool) or not isinstance(value, int):
            self.fail(path, "必須是整數")
            return
        if "const" in spec and value != spec["const"]:
            self.fail(path, f"必須是 {spec['const']}")
        if "min" in spec and value < spec["min"]:
            self.fail(path, f"不能小於 {spec['min']}")

    def _string(self, spec, value, path):
        if not isinstance(value, str):
            self.fail(path, "必須是字串")
            return
        n = graphemes(value)
        if n < spec.get("min_chars", 0):
            self.fail(path, f"至少 {spec['min_chars']} 個字")
        if n > spec["max_chars"]:
            self.fail(path, f"最多 {spec['max_chars']} 個字")

    def _enum(self, spec, value, path):
        if value not in spec["values"]:
            self.fail(path, f"只能是 {'、'.join(spec['values'])}")

    def _uuid4(self, spec, value, path):
        if not isinstance(value, str) or not _UUID4.match(value):
            self.fail(path, "必須是小寫的 UUID 第 4 版")

    def _utc(self, value, path, pattern, fmt):
        if not isinstance(value, str) or not pattern.match(value):
            self.fail(path, "時間格式不對")
            return
        try:
            datetime.strptime(value, fmt)
        except ValueError:
            self.fail(path, "不是存在的日期時間")

    def _utc_minute(self, spec, value, path):
        self._utc(value, path, _UTC_MINUTE, "%Y-%m-%dT%H:%M:%SZ")

    def _utc_millis(self, spec, value, path):
        self._utc(value, path, _UTC_MILLIS, "%Y-%m-%dT%H:%M:%S.%fZ")

    def _tz_offset(self, spec, value, path):
        m = _TZ.match(value) if isinstance(value, str) else None
        if not m:
            self.fail(path, "時區格式要是 ±HH:MM")
            return
        minutes = int(m.group(2)) * 60 + int(m.group(3))
        minutes = -minutes if m.group(1) == "-" else minutes
        if not -12 * 60 <= minutes <= 14 * 60 or int(m.group(3)) % 15:
            self.fail(path, "時區超出範圍（-12:00~+14:00）")

    def _amount(self, spec, value, path):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            self.fail(path, "必須是數字")
            return
        try:
            d = Decimal(repr(value)) if isinstance(value, float) else Decimal(value)
        except InvalidOperation:
            self.fail(path, "必須是數字")
            return
        if not Decimal(0) <= d <= Decimal("999.99"):
            self.fail(path, "要在 0~999.99 之間")
        elif d != d.quantize(Decimal("0.01")):
            self.fail(path, "最多 2 位小數")

    def _photo_path(self, spec, value, path):
        if not isinstance(value, str) or not _PHOTO.match(value):
            self.fail(path, "照片路徑要是 photos/{UUID}.jpg")


def validate(data: dict, version: int | None = None) -> list[str]:
    """照第 version 版(沒給就是目前最新版)的欄位定義檢查 data.json。回傳錯誤清單,空的代表通過。

    ★ 預設值不能寫成 `version=CURRENT`:那會在定義函式時就固定下來,之後升版(或測試
      換掉 CURRENT)都還是檢查舊版。
    """
    if version is None:
        version = CURRENT
    if version not in DEFINITIONS:
        return [f"沒有第 {version} 版的格式定義"]
    checker = _Checker()
    checker.check(DEFINITIONS[version]["root"], data, "", "")
    return checker.errors


def convert(data: dict) -> tuple[dict, list[dict]]:
    """把舊格式逐版轉到最新版。回傳(最新版的資料, 缺資料的必填欄位)。

    呼叫前要確定 data 是 dict、format_version 是比 CURRENT 小的已知版本。
    缺資料的必填欄位(例如以後與法律有關的新欄位)由 App 顯示補資料介面;
    目前沒有這種欄位。
    """
    missing: list[dict] = []
    version = data["format_version"]
    while version < CURRENT:
        step = CONVERTERS.get(version)
        if step is None:
            raise ValueError(f"缺少第 {version} 版轉第 {version + 1} 版的轉換程式")
        data, step_missing = step(data)
        missing.extend(step_missing)
        version += 1
        data["format_version"] = version
    return data, missing
