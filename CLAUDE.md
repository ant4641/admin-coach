# 管到飛 — 每週陪跑教練，一上任就帶團隊起飛

## 產品目標
給剛上任的中小企業主管用的每週 AI 陪跑教練：
1. 主管輸入「理想情境」（電影畫面般的具體場景）與「現在情境」
2. 系統依規則檔的「行政管理等級表」（10 級）產出計畫；資訊不足時**追問**，不硬產出
3. 主管給「執行信心分數」(0–100)，< 80 就來回調整，≥ 80 才執行
4. 一週後主管回報實際狀況與成果分數 → 產出調整後的新等級表（標示與上週差異）

## 規則檔（產品的大腦）
- 位置：`coach_rules.md`（專案根目錄）
- **不可由 AI 修改內容**。發現矛盾或缺漏 → 列出來問使用者。
- 版本：取自檔尾「版本紀錄」表最後一列（例如 `v0.1`）＋ 檔案 SHA-256 前 8 碼（沒升版號但改了內容也分得出來）。
- 程式附加的「輸出約定」(JSON 格式) 在 `coach/prompt.py`，不寫進規則檔。

## 專案結構
```
coach_rules.md        規則檔（使用者維護）
coach/
  rules.py            讀規則檔、算版本
  cases.py            讀測試案例
  prompt.py           組 messages、解析模型回覆（含信心調整、週回報兩段的輸出約定）
  llm.py              LLM 抽象層（azure / openai），換供應商只改這裡
  cli.py              測試案例執行器（含 kind: weekly 的週回報案例）
  flow.py             對話狀態機（clarify → plan → ready → 週回報 → plan…），API 與 chat 共用
  store.py            session 存取（JSON 檔）；換資料庫只換這一層
  api.py              最小 FastAPI
  chat.py             互動式命令列，不開網頁跑完整流程
test_cases/           測試輸入（*.md，front matter 有 expected_status）
outputs/              每次執行的輸出（依案例分資料夾）+ runs.csv 總表；sessions/<id>.json 是對話紀錄
tests/                pytest（用 tests/fakes.py 的替身，不需金鑰；不驗證規則效果）
```

## 階段
- [x] 階段 0：規劃 + CLAUDE.md
- [x] 階段 1：CLI 腳本（讀規則 → 讀案例 → 呼叫 LLM → 輸出檔案）
- [x] 階段 2-lite：信心分數來回調整 + 週回報 + 最小 FastAPI（session 存 JSON 檔，不接資料庫）
- [ ] 階段 2：PostgreSQL（SQLAlchemy + Alembic），換掉 store.py
- [ ] 階段 3：Flutter Web
- [ ] 階段 4：部署（Render / Neon / 靜態託管）、CORS

每階段做完先停，等使用者確認。

## 執行
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env      # 填入 Azure OpenAI 端點/金鑰/部署名

python -m coach.cli --all                          # 跑全部案例
python -m coach.cli test_cases/01_noodle_shop.md   # 跑單一案例
python -m coach.cli --all --dry-run                # 不呼叫 LLM，寫到 outputs/_dry_run/*.md

uvicorn coach.api:app --reload                     # API，開 http://127.0.0.1:8000/docs
python -m coach.chat                               # 互動式命令列（--case / --resume / --weekly / --list）
```
輸出：`outputs/<案例>/<時間>__rules-<版本>-<sha8>.md`，並追加一列到 `outputs/runs.csv`。
所有案例的 actual_status 與 expected_status 一致 → exit 0，否則 exit 1。

## 測試
```powershell
pytest -q
```

## 安全規則
- **絕不 commit `.env` 或任何金鑰**。只提交 `.env.example`。
- 前端（階段 3）只呼叫自家後端，不放任何金鑰。
- 不編造使用者沒提供的資料（數字、人名、事實）。

## 程式風格
- Python 3.11+，型別註記，標準函式庫優先
- 註解與使用者訊息用繁體中文
