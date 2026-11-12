# router-backend

好幾個 App 共用的一個後端服務（Render 免費方案、單一 worker）。每個 App 掛在自己的網址前綴底下：

```
https://router-backend-3hl2.onrender.com
  /health            保活用，不載入任何 app、不碰資料庫（每 10 分鐘由外部排程打一次）
  /ready             每個 app 載不載得起來（不碰資料庫）
  /diary/...         全心日記（apps/all_mo_diary）
  /mama-fufood/...   媽媽記副食品（apps/mama_fufood）
  /mama-stocker/...   LINE 股票資訊 bot（apps/mama_stocker）
  /fireless-war/...   願浮沉2：無火戰爭（apps/fireless_war）
```

## 目錄

```
wsgi.py               gunicorn 進入點
router/
  root.py             MOUNTS（有哪些 app）、/health、/ready
  dispatcher.py       每個 app 延遲載入；載入失敗只有那個前綴回 503
  settings.py         讀 <APP>__ 前綴的環境變數；設定寫錯服務直接起不來
shared/               各 app 共用的安全機制與基礎設施（見下表）
apps/<套件>/          每個 App 一個套件，進入點是 create_app(settings)
tests/                router 與 shared 的測試；tests/<套件>/ 是各 app 自己的
```

## 共用的部分（`shared/`）

API 每個 App 不同，留在各自的 app；跟 API 沒有直接關係的安全機制與基礎設施只寫一份：

| 模組 | 做什麼 |
|---|---|
| `google_auth` | 驗 Google ID Token：形狀與 `kid` 先擋、Google 公鑰快取、抓公鑰 10 秒逾時；「無效」回 None、「Google 連不上」丟 `VerificationUnavailable`（回 503，不登出） |
| `app_pass` | App 自己的無狀態登入通行證（HMAC、180 天、30 天後換新） |
| `admin_auth` | 管理後台：session（不透明 token＋HMAC 標籤、滑動到期）、管理員名單、`require_admin` 門 |
| `database` | 每次開一條連線、每個 app 與整個行程兩層上限、逾時、advisory lock 下建表一次 |
| `payload` | 請求大小上限、gzip 解壓上限（防壓縮炸彈） |
| `quota` | 每人每天的次數（記憶體、台北時間） |
| `rejections` | 被拒絕的請求只計數，每分鐘一行彙總 |
| `snapshot_cache` | 公用資料的記憶體副本，熱路徑不碰資料庫 |
| `flask_app` | `new_app`：統一的 JSON 錯誤、大小上限；`allow_cors`：只收精確的來源 |
| `timeutil`、`textutil` | 現在時間、台北的「今天」、版本戳記；字素計數 |

只有一個 app 用到的東西留在那個 app，就算跟安全有關也一樣（例如 LINE webhook 簽章）。等第二個 app 也要用，再搬進 `shared/`。`shared/` 的改動會同時影響每個 app，要單獨改、跑過每個 app 的測試。

## 開發規則

測試會檢查的：

- app 裡的 import 一律用相對路徑（`from . import db`）或 `apps.<套件>.…`，不能寫裸的 `import db`、`import config`：在同一個行程裡，兩個 app 的同名模組會互相蓋掉。
- app 不讀 `os.environ`／`os.getenv`，也不呼叫 `load_dotenv`。設定只從 `create_app(settings)` 拿到的 `AppSettings` 讀，環境變數都有前綴（例如 `MAMA_FUFOOD__DATABASE_URL`）。
- app 不 import 別的 app。`shared/` 不 import 任何 app、不讀環境變數、模組層不放可變的狀態。

測試抓不到的：

