"""業務邏輯層:知道 Google Sheet 裡的資料代表什麼意義(1stDayMorning 是第一天早晨、A~J 欄各是什麼)。

只透過 google_access.sheet_batch_read 讀資料,不直接碰 gspread。
"""

from __future__ import annotations

from . import config, google_access

# Morning 型態頁籤(1stDayMorning ~ 7thDayMorning,以及 FinalDayResult):H 欄空白,I=持有金錢,J=當前積分
MORNING_FORMAT = ["角色", "代表組織", "正當事業", "闇金", "色情", "毒品", "軍火", None, "持有金錢", "當前積分"]

# 一般 Report 頁籤(1stDayReport ~ 7thDayReport):H=應得收入,I=應持金錢,J=預期積分。
# I/J 用字刻意跟 Morning 不同:這裡是估算值,不是已確定的當下數字。
REPORT_FORMAT = ["角色", "代表組織", "正當事業", "闇金", "色情", "毒品", "軍火", "應得收入", "應持金錢", "預期積分"]

# Sheet 上的角色姓名 -> API 用的英文 key(不受 oniwara_out/mike_out/kouno_single 轉換影響)
CHARACTER_SLUGS = {
    "鬼原響介": "oniwara",
    "麥克・葛雷希爾": "mike",
    "鬼怒川新助": "kinugawa",
    "河野麗一": "kouno",
    "PH-003": "ph003",
}

# Demo 的 Sheet 用通用角色名,slug 跟正式版共用,業務邏輯不用另外一套
DEMO_CHARACTER_SLUGS = {
    "Character A": "oniwara",
    "Character B": "mike",
    "Character C": "kinugawa",
    "Character D": "kouno",
    "Character E": "ph003",
}

BUSINESS_KEY_MAP = {
    "正當事業": "general_business",
    "闇金": "finance",
    "色情": "sex",
    "毒品": "drug",
    "軍火": "arms",
}

ILLEGAL_BUSINESS_KEYS = ["finance", "sex", "drug", "arms"]

# 「風頭事業」平手時的優先序:毒品 > 金融(闇金)> 軍火 > 色情
HOT_BUSINESS_PRIORITY = ["drug", "finance", "arms", "sex"]

GLOBAL_PARAM_SHEET = "Global_Param"
ORIGINAL_STATUS_SHEET = "OriginalStatus"


def validate_round_params(day: str, type: str):
    """驗證 day/type,回傳正規化後的值;不合法丟 ValueError(路由回 400)。"""
    if day not in config.VALID_DAYS:
        raise ValueError(f"day 參數不合法:{day},合法值為 {config.VALID_DAYS}")

    # 不分大小寫:瀏覽器網址列自動完成可能把大寫改成小寫
    normalized_type = type.capitalize()
    if normalized_type not in config.VALID_TYPES:
        raise ValueError(f"type 參數不合法:{type},合法值為 {config.VALID_TYPES}")

    if day in config.DAYS_WITHOUT_MORNING and normalized_type == "Morning":
        raise ValueError(f"{day} 沒有 Morning 回合,只有 Report")

    return day, normalized_type


def _resolve_sheet_name(day: str, type: str):
    # Final 的頁籤叫 FinalDayResult,資料結構跟 Morning 一樣(H 欄空白),刻意跟 Report 區隔
    if day in config.DAYS_WITHOUT_MORNING:
        return "FinalDayResult"
    return f"{day}Day{type}"


def _resolve_format(day: str, type: str):
    if type == "Morning" or day in config.DAYS_WITHOUT_MORNING:
        return MORNING_FORMAT
    return REPORT_FORMAT


def _to_int(value):
    """儲存格顯示值轉 int(空字串為 0,去掉千分位與空白)。

    不是數字時丟 RuntimeError 而不是 ValueError:路由把 ValueError 當成參數錯誤(400),
    儲存格內容有問題是資料面的問題。
    """
    if value in ("", None):
        return 0

    normalized = str(value).strip().replace(",", "")
    try:
        return int(normalized)
    except ValueError as e:
        raise RuntimeError(f"儲存格數值無法轉成數字:{value!r}") from e


def _cell_value(batch_values, idx, default=""):
    rows = batch_values[idx]
    if rows and rows[0]:
        return rows[0][0]
    return default


def _read_round_batch(sheet_name: str, spreadsheet_id: str):
    """一次讀完 /round 需要的所有原始資料(只打一次 Sheets API)。

    這些值遊戲中隨時會被主持人手動改,所以每次都重讀、不快取。
    """
    ranges = [
        f"'{sheet_name}'!A1",
        f"'{sheet_name}'!A4:J8",
        f"{GLOBAL_PARAM_SHEET}!B3",
        f"{GLOBAL_PARAM_SHEET}!B4",
        f"{GLOBAL_PARAM_SHEET}!B5",
        f"{ORIGINAL_STATUS_SHEET}!M3",
        f"{ORIGINAL_STATUS_SHEET}!M4",
        f"{ORIGINAL_STATUS_SHEET}!M5",
        f"{GLOBAL_PARAM_SHEET}!B10",
    ]
    batch = google_access.sheet_batch_read(ranges, spreadsheet_id)

    return {
        "round_label": _cell_value(batch, 0),
        "rows": batch[1],
        "oniwara_out": _cell_value(batch, 2) == "是",
        "mike_out": _cell_value(batch, 3) == "是",
        "kouno_single": _cell_value(batch, 4) == "是",
        "legal_basic": _to_int(_cell_value(batch, 5, "0")),
        "illegal_basic": _to_int(_cell_value(batch, 6, "0")),
        "broken_target": _to_int(_cell_value(batch, 7, "0")),
        "kiyoshiro_escape": _cell_value(batch, 8) == "是",
    }


