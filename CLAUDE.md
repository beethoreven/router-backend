# router-backend 的規則（每個在這裡改程式的 session 都要遵守）

這個 repo 是好幾個私人專案共用的**一個** Render 服務。任何一個 app 改壞，可能讓所有 app 一起掛，所以下面的規則比單一專案嚴格。

## 誰負責什麼

- **主掌者**：all-mo-diary 的 session `local_c53463eb-860b-4a84-84b4-3b1e7c827ae9`（「Router backend 整合與共用代碼」）。`shared/`、`router/`、`wsgi.py`、`tests/conftest.py`、`tests/test_isolation.py`、`tests/test_router.py`、`tests/test_settings.py`、`tests/shared/` 都歸主掌者改。
- **各 app 的 session**：只改自己的 `apps/<套件>/` 與 `tests/<套件>/`。另外可以做三種「只加一行」的改動：
  1. `router/root.py` 的 `MOUNTS` 加自己那一行；
  2. `router/settings.py` 的 `PER_APP_KEYS` 加自己的秘密／每個 app 各一份的設定名稱；
  3. `requirements.txt` 加自己需要的套件（先查 PyPI 最新正式版，旁邊寫用途與為什麼）。
- 覺得 `shared/` 少了什麼、有 bug、要改介面：**用 SendMessage 告訴主掌者**，不要自己改。主掌者會判斷是不是所有 app 都適用，改完通知大家。

## 動手前必讀

1. `README.md` 的「加入 router-backend」清單。
2. `shared/__init__.py`：有哪些共用模組、什麼東西才放進 `shared/`。
3. `router/settings.py` 與 `router/dispatcher.py` 開頭的說明。

## 硬規則（測試會抓的）

- app 裡的 import 一律用相對路徑（`from . import db`）或 `apps.<套件>.…`，不能寫裸的 `import db`、`import config`：每個專案都有同名模組，在同一個行程裡第二個 import 會默默拿到第一個專案的。
- app 不讀 `os.environ`／`os.getenv`，也不呼叫 `load_dotenv`。設定只從 `create_app(settings)` 拿到的 `AppSettings` 讀，環境變數都有前綴（`FUFOOD__DATABASE_URL`）。
- app 不 import 別的 app。
- `shared/` 不 import 任何 app、不讀環境變數、模組層不放可變的狀態。

## 硬規則（測試抓不到的，自己注意）

- **import 與 `create_app()` 期間不開執行緒、不對外連線、不連資料庫。** 背景工作在第一個請求時（`before_request`）才開始。`create_app` 在 app 的載入鎖裡跑，卡住會讓這個 app 的所有請求排隊。
- 對外連線一律設逾時（requests 要寫 `timeout=`）。所有 app 共用 16 條執行緒，一個卡住的連線會佔走一條。
- **只能一個 worker**（記憶體裡的每日上限與快取）。不要改 gunicorn 的啟動參數。
- 不印、不記秘密與使用者寫的內容。錯誤 log 只記例外的型別與位置（`shared.flask_app` 已經這樣做）。
- 不保存 IP。gunicorn 的 access log 由 `router/root.py` 關掉，不要打開。
- 回應格式一律 `{"error": 給人看的訊息, "code": 代碼}`（`shared.flask_app.new_app` 已經統一）。

## 推上去之前

1. 先 `git pull --rebase`。好幾個 session 各自 clone 了這個 repo，main 隨時會前進。
2. **跑全部的測試**，不只自己的 app。改到共用的三個檔案（`MOUNTS`、`PER_APP_KEYS`、`requirements.txt`），別的 app 也可能壞：
   ```bash
   tools/local_db.sh start
   TEST_DATABASE_URL=postgresql://postgres@localhost:5434/amd_test .venv/bin/python -m pytest
   ```
3. 推到 main 就會部署整個服務。設定檢查不過（例如少了前綴、兩個 app 用同一個資料庫），服務起不來，Render 會繼續跑舊版，但新版一直上不去。

## 版本

Python 與所有套件都用最新正式版，寫在 `.python-version` 與 `requirements.txt`。全部 app 共用同一份，升級時要一起測。
