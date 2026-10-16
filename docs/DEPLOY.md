# 部署到雲端：Render + Neon

架構：Render 單一 Web Service（FastAPI 同時提供 API 與 `coach/web/` 靜態前端，同網域、不開 CORS）＋ Neon Postgres ＋ Azure OpenAI。
本機仍可用 `bat/run.bat`／`run_local.bat`（SQLite，不需要 Neon）。

## 1. Neon（手動操作）

1. 到 neon.tech 註冊 → Create project。Region 選 **AWS Asia Pacific 1 (Singapore)**（和 Render 同區域，延遲最低）。Postgres 版本用預設。
2. Dashboard → **Connect**，各複製一條連線字串：
   - **Pooled connection**（主機名含 `-pooler`）→ 給 Render 的 `DATABASE_URL`。執行期連線多又短，走 PgBouncer 才不會超過 Neon 的連線數上限。程式已關閉 psycopg 的 prepared statement 快取（`prepare_threshold=None`），相容 PgBouncer transaction 模式。
   - **Direct connection**（主機名**不含** `-pooler`）→ 給 Render 的 `DATABASE_URL_DIRECT`。只在啟動時跑 Alembic migration 用：DDL 與 advisory lock 走 PgBouncer 可能出狀況，direct 最穩。
3. 字串結尾要有 `?sslmode=require`（Neon 預設就有）。沒有也沒關係，程式會自動加 `sslmode=require`。`postgresql://` 與 `postgres://` 開頭都可以，程式會轉成 `postgresql+psycopg://`。
4. 不要先手動建表。第一次部署時 `coach.serve` 會跑 `alembic upgrade head` 自動建 `sessions` 表。

## 2. Render（手動操作）

方式 A：New → **Blueprint** → 選這個 repo（讀根目錄 `render.yaml`），再把 `sync: false` 的變數填進去。
方式 B：New → **Web Service** → 手動填：

| 項目 | 值 |
|---|---|
| Runtime | Python 3 |
| Region | Singapore |
| Build Command | `pip install -r requirements.txt` |
| Start Command | `python -m coach.serve` |
| Health Check Path | `/healthz` |
| Instance Type | Free（試用）／Starter（正式，不休眠） |

環境變數（Environment 頁面；**值只在這裡填，不進 repo**）：

| 名稱 | 用途 |
|---|---|
| `PYTHON_VERSION` | `3.13.5` |
| `APP_ENV` | 固定填 `production`：啟動時檢查必要變數、限流改用雲端的 IP 判斷 |
| `DATABASE_URL` | Neon **pooled** 連線字串 |
| `DATABASE_URL_DIRECT` | Neon **direct** 連線字串（只給 migration） |
| `LLM_PROVIDER` | `azure` |
| `AZURE_OPENAI_ENDPOINT` | Azure OpenAI 端點 |
| `AZURE_OPENAI_API_KEY` | Azure OpenAI 金鑰 |
| `AZURE_OPENAI_DEPLOYMENT` | Azure 上的部署名稱 |
| `AZURE_OPENAI_API_VERSION` | API 版本，如 `2024-10-21` |
| `LLM_TEMPERATURE` | 選填；推理模型不要填 |
| `TRUSTED_PROXY_HOPS` | `1`（見下方「限流 IP」） |
| `RATE_LIMIT_*` | 選填，見 CLAUDE.md |
| `RUN_MIGRATIONS` | 選填；填 `0` 可跳過啟動時的 migration |

缺少必要變數時，服務啟動失敗並在 Render Logs 顯示「缺少必要的環境變數：…」（只列名稱，不印值）。

### 自動套用 migration 的做法與風險

做法：`python -m coach.serve` 在啟動 uvicorn 之前呼叫 `alembic upgrade head`（連 `DATABASE_URL_DIRECT`，沒設就連 `DATABASE_URL`）。失敗時程序直接結束，Render 會判定部署失敗並保留舊版繼續服務。

風險：
- 只有一個實例時沒問題。**多實例同時啟動**會同時跑 migration（Postgres 的 DDL 是交易式的，其中一個會失敗重來，但仍建議多實例時改成獨立的 pre-deploy 指令）。
- migration 跑在新版程式上線之前，**舊版程式在這段時間仍連著同一個資料庫**。所以 migration 必須向下相容（先加欄位、再改程式、最後才刪舊欄位）。
- 破壞性 migration（刪欄位、改型別）部署前先在 Neon 做 branch 備份（Neon 的 Branching 很便宜）。
- 回退：`alembic downgrade -1`（本機連到該資料庫執行），資料不會自動還原。

### 限流 IP

