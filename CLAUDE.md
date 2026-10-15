# 管到飛 — 帶主管推理出團隊的最終有價值產品（VFP），再拆成副產品

## 產品目標
給剛上任的中小企業主管用的 AI 陪跑教練：
1. 主管輸入六項：稱呼、產業（規則檔 2.1 的編號清單）、在賣什麼產品、賣得如何、營運自評 1～10 分、給分原因
2. 系統用「客人狀態」的問法**一次問一個問題**，引導主管說出團隊交付給誰、讓對方變成什麼狀態
3. 資訊足夠時產出一句話的 VFP，再拆成「副產品」（由源頭排到終點）；資訊不足就追問，不硬產出
4. 產出後主管可以修改 VFP 與副產品（保留模型原始版本與主管修改版本）

## 規則檔（產品的大腦）
- 位置：`coach_rules.md`（專案根目錄）
- **不可由 AI 修改內容**。發現矛盾或缺漏 → 列出來問使用者。
- 版本：取自檔尾「版本紀錄」表最後一列（例如 `v0.4`）＋ 檔案 SHA-256 前 8 碼。
- **輸出約定（clarify / vfp 兩種 JSON）以規則檔第 5 章為唯一來源**。`coach/prompt.py` 不附加任何格式說明，只組 messages 與解析。
- 產業清單從規則檔 2.1 讀取（`coach/inputs.py`），程式不寫死。
- 表格怎麼畫（命令列文字表格、之後的網頁）由程式負責，與規則檔無關。

## 專案結構
```
coach_rules.md        規則檔（使用者維護）
coach/
  rules.py            讀規則檔、算版本
  inputs.py           六項輸入的題目文字、產業清單解析（規則檔 2.1）、驗證
  cases.py            讀測試案例（含 answers 多輪腳本、todo 待補標記）
  prompt.py           組 messages（規則檔當 system）、解析 clarify / vfp 回覆
  llm.py              LLM 抽象層（azure / openai），換供應商只改這裡
  flow.py             對話狀態機（clarify → vfp），API 與 chat 共用；含 edit_result
  store.py            session 存取（JSON 檔）
  display.py          命令列文字表格（處理中文字寬度）
  cli.py              測試案例執行器（多輪、安全上限 10 輪）
  chat.py             互動式命令列
  export.py           匯出 Excel（openpyxl）
  check.py            發表前檢查（python -m coach.check）
  launch.py           發表用啟動（uvicorn + ngrok 公開網址 + QR + 開瀏覽器）；python -m coach.launch [--no-tunnel]
  tunnel.py           ngrok 通道、網址檔、QR（SVG）的純函式與可替換元件
  ratelimit.py        記憶體限流與來源 IP 判斷
  api.py              FastAPI：GET /industries、POST /sessions、POST /sessions/{id}/answer、PUT /sessions/{id}/byproducts、GET /sessions/{id}、GET /sessions/{id}/export.xlsx，並在 / 提供網頁
  web/                三頁網頁（index.html、style.css、app.js、intake.js、export-image.js）；intake.js 是六題的狀態邏輯（純函式，不碰 DOM）；assets/ 放 sky.jpg、logo.png（選用）、icons/*.svg；全部本機提供，不用 CDN
test_cases/           測試輸入（*.md，front matter 有 expected_status）
outputs/              每次執行的輸出（依案例分資料夾）+ runs.csv；sessions/<id>.json 是對話紀錄
tests/                pytest；tests/js/ 是 node 內建 test runner 的前端邏輯測試（pytest 會呼叫，找不到 node 就跳過）（用 tests/fakes.py 的替身，不需金鑰；不驗證規則效果）
```

## session 與狀態
- `status` 只有 `clarify`（等主管回答一個問題）與 `vfp`（已產出）。「收集輸入」只是建立 session 時的驗證。
- 只有稱呼、產業必填；其餘空白以「（未填）」送模型，由模型追問。
- 自評分數與原因只存檔，不送模型。
- 追問輪數產品沒有上限；vfp 之後 `answer` 回 409。
- 模型回覆無法解析 → 不存檔，可重送。

## 案例格式（test_cases/*.md）
front matter：`title`、`expected_status`（clarify | vfp）、選用 `todo: 待補`（會被跳過）。
內文分段：`## 稱呼 / 產業（編號）/ 其他產業說明 / 產品 / 賣得如何 / 自評分數 / 原因 / answers`；
`answers` 一行 `- ` 一個回答，依序回答追問。沒有 answers 的案例只跑第一輪。

