# router-backend

好幾個私人專案共用的一個後端服務（Render 免費方案、單一 worker）。每個專案是一個 app，掛在自己的網址前綴底下：

```
https://router-backend-3hl2.onrender.com
  /health            保活用，不載入任何 app、不碰資料庫（cron-job.org 每 10 分鐘打一次）
  /ready             每個 app 載不載得起來（不碰資料庫）
  /diary/...         全心日記（apps/diary）
```

為什麼合併、取捨與待辦：all-mo-diary 的 `docs/router-backend-plan.md`。改程式的規則：`CLAUDE.md`。

## 目錄

```
wsgi.py               gunicorn 進入點
router/
  root.py             MOUNTS（有哪些 app）、/health、/ready
  dispatcher.py       每個 app 延遲載入；載入失敗只有那個前綴回 503
  settings.py         讀 <APP>__ 前綴的環境變數；設定寫錯服務直接起不來
shared/               各 app 共用的安全機制與基礎設施（見下表）
apps/<套件>/          每個專案一個套件，進入點是 create_app(settings)
tests/                router 與 shared 的測試；tests/<套件>/ 是各 app 自己的
```

## 共用的部分（`shared/`）

API 每個專案不同，留在各自的 app；跟 API 沒有直接關係的安全機制與基礎設施只寫一份：

| 模組 | 做什麼 | 誰在用 |
|---|---|---|
| `google_auth` | 驗 Google ID Token：形狀與 `kid` 先擋、Google 公鑰快取、抓公鑰 10 秒逾時；「無效」回 None、「Google 連不上」丟 `VerificationUnavailable`（回 503，不登出） | diary、fufood、fireless-war |
| `app_pass` | 我們自己的無狀態登入通行證（HMAC、180 天、30 天後換新）；格式跟各 app 原本發的一模一樣，手機上的舊通行證繼續有效 | diary、fufood |
| `admin_auth` | 管理後台：session（不透明 token＋HMAC 標籤、滑動到期）、管理員名單、`require_admin` 門 | diary、fufood |
| `database` | 每次開一條連線、每個 app 與整個行程兩層上限、逾時、advisory lock 下建表一次 | diary、fufood、stocker |
| `payload` | 請求大小上限、gzip 解壓上限（防壓縮炸彈） | diary、fufood |
| `quota` | 每人每天的次數（記憶體、台北時間） | diary、fufood |
| `rejections` | 被拒絕的請求只計數，每分鐘一行彙總 | diary、fufood |
| `snapshot_cache` | 公用資料的記憶體副本，熱路徑不碰資料庫 | diary、fufood |
| `flask_app` | `new_app`：統一的 JSON 錯誤、大小上限；`allow_cors`：只收精確的來源 | 全部 |
| `timeutil`、`textutil` | 現在時間、台北的「今天」、版本戳記；字素計數 | diary、fufood、stocker |

**不共用、留在 app 裡的**：只有一個 app 用到的東西，就算跟安全有關也一樣，例如 LINE webhook 簽章、走勢圖網址簽章（stocker）、主持人白名單（fireless-war）。等第二個 app 也要用，再搬進 `shared/`。

## 加入 router-backend（給各專案的 session）

1. **先讀 `CLAUDE.md`**，有什麼不清楚就問主掌者 session。
2. **選名字**：網址前綴與環境變數前綴都由它決定。`fufood` → `/fufood`、`FUFOOD__…`；名字有連字號時，Python 套件改成底線：`Mount("fireless-war", "apps.fireless_war.app:create_app")`。
3. **搬程式**：放到 `apps/<套件>/`，要保留歷史就用 `git subtree`。import 全部改成相對路徑。
4. **設定**：仿照 `apps/diary/config.py`，寫一個 frozen dataclass，用 `from_settings(settings)` 讀值，在 `create_app` 裡 `set_current`。拿掉所有 `os.environ`、`load_dotenv`，也拿掉寫死在程式裡的預設值（client ID、試算表 ID、來源網域）。秘密欄位加 `repr=False`。秘密與每個 app 各一份的設定名稱，加進 `router/settings.py` 的 `PER_APP_KEYS`。
5. **換成 shared**：重複的安全機制、基礎設施刪掉，改用 `shared/`。參考 `apps/diary/auth.py`、`db.py`、`cache.py`、`app.py`。已經發出去的 token 要繼續有效時，`prefix`、`purpose`、資料表名稱、`schema_lock` 都要沿用原本的值，秘密也要用同一個。
6. **create_app(settings)**：用 `shared.flask_app.new_app` 建 app；要跨來源就用 `allow_cors`，來源從設定讀。不要自己定義 `/health`：保活打的是 router 的 `/health`。要有自己的 `/ready` 可以，但不能碰資料庫。
7. **背景工作**：只能在 `before_request` 裡開始。注意：保活只打 router 的 `/health`，**不會**觸發你 app 的 `before_request`。需要定時跑的工作，請先跟主掌者討論。
8. **測試**：放在 `tests/<套件>/`，有自己的 `conftest.py`。要資料庫就用 `empty_database` fixture。把原本的測試一起搬過來。
9. **套件版本**：缺的套件加進 `requirements.txt`，用 PyPI 最新正式版；原本釘的舊版不要照抄。
10. **跑全部測試**，`git pull --rebase` 之後再推。
11. **切換**（一次只切一個 app）：在 Render 設好 `<APP>__…` 環境變數 → 確認 `/ready` 裡你的 app 是 `loaded: true` → 換用戶端的網址（App 要發新版、網頁改設定、LINE webhook 改網址）→ 確認沒問題後才關舊服務，關掉之後拿掉舊的保活。資料庫的區域要先查：Neon 跟 Render 要同一區（新加坡）。
12. 跟主掌者回報：做了什麼、改了哪些共用檔案、切換到哪一步。

## 本機

```bash
python3.15 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
tools/local_db.sh start                       # 本機 Postgres 18（port 5434）
.venv/bin/python -m router.root                # http://localhost:5010
TEST_DATABASE_URL=postgresql://postgres@localhost:5434/amd_test .venv/bin/python -m pytest
```

本機設定寫在 `router-backend/.env`（不進版本控制），變數名稱一樣要有前綴。

## 部署

Render Web Service，start command：`gunicorn wsgi:app --workers 1 --threads 16 --timeout 120`。推到 main 就會部署。設定檢查不過時新版起不來，Render 繼續跑舊版。這個服務要單獨放在一個 Render workspace：750 小時的額度是整個 workspace 共用的。
