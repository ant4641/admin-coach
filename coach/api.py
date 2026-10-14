"""最小 FastAPI。啟動：uvicorn coach.api:app --reload，然後開 http://127.0.0.1:8000/docs"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StringConstraints

from coach.flow import CoachService, FlowError
from coach.llm import get_llm
from coach.store import JsonFileStore, SessionNotFound

ROOT = Path(__file__).resolve().parent.parent
SESSIONS_DIR = ROOT / "outputs" / "sessions"

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Score = Annotated[int, Field(ge=0, le=100)]


class StartBody(BaseModel):
    name: Text = Field(description="稱呼，例如「林主管」")
    ideal: Text = Field(description="理想情境（像電影畫面的具體場景）")
    current: Text = Field(description="現在情境（團隊人數與職位、目前怎麼運作、最常處理的三個狀況）")


class AnswerBody(BaseModel):
    answer: Text = Field(description="對上一個追問的回答")


class ConfidenceBody(BaseModel):
    score: Score = Field(description="對目前這一版的執行信心分數，0～100；80 以上進入執行")


class WeeklyReportBody(BaseModel):
    actual: Text = Field(description="實際狀況")
    score: Score = Field(description="本週成果分數，0～100")
    achieved: str = Field("", description="哪些達成")
    not_achieved: str = Field("", description="哪些沒達成")
    reason: str = Field("", description="原因")


class Reply(BaseModel):
    session_id: str
    status: str = Field(description="clarify＝有一個問題等你用 /answer 回答；plan＝有等級表，等你用 /confidence 給分；ready＝進入執行，一週後送 /weekly-report")
    message: str = Field(description="追問、等級表或提醒（Markdown）")
    kind: str | None = Field(None, description="status=clarify 時在問什麼：intake / shaky_item / upstream_confirm / adjust_info / weekly")
    missing: list[str] = []
    version: int | None = Field(None, description="這一步產出（或目前）的等級表版本")
    changes: list[dict[str, Any]] = Field([], description="與上一版相比改了哪裡（模型說明，含原因）")
    diff: str | None = Field(None, description="與上一版的逐行差異（程式算的）")
    analysis: dict[str, Any] | None = Field(None, description="週回報的語意分析")
    upstream: dict[str, Any] | None = Field(None, description="上游目標提議，等你用 /answer 同意或不同意")
    upstream_decision: str | None = None
    advice: str = ""
    rules: dict[str, Any] = Field({}, description="這一步使用的規則檔版本")


def create_app(service: CoachService | None = None) -> FastAPI:
    load_dotenv(ROOT / ".env")
    svc = service or CoachService(JsonFileStore(SESSIONS_DIR), get_llm)
    app = FastAPI(title="管到飛", description="每週陪跑教練：等級表 → 信心分數 → 週回報 → 新等級表")

    @app.exception_handler(SessionNotFound)
    async def _not_found(_: Request, exc: SessionNotFound):
        return JSONResponse(status_code=404, content={"detail": f"找不到 session：{exc}"})

    @app.exception_handler(FlowError)
    async def _flow_error(_: Request, exc: FlowError):
        return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})

    def reply(outcome) -> Reply:
        return Reply(**outcome.to_dict())

    @app.post("/sessions", response_model=Reply, summary="建立對話：回 plan（等級表）或 clarify（一個追問）")
    def create_session(body: StartBody):
        return reply(svc.start(body.name, body.ideal, body.current))

    @app.post("/sessions/{session_id}/answer", response_model=Reply, summary="回答追問")
    def answer(session_id: str, body: AnswerBody):
        return reply(svc.answer(session_id, body.answer))

    @app.post("/sessions/{session_id}/confidence", response_model=Reply, summary="對目前這一版給執行信心分數（0～100）")
    def confidence(session_id: str, body: ConfidenceBody):
        return reply(svc.confidence(session_id, body.score))

    @app.post("/sessions/{session_id}/weekly-report", response_model=Reply, summary="一週後回報，產出調整後的新等級表")
    def weekly_report(session_id: str, body: WeeklyReportBody):
        return reply(
            svc.weekly_report(session_id, body.actual, body.score, body.achieved, body.not_achieved, body.reason)
        )

    @app.get("/sessions/{session_id}", summary="完整紀錄與所有版本")
    def get_session(session_id: str):
        return svc.get(session_id)

    return app


app = create_app()