- import 與 `create_app()` 期間不開執行緒、不對外連線、不連資料庫。背景工作在第一個請求時（`before_request`）才開始。`create_app` 在 app 的載入鎖裡跑，卡住會讓這個 app 的所有請求排隊。
- 對外連線一律設逾時。所有 app 共用 16 條執行緒，一個卡住的連線就佔走一條。
- 只能一個 worker（記憶體裡的每日上限與快取）。
- 不印、不記秘密與使用者寫的內容；錯誤 log 只記例外的型別與位置。不保存 IP（gunicorn 的 access log 在 `router/root.py` 關掉）。
- 回應格式一律 `{"error": 給人看的訊息, "code": 代碼}`（`shared.flask_app.new_app` 已經統一）。
- 程式、註解、測試、README 與提交訊息只寫跟程式有關的內容與技術理由。

## 加入一個新的 app

1. **名字**：用 App 的完整名稱，網址前綴與環境變數前綴都由它決定：`mama-fufood` → `/mama-fufood`、`MAMA_FUFOOD__…`。Python 套件名稱不能有連字號，所以資料夾用底線：`Mount("mama-fufood", "apps.mama_fufood.app:create_app")`。
2. **程式**放到 `apps/<套件>/`，import 全部改成相對路徑。
3. **設定**：仿照 `apps/all_mo_diary/config.py`，寫一個 frozen dataclass，用 `from_settings(settings)` 讀值，在 `create_app` 裡 `set_current`。不要在程式裡寫死預設值（client ID、試算表 ID、來源網域）。秘密欄位加 `repr=False`。秘密與每個 app 各一份的設定名稱，加進 `router/settings.py` 的 `PER_APP_KEYS`。
4. **改用 `shared/`**：參考 `apps/all_mo_diary/auth.py`、`db.py`、`cache.py`、`app.py`。已經發出去的 token 要繼續有效時，`prefix`、`purpose`、資料表名稱、`schema_lock` 都要沿用原本的值，秘密也要同一個。
5. **create_app(settings)**：用 `shared.flask_app.new_app` 建 app；要跨來源就用 `allow_cors`，來源從設定讀。不要自己定義 `/health`，那是整台的保活。要有自己的 `/ready` 可以，但不能碰資料庫。
6. **定時的背景工作**：保活只打 router 的 `/health`，不會觸發 app 的 `before_request`。需要定時跑的工作，app 自己開一個 `GET /<app>/warm`：請求本身不碰資料庫、不對外連線，只回報狀態，靠它觸發 `before_request`；在 `before_request` 裡用時間間隔加上 non-blocking lock，保證同時間只有一條背景執行緒，再用外部排程定時打它。別人也打得到這個網址，所以頻率的上限要由間隔本身保證。
7. **測試**放在 `tests/<套件>/`，有自己的 `conftest.py`；要資料庫就用 `empty_database` fixture。
8. **套件**：缺的加進 `requirements.txt`，用 PyPI 最新正式版，旁邊寫用途。
9. **推之前**：`git pull --rebase`，然後跑**全部**的測試（見「本機」）。推到 main 就會部署整個服務。
10. **切換**：在 Render 設好 `<APP>__…` 環境變數 → 確認 `/ready` 裡這個 app 是 `loaded: true` → 換用戶端的網址 → 確認沒問題後才關舊服務。資料庫要跟 Render 同一區（新加坡）。

## 本機

```bash
python3.15 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
tools/local_db.sh start                       # 本機 Postgres 18（port 5434）
.venv/bin/python -m router.root                # http://localhost:5010
TEST_DATABASE_URL=postgresql://postgres@localhost:5434/amd_test .venv/bin/python -m pytest
```

測試開始前會清空測試資料庫的整個 public schema。兩個人（或兩個視窗）同時跑測試時，各用自己的資料庫（例如 `createdb -h localhost -p 5434 -U postgres mytest`），`TEST_DATABASE_URL` 指向它。

本機設定寫在 `router-backend/.env`（不進版本控制），變數名稱一樣要有前綴。

## 部署

Render Web Service，start command：`gunicorn wsgi:app --workers 1 --threads 16 --timeout 120`。推到 main 就會部署。設定檢查不過時新版起不來，Render 繼續跑舊版。這個服務要單獨放在一個 Render workspace：750 小時的額度是整個 workspace 共用的。
