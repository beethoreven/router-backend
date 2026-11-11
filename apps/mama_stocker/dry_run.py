"""對線上（或本機）的 bot 試跑幾則訊息，印出它本來會回什麼（webhook 的 X-Dry-Run，見 app.py）。

    .venv/bin/python -m apps.mama_stocker.dry_run 台積電 "台積電 利率" "00918 配息 3張"
    .venv/bin/python -m apps.mama_stocker.dry_run --base http://localhost:5010/mama-stocker 台積電

讀 router-backend/.env 的兩個值（這是本機工具，不在服務裡跑）：
    MAMA_STOCKER__LINE_CHANNEL_SECRET   簽 webhook 用（跟線上同一把）
    MAMA_STOCKER__DRY_RUN_USER_ID       假裝是誰在問：要是 users 表裡開通過的 userId，不然只會回「沒有權限」
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import requests
from dotenv import dotenv_values

from apps.mama_stocker.line_utils import signature

BASE = "https://router-backend-3hl2.onrender.com/mama-stocker"
_ENV = dotenv_values(Path(__file__).resolve().parents[2] / ".env")


def ask(text: str, *, base: str = BASE, show_sources: bool = False) -> dict | None:
    secret = _ENV.get("MAMA_STOCKER__LINE_CHANNEL_SECRET") or ""
    user = _ENV.get("MAMA_STOCKER__DRY_RUN_USER_ID") or ""
    raw = json.dumps({"events": [{
        "type": "message", "replyToken": "0" * 32,
        "source": {"type": "user", "userId": user},
        "message": {"type": "text", "id": "1", "text": text},
    }]}).encode()
    started = time.time()
    try:
        r = requests.post(f"{base}/line/webhook", data=raw, timeout=60, headers={
            "X-Line-Signature": signature.sign(raw, secret), "X-Dry-Run": "1"})
        d = r.json()
    except Exception as exc:  # noqa: BLE001
        print(f"▶ {text}  ERR {type(exc).__name__} {time.time() - started:.1f}s")
        return None
    if "dry_run" not in d:
        print(f"▶ {text}  (沒有試跑結果) {r.status_code} {d}")
        return None
    for e in d["dry_run"]:
        rep = e["replies"][0] if e["replies"] else "(沒有回覆)"
        shown = rep if isinstance(rep, str) else json.dumps(rep, ensure_ascii=False)[:90]
        print(f"▶ {text}  [{e['seconds']}s]  " + shown.replace("\n", " / "))
    if show_sources:
        for k, v in d["sources"].items():
            print("    ", k, v)
    return d


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=BASE)
    parser.add_argument("texts", nargs="+")
    args = parser.parse_args()
    for i, t in enumerate(args.texts):
        ask(t, base=args.base, show_sources=(i == len(args.texts) - 1))