Render 的代理會**保留**用戶端自己送的 `X-Forwarded-For`，再把真實來源**附加在尾端**，所以最前面那個值可以被偽造。`APP_ENV=production` 時程式只信「從右邊數第 `TRUSTED_PROXY_HOPS` 個」，預設 1，並忽略 `CF-Connecting-IP`。
**上線後要驗證一次**：用兩支不同網路的手機（一支用行動網路）各連續送出超過限額的請求，應該各自被限流、不互相影響。若一個人被擋全站都被擋，表示 Render 前面還有一層代理（把 `TRUSTED_PROXY_HOPS` 改成 2 再測）。我無法在本機驗證 Render 的實際層數。

## 3. 資料表與 migration 規則

- 資料表只能透過 Alembic 變更：改 `coach/db.py` 的模型 → `alembic revision --autogenerate -m "說明"` → 人工檢查產生的檔案（尤其 `Text`、JSONB 的 import）→ `alembic upgrade head`。不得手改資料庫。
- `tests/test_cloud.py::test_migration_creates_sessions_table_and_matches_models` 會在模型與 migration 不一致時失敗。
- 目前只有 `sessions`（id、created_at、updated_at、payload JSON／JSONB、owner_user_id 可為空）。**還沒有 users 表。**

## 4. 驗證

### 本機（SQLite）
```powershell
pip install -r requirements-dev.txt
pytest -q                                  # 全部通過（Postgres 整合測試預設略過）
python -m coach.check                      # 模型連線等
bat\run_local.bat                          # 雙擊；首次啟動自動建立 outputs\coach.db
```
1. 開 http://127.0.0.1:8000/ ，跑完六題 → 看到 VFP 與副產品。
2. 重新整理頁面，案例還在。關掉 run 視窗再重開，用 `python -m coach.chat --list` 看得到舊案例。
3. http://127.0.0.1:8000/healthz 回 `{"status":"ok"}`；http://127.0.0.1:8000/docs 看得到 `/healthz`。
4. 舊 JSON 檔：`python -m scripts.import_json_sessions`（dry-run，只印數量）→ 確認後加 `--apply`。
5. 想在本機測 Postgres：`$env:TEST_DATABASE_URL="<一個可隨意寫入的 Postgres>"; pytest tests/test_cloud.py -k postgres`（用 Neon 的另一個 branch，不要用正式資料）。

### 雲端
1. Render 部署成功（Logs 有 `migration 已是最新版`）；開 `https://<服務>.onrender.com/healthz`。
2. 開首頁 → 跑完六題 → 看到結果。
3. **重新整理**：案例還在（session id 存在該分頁的 sessionStorage，同一個分頁才找得回來）。
4. **重新部署**（Render → Manual Deploy → Deploy latest commit）後，用同一個分頁重新整理：案例還在；或用 `GET /sessions/<id>` 確認。
5. Neon → Tables → `sessions` 看得到資料。
6. 匯出 Excel、匯出圖片（JPG）都能下載且內容正確。
7. 手機實測：用手機（行動網路）開網址，跑完六題，匯出。
8. 語音鍵：Web Speech API 需要 HTTPS（Render 預設有）與瀏覽器麥克風權限；Chrome／Android 與 iOS Safari 的支援度不同，需實機測。
9. 限流：見上方「限流 IP」。
10. 關掉 Azure 金鑰（改成錯的）測一次：前端應顯示「目前無法連線到模型，請稍後再試」，不是堆疊或金鑰。

## 5. 失敗時前端看到什麼

| 狀況 | HTTP | 前端訊息 |
|---|---|---|
| Azure 金鑰錯、逾時、網路斷、額度用完 | 503 | 目前無法連線到模型，請稍後再試 |
| 模型回覆不是合法 JSON／缺欄位 | 502 | 教練的回覆格式有誤，請再按一次送出（沒存檔，可直接重送） |
| 資料庫連不上（Neon 剛醒來會先自動重試 4 次、約 5 秒） | 503 `code=db_unavailable` | 資料暫時無法存取，請稍後再試 |
| 沒預料到的錯誤 | 500 | 系統發生錯誤，請稍後再試 |
| Render 服務休眠、重啟中（代理回的、沒有 JSON） | 502／503／504 | 伺服器正在啟動或暫時忙碌，請等幾秒再試一次 |
| 超過限流 | 429 | 目前使用的人比較多，請稍後再試 |
| 網路斷線 | — | 無法連線到伺服器，請稍後再試 |

日誌只記：session id 與狀態、例外**類型**、HTTP 方法與路徑。不記使用者輸入、模型原文、連線字串、SQL 與其參數（SQLAlchemy 例外訊息會帶出整份 payload，所以刻意不記訊息）。
開發用的技術細節（例外完整訊息、模型原文前 300 字）只在命令列工具（`coach.cli`、`coach.chat`）顯示。