def _rows_to_business_level(rows, format_fields, is_report_format: bool, kouno_single: bool, character_slugs=None):
    """A4:J8 原始列 -> 英文 key 格式。

    kouno_single=False 時,河野麗一當次的積分設為 None、併加到 PH-003;金錢欄位不受影響。
    """
    if character_slugs is None:
        character_slugs = CHARACTER_SLUGS

    business_level = {}
    for row in rows:
        char_name = row[0] if row else ""
        slug = character_slugs.get(char_name)
        if slug is None:
            continue

        entry = {"organization": row[1] if len(row) > 1 else ""}
        for idx, field_name in enumerate(format_fields):
            if field_name is None or idx in (0, 1):
                continue
            value = row[idx] if idx < len(row) else ""
            if field_name in BUSINESS_KEY_MAP:
                entry[BUSINESS_KEY_MAP[field_name]] = _to_int(value)
            elif field_name == "持有金錢":
                entry["owned_money"] = _to_int(value)
            elif field_name == "當前積分":
                entry["current_integral"] = _to_int(value)
            elif field_name == "應得收入":
                entry["expected_income"] = _to_int(value)
            elif field_name == "應持金錢":
                entry["expected_money"] = _to_int(value)
            elif field_name == "預期積分":
                entry["expected_integral"] = _to_int(value)

        business_level[slug] = entry

    integral_field = "expected_integral" if is_report_format else "current_integral"
    if not kouno_single and "kouno" in business_level and "ph003" in business_level:
        kouno_value = business_level["kouno"].get(integral_field, 0)
        business_level["ph003"][integral_field] = (
            business_level["ph003"].get(integral_field, 0) + kouno_value
        )
        business_level["kouno"][integral_field] = None

    return business_level


def _compute_business_totals(business_level, oniwara_out: bool, legal_basic: int, illegal_basic: int):
    """合法/非法事業總和;oniwara_out=True 時鬼原商事洗白,五項全算合法。"""
    legal_total = legal_basic
    illegal_total = illegal_basic

    for slug, entry in business_level.items():
        if slug == "oniwara" and oniwara_out:
            legal_total += entry["general_business"]
            for key in ILLEGAL_BUSINESS_KEYS:
                legal_total += entry[key]
        else:
            legal_total += entry["general_business"]
            for key in ILLEGAL_BUSINESS_KEYS:
                illegal_total += entry[key]

    return legal_total, illegal_total


def _compute_hot_business(business_level, oniwara_out: bool):
    """四類非法事業加總最高者(oniwara_out=True 時排除鬼原響介),同分依 HOT_BUSINESS_PRIORITY。"""
    totals = {key: 0 for key in ILLEGAL_BUSINESS_KEYS}
    for slug, entry in business_level.items():
        if slug == "oniwara" and oniwara_out:
            continue
        for key in ILLEGAL_BUSINESS_KEYS:
            totals[key] += entry[key]

    max_value = max(totals.values())
    if max_value == 0:
        # 沒有人經營非法事業(或資料整批沒讀到):風頭事業不存在,不能讓優先序挑出毒品
        return None

    for key in HOT_BUSINESS_PRIORITY:
        if totals[key] == max_value:
            return key
    return None


def build_round_response(day: str, type: str, spreadsheet_id: str, character_slugs=None):
    """組出 /round 的完整回應。day/type 要先經過 validate_round_params。"""
    sheet_name = _resolve_sheet_name(day, type)
    format_fields = _resolve_format(day, type)
    is_report_format = format_fields is REPORT_FORMAT

    batch = _read_round_batch(sheet_name, spreadsheet_id)

    business_level = _rows_to_business_level(
        batch["rows"], format_fields, is_report_format, batch["kouno_single"], character_slugs
    )
    legal_business, illegal_business = _compute_business_totals(
        business_level, batch["oniwara_out"], batch["legal_basic"], batch["illegal_basic"]
    )
    hot_business = (
        _compute_hot_business(business_level, batch["oniwara_out"])
        if batch["kiyoshiro_escape"]
        else None
    )

    return {
        "day": day,
        "type": type,
        "round_label": batch["round_label"],
        "oniwara_out": batch["oniwara_out"],
        "mike_out": batch["mike_out"],
        "kouno_single": batch["kouno_single"],
        "legal_business": legal_business,
        "illegal_business": illegal_business,
        "broken_target": batch["broken_target"],
        "business_level": business_level,
        "kiyoshiro_escape": batch["kiyoshiro_escape"],
        "hot_business": hot_business,
    }