## 階段
- [x] 階段 0：規劃
- [x] 階段 1：核心流程（命令列）
- [x] 階段 2：API（POST /sessions、POST /sessions/{id}/answer、GET /sessions/{id}、PUT /sessions/{id}/byproducts）
- [ ] 階段 3：網頁（#/ 首頁 → #/chat 對話 → #/result 結果；匯出 Excel／JPG）← 進行中，等使用者確認
- [ ] 階段 4：發表保險（精簡版：檢查指令、.bat、VS Code 任務已完成；DEMO 模式暫不做）

每階段做完先停，等使用者確認。

## 執行
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env      # 填入 Azure OpenAI 端點/金鑰/部署名

python -m coach.cli --all                          # 跑全部案例（待補的略過）
python -m coach.cli test_cases/01_design_vague.md  # 跑單一案例
python -m coach.cli --all --dry-run                # 不呼叫 LLM，第一輪 messages 寫到 outputs/_dry_run/*.md

uvicorn coach.api:app --reload                     # 網頁 http://127.0.0.1:8000/ ，API 文件 /docs
python -m coach.check                             # 發表前檢查：模型連線、網頁檔案、sky/logo、8000 連接埠（全部 OK 結束碼 0）
python -m coach.chat                               # 互動式命令列（--case / --resume / --list）
```
Windows 雙擊（不用 .ps1，避免執行原則被擋）：
- `bat/run.bat`：啟用 .venv、呼叫 `python -m coach.launch`（啟動 uvicorn、ngrok 公開網址、QR、開兩個分頁），結束前 pause
- `bat/run_local.bat`：同上但加 `--no-tunnel`，只在本機執行
- `bat/check.bat`：啟用 .venv、跑 `python -m coach.check`，最後 pause
- VS Code：Terminal > Run Task > 「管到飛：啟動」「管到飛：啟動（僅本機）」「管到飛：發表前檢查」（`.vscode/tasks.json` 有進版控）

### 現場操作順序（發表用）
1. 雙擊 `bat/check.bat`：確認 Azure、網頁檔案、ngrok、qrcode、對外通道（會短暫開一條通道再關掉）都 OK
2. 雙擊 `bat/run.bat`：等兩個分頁自動打開（首頁 `/`、QR 頁 `/qr`）
3. 把 `/qr` 那個分頁投影出來，觀眾掃描就能試用；第一次開啟若看到 ngrok 的提醒頁，請按 Visit Site
4. 結束：在 run 視窗按 Ctrl+C 或直接關視窗（uvicorn 與 ngrok 都會一起結束）

注意：
- **不要在同一台電腦重複開多個 run.bat**（8000 連接埠會被占用，啟動程式會提示先關閉舊的視窗）。
- 取不到公開網址（ngrok 不存在、authtoken 無效、網路被擋、逾時）不會中止，伺服器照常運作，`/qr` 顯示「目前沒有公開網址」；網路被擋時建議改用手機熱點。authtoken 要重設：`ngrok config add-authtoken <token>`。
- 每次啟動都會先刪掉 `outputs/qr/`，再寫入新的 `public_url.txt` 與 `qr.svg`。`/qr` 優先讀 `public_url.txt`，沒有才用環境變數 `PUBLIC_URL`。
- 會呼叫模型的端點（POST /sessions、POST /sessions/{id}/answer）有記憶體限流：每個來源 IP 每 10 分鐘 30 次、全站每小時 300 次，超過回 429「目前使用的人比較多，請稍後再試」。可用環境變數 `RATE_LIMIT_IP_MAX`、`RATE_LIMIT_IP_WINDOW_SECONDS`、`RATE_LIMIT_GLOBAL_MAX`、`RATE_LIMIT_GLOBAL_WINDOW_SECONDS` 調整。來源 IP 依序取 `CF-Connecting-IP`、`X-Forwarded-For` 第一個值、連線位址。

檢查失敗時會用中文說明：401 金鑰錯、404 部署名稱錯、400 API version 錯（要填 API 版本如 2024-10-21，不是模型版本）、逾時或連線失敗、`.env` 缺哪個欄位。金鑰不會被印出。

輸出：`outputs/<案例>/<時間>__rules-<版本>-<sha8>.md`，並追加一列到 `outputs/runs.csv`。
所有（非待補）案例的 actual_status 與 expected_status 一致 → exit 0，否則 exit 1。
一個案例最多呼叫模型 10 次，超過標記失敗（over_limit），只為避免失控花費。

## 測試
```powershell
pytest -q
```

## 安全規則
- **絕不 commit `.env` 或任何金鑰**。只提交 `.env.example`。
- 前端只呼叫自家後端，不放任何金鑰。
- 不編造使用者沒提供的資料（數字、人名、事實）。

## 程式風格
- Python 3.11+，型別註記，標準函式庫優先
- 註解與使用者訊息用繁體中文