## 6. 安全檢查結果

- **session id**：原本是 `時間戳-3 位元組亂數`（約 24 位元熵，加上可推測的時間），別人知道大概時間就能暴力猜。**已改**為 `secrets.token_urlsafe(16)`（128 位元，22 字元）。舊 id（匯入的）仍可讀取，不受影響。
- session id 是唯一憑證（沒有登入）：知道 id 的人可讀、可改、可匯出。網址不要公開貼出；加入登入後改成 owner 檢查（見下）。
- API 沒有「列出所有 session」的端點，不能列舉他人的案例。
- repo 檢查：沒有 `.env`、金鑰、連線字串被追蹤，`.env` 從未進過 git 歷史（`git log -S` 確認）。`outputs/`（含 sessions、qr、coach.db）與 `public/` 均被 `.gitignore` 排除。`*.db` 也已排除。
- 尚未做：請求內容長度上限、HTTP 安全標頭（CSP、HSTS 由 Render 的 HTTPS 提供）。

## 7. 之後加 Google OIDC：目前程式需要改的地方（本次不做）

預計新增 `users` 表欄位：`id`（內部主鍵，建議 UUID 字串）、`google_sub`（唯一）、`email`、`name`、`picture`、`created_at`、`last_login_at`；`sessions.owner_user_id` 加外鍵指向 `users.id`（走新的 Alembic migration）。

要改的地方：
1. `coach/db.py`＋新 migration：users 表、外鍵、`owner_user_id` 索引（已有）。
2. 新增 `coach/auth.py`：OIDC 授權碼流程（`/auth/login`、`/auth/callback`、`/auth/logout`），驗證 ID token；簽章的 session cookie（HttpOnly、Secure、SameSite=Lax），需要新環境變數 `GOOGLE_CLIENT_ID`、`GOOGLE_CLIENT_SECRET`、`SESSION_SECRET`、`PUBLIC_BASE_URL`。
3. `coach/api.py`：加 `current_user` 依賴；`POST /sessions` 寫入 `owner_user_id`；所有 `/sessions/{id}...` 端點檢查擁有者（不是擁有者回 404，不洩漏存在與否）；新增 `GET /sessions`（只列自己的）。
4. `coach/store.py`：`SessionStore` 介面加 owner 參數（`load(id, owner)`、`list_for(owner)`）；`DbSessionStore` 以 `owner_user_id` 過濾。
5. `coach/flow.py`：`start()` 接收 owner，`CoachService` 方法傳遞 owner。
6. `coach/web/app.js`：session id 不再存 sessionStorage，改由 `GET /sessions` 列出歷史；加登入／登出 UI；401 時導向登入。
7. `coach/ratelimit.py`：登入後可改以 user id 限流。
8. 匿名使用要不要保留（訪客模式）要先決定；若保留，舊的「知道 id 就能存取」要明確標示為匿名 session。
9. 測試：後端加授權測試（A 不能讀 B 的 session）；Flutter／LINE Bot 之後共用同一組 API，可再加 Bearer token（OIDC ID token）驗證。

## 8. 已知風險與限制

- **Render 免費方案**：閒置 15 分鐘休眠，喚醒要約 1 分鐘（第一個請求會逾時或很慢，前端會顯示「伺服器正在啟動」）；每月 750 小時；檔案系統是暫存的（所以才用 Neon 存資料）。發表當天請改 Starter，或事先喚醒。
- **Neon 免費方案**：5 分鐘無連線就休眠，首次連線多 0.5～數秒（已用 pool_pre_ping、`pool_recycle=240`、連線逾時 15 秒、4 次重試處理）；有儲存空間與運算時數額度，超過會暫停。每筆 session 約數 KB，空間不是問題。
- **記憶體限流**：重啟歸零、多實例各算各的。目前單實例可接受。
- **session id 即憑證**：沒有登入前，任何知道 id 的人都能存取該案例。
- **並行更新**：同一 session 的互斥鎖是程序內的 `threading.Lock`，單實例有效；多實例要改成資料庫列鎖（`SELECT ... FOR UPDATE`）或版本欄位。
- **Azure 呼叫**：同步呼叫，沒有重試；失敗時使用者按「送出」重試即可（失敗不存檔）。
- **`TRUSTED_PROXY_HOPS` 未在 Render 實測**（見上）。
- 依賴版本是目前本機安裝的版本；Render 建置時 `PYTHON_VERSION` 需為 Render 支援的版本，若建置失敗先檢查這一項。
