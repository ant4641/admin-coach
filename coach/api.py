"""FastAPI。啟動：uvicorn coach.api:app --reload，然後開 http://127.0.0.1:8000/docs"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
import html

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from coach.export import XLSX_TYPE, build_xlsx, content_disposition
from coach.flow import CoachService, FlowError, WrongState, current_result
from coach.llm import get_llm
from coach.ratelimit import RateLimiter
from coach.tunnel import QR_DIR, make_qr_svg, read_public_url
from coach.store import JsonFileStore, SessionNotFound

ROOT = Path(__file__).resolve().parent.parent
SESSIONS_DIR = ROOT / "outputs" / "sessions"
WEB_DIR = Path(__file__).resolve().parent / "web"


class StartBody(BaseModel):
    name: str = Field(description="稱呼（必填），例如「林老闆」")
    industry: int = Field(description="產業編號 1～20（必填），清單見 GET /industries")
    industry_other: str = Field("", description="產業選 20（其他）時必填，要填的產業")
    product: str = Field("", description="你覺得自己/團隊/公司在賣什麼產品（可空白）")
    sales: str = Field("", description="賣的如何（可空白）")
    score: int | None = Field(None, description="為自己的營運打個分數 1～10（可空白，只存檔不送模型）")
    reason: str = Field("", description="為何給這個分數（可空白，只存檔不送模型）")


class AnswerBody(BaseModel):
    answer: str = Field(description="對上一個追問的回答")


class Byproduct(BaseModel):
    name: str = Field(description="副產品名稱")
    description: str = Field(description="說明")


class ByproductsBody(BaseModel):
    vfp: str = Field(description="VFP 一句話")
    byproducts: list[Byproduct] = Field(description="整份副產品清單，由源頭排到終點；會整份覆寫")


class Reply(BaseModel):
    session_id: str
    status: str = Field(description="clarify＝有一個問題等你用 /answer 回答；vfp＝已產出 VFP 與副產品")
    question: str = Field("", description="status=clarify 時的問題")
    vfp: str = Field("", description="status=vfp 時目前採用的 VFP（主管改過就是改過的）")
    byproducts: list[dict[str, Any]] = Field([], description="status=vfp 時目前採用的副產品，由源頭排到終點")
    edited: bool = Field(False, description="主管是否修改過")
    rules: dict[str, Any] = Field({}, description="這一步使用的規則檔版本")


class IndustryItem(BaseModel):
    no: int
    name: str


QR_STYLE = """
html, body { margin: 0; background: #FFFFFF; color: #0C3467; font-family: "Microsoft JhengHei", "PingFang TC", "Noto Sans TC", system-ui, sans-serif; }
main { min-height: 100vh; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 24px; padding: 24px; box-sizing: border-box; text-align: center; }
h1 { margin: 0; font-size: 2.2rem; }
.qr { width: min(80vw, 420px); min-width: 360px; background: #FFFFFF; }
.url { font-size: 2rem; font-weight: 700; word-break: break-all; max-width: 90vw; }
.note { margin: 0; font-size: 0.95rem; color: #5B6B80; }
.none { font-size: 1.8rem; font-weight: 700; color: #5B6B80; }
@media (max-width: 420px) { .qr { min-width: 0; width: 80vw; } .url { font-size: 1.4rem; } }
"""


def render_qr_page(url: str | None) -> str:
    """QR 頁面 HTML：白底置中，不使用任何外部資源（SVG 直接內嵌）。"""
    if url:
        body = (
            f'<div class="qr">{make_qr_svg(url)}</div>'
            f'<div class="url">{html.escape(url)}</div>'
            '<p class="note">第一次開啟若看到 ngrok 的提醒頁，請按 Visit Site</p>'
        )
    else:
        body = '<div class="none">目前沒有公開網址</div>'
    return (
        '<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>掃描就能試用：管到飛</title><style>{QR_STYLE}</style></head>"
        f"<body><main><h1>掃描就能試用：管到飛</h1>{body}</main></body></html>"
    )


def create_app(
    service: CoachService | None = None, limiter: RateLimiter | None = None, qr_dir: Path | None = None
) -> FastAPI:
    load_dotenv(ROOT / ".env")
    svc = service or CoachService(JsonFileStore(SESSIONS_DIR), get_llm)
    rate = limiter or RateLimiter.from_env()
    qr_path = qr_dir or QR_DIR

    def limit_model_calls(request: Request) -> None:
        """會呼叫模型的端點才限流；來源 IP 看通道帶來的標頭，不能只看連線位址（通道讓所有連線都像來自本機）。"""
        rate.check(request.headers, request.client.host if request.client else None)
    app = FastAPI(title="管到飛", description="帶主管推理出最終有價值產品（VFP），再拆成副產品")

    @app.exception_handler(SessionNotFound)
    async def _not_found(_: Request, exc: SessionNotFound):
        return JSONResponse(status_code=404, content={"detail": f"找不到 session：{exc}"})

    @app.exception_handler(FlowError)
    async def _flow_error(_: Request, exc: FlowError):
        return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})

    @app.exception_handler(RequestValidationError)
    async def _bad_request(_: Request, exc: RequestValidationError):
        problems = []
        for e in exc.errors():
            field = ".".join(str(x) for x in e["loc"] if x != "body") or "請求內容"
            problems.append(f"{field}{'缺少' if e['type'] == 'missing' else '格式不正確'}")
        return JSONResponse(status_code=422, content={"detail": "、".join(problems)})

    def reply(outcome) -> Reply:
        return Reply(**outcome.to_dict())

    @app.get("/industries", response_model=list[IndustryItem], summary="產業清單（來自規則檔 2.1）")
    def industries():
        return [IndustryItem(no=i, name=n) for i, n in enumerate(svc.industries(), start=1)]

    @app.post(
        "/sessions",
        response_model=Reply,
        summary="建立對話：回 clarify（一個追問）或 vfp",
        dependencies=[Depends(limit_model_calls)],
    )
    def create_session(body: StartBody):
        return reply(
            svc.start(
                body.name, body.industry, body.industry_other, body.product, body.sales, body.score, body.reason
            )
        )

    @app.post(
        "/sessions/{session_id}/answer",
        response_model=Reply,
        summary="回答追問（vfp 之後回 409）",
        dependencies=[Depends(limit_model_calls)],
    )
    def answer(session_id: str, body: AnswerBody):
        return reply(svc.answer(session_id, body.answer))

    @app.put(
        "/sessions/{session_id}/byproducts",
        response_model=Reply,
        summary="整份覆寫 VFP 與副產品（只有 vfp 狀態可用，其他狀態回 409）",
    )
    def put_byproducts(session_id: str, body: ByproductsBody):
        return reply(svc.edit_result(session_id, body.vfp, [b.model_dump() for b in body.byproducts]))

    @app.get("/sessions/{session_id}", summary="完整紀錄；vfp 之後 result.original 是模型原始版、result.edited 是主管修改版")
    def get_session(session_id: str):
        return svc.get(session_id)

    @app.get("/sessions/{session_id}/export.xlsx", summary="匯出 Excel（.xlsx）；內容以主管修改版為準，沒改過就是模型原始版。非 vfp 狀態回 409")
    def export_xlsx(session_id: str):
        s = svc.get(session_id)
        if s["status"] != "vfp":
            raise WrongState(f"目前狀態是 {s['status']}，還沒有可匯出的結果")
        cur = current_result(s)
        today = date.today()
        return Response(
            content=build_xlsx(cur["vfp"], cur["byproducts"], today, s["inputs"]),
            media_type=XLSX_TYPE,
            headers={"Content-Disposition": content_disposition(today)},
        )

    @app.get("/qr", include_in_schema=False)
    def qr_page():
        """掃描用的 QR 頁面。每次載入都讀最新的網址檔，不快取。"""
        url = read_public_url(qr_path)
        return HTMLResponse(render_qr_page(url), headers={"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"})

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(WEB_DIR / "index.html")

    # 網頁的靜態檔全部本機提供（不用 CDN）。放在最後，不蓋掉上面的 API 路由
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    return app


app = create_app()
