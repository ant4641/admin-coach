"""對話流程（狀態機）。API 與命令列共用這一層，不碰 HTTP 也不碰檔案格式。

「收集輸入」只是 start() 的驗證，不是 session 的狀態。session["status"]：
  clarify  等主管回答一個問題（answer）。session["pending"] = {"question": ...}
  vfp      已產出 VFP 與副產品。answer 之後回 409；主管可用 edit_result 整份改寫

模型呼叫失敗或回覆不能用時一律丟例外，而且不存檔：session 維持呼叫前的樣子，可以直接重試。
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Callable

from coach.inputs import build_inputs, parse_industries
from coach.llm import LLMClient, LLMResult
from coach.prompt import CoachReply, ValidationErrors, build_messages, parse_reply, upgrade_byproduct, validate_edited_byproducts
from coach.staff import validate_staff
from coach.rules import Rules, load_rules
from coach.store import SessionStore


log = logging.getLogger("coach.flow")

# 對外（API／網頁）看到的訊息。技術細節放在例外的 debug 屬性，只給命令列開發工具看，不進 HTTP 回應與日誌
MODEL_DOWN_MESSAGE = "目前無法連線到模型，請稍後再試"
BAD_REPLY_MESSAGE = "模型回覆無法解析，請再送出一次"


class FlowError(Exception):
    status_code = 400
    debug: str = ""  # 開發用的技術細節（可能含模型原文），不可回給網頁使用者


class InvalidInput(FlowError):
    status_code = 422

    def __init__(self, message: str = "", errors: list[dict] | None = None) -> None:
        super().__init__(message)
        self.errors = errors or []


class WrongState(FlowError):
    status_code = 409


class BadModelReply(FlowError):
    status_code = 502

    def __init__(self, message: str = BAD_REPLY_MESSAGE, debug: str = "") -> None:
        super().__init__(message)
        self.debug = debug


class LLMUnavailable(FlowError):
    status_code = 503

    def __init__(self, message: str = MODEL_DOWN_MESSAGE, debug: str = "") -> None:
        super().__init__(message)
        self.debug = debug


@dataclass
class Outcome:
    """每一步回給呼叫端的內容。vfp / byproducts 為目前採用的版本（主管改過就是改過的）。"""

    session_id: str
    status: str  # clarify | vfp
    question: str = ""
    vfp: str = ""
    byproducts: list[dict] = field(default_factory=list)
    edited: bool = False
    rules: dict = field(default_factory=dict)
    llm_result: LLMResult | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("llm_result")
        return d


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _rules_info(rules: Rules) -> dict:
    return {"version": rules.version, "sha8": rules.sha256[:8]}


def current_result(s: dict) -> dict:
    """session 目前採用的 vfp 與副產品：主管改過用改過的，否則是模型原始版本。"""
    r = s["result"]
    cur = r["edited"] or r["original"]
    return {**cur, "byproducts": [upgrade_byproduct(b) for b in cur["byproducts"]]}


class CoachService:
    def __init__(
        self,
        store: SessionStore,
        llm_factory: Callable[[], LLMClient],
        rules_loader: Callable[[], Rules] = load_rules,
    ) -> None:
        self.store = store
        self._llm_factory = llm_factory
        self._rules_loader = rules_loader
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # ---- 對外動作 -------------------------------------------------------

    def start(
        self,
        name: str,
        industry_no: int,
        industry_other: str = "",
        product: str = "",
        sales: str = "",
        score: int | None = None,
        reason: str = "",
    ) -> Outcome:
        rules = self._rules_loader()
        try:
            inputs = build_inputs(
                parse_industries(rules.text), name, industry_no, industry_other, product, sales, score, reason
            )
        except ValueError as e:
            raise InvalidInput(str(e)) from e
        s = {
            "id": self.store.new_id(),
            "created_at": _now(),
            "inputs": inputs,
            "status": "clarify",
            "pending": None,
            "exchanges": [],
            "result": None,
        }
        return self._run(s, rules)

    def answer(self, session_id: str, text: str) -> Outcome:
        text = text.strip()
        if not text:
            raise InvalidInput("回答不可為空")
        with self._locked(session_id):
            s = self.store.load(session_id)
            if s["status"] != "clarify" or not s.get("pending"):
                raise WrongState(f"目前狀態是 {s['status']}，沒有待回答的問題")
            s["exchanges"][-1]["manager"] = text
            s["exchanges"][-1]["answered_at"] = _now()
            return self._run(s, self._rules_loader())

    def edit_result(self, session_id: str, vfp: str, byproducts: list[dict]) -> Outcome:
        """整份覆寫 vfp 與副產品（含負責人）。模型原始版本留在 result.original，主管版本放 result.edited。"""
        vfp = (vfp or "").strip()
        if not vfp:
            raise InvalidInput("VFP 不可為空")
        try:
            validate_edited_byproducts(byproducts)
        except ValidationErrors as e:
            raise InvalidInput(str(e), e.errors) from e
        with self._locked(session_id):
            s = self.store.load(session_id)
            if s["status"] != "vfp":
                raise WrongState(f"目前狀態是 {s['status']}，只有 vfp 狀態能修改")
            try:
                items = validate_edited_byproducts(byproducts, {p["id"] for p in s.get("staff", [])})
            except ValidationErrors as e:
                raise InvalidInput(str(e), e.errors) from e
            s["result"]["edited"] = {"vfp": vfp, "byproducts": items, "at": _now()}
            self._save(s)
            return self._outcome(s)

    def get_staff(self, session_id: str) -> list[dict]:
        s = self.store.load(session_id)
        if s["status"] != "vfp":
            raise WrongState(f"目前狀態是 {s['status']}，只有 vfp 狀態能使用員工名單")
        return list(s.get("staff", []))

    def set_staff(self, session_id: str, staff: list[dict]) -> list[dict]:
        """整份覆寫員工名單。刪除的員工，原本指派給他的項目自動改成未指派。"""
        with self._locked(session_id):
            s = self.store.load(session_id)
            if s["status"] != "vfp":
                raise WrongState(f"目前狀態是 {s['status']}，只有 vfp 狀態能使用員工名單")
            old_ids = {p["id"] for p in s.get("staff", [])}
            try:
                items = validate_staff(staff, old_ids)
            except ValidationErrors as e:
                raise InvalidInput(str(e), e.errors) from e
            keep = {p["id"] for p in items}
            edited = s["result"].get("edited")
            if edited:
                edited["byproducts"] = [
                    {**b, "assignee": b.get("assignee") if b.get("assignee") in keep else None} for b in edited["byproducts"]
                ]
            s["staff"] = items
            self._save(s)
            return items

    def industries(self) -> list[str]:
        """產業清單（規則檔 2.1），第 n 項的編號是 n。"""
        return parse_industries(self._rules_loader().text)

    def get(self, session_id: str) -> dict:
        s = self.store.load(session_id)
        if s.get("result"):  # 舊格式只在回傳時轉換，不改寫檔案
            r = s["result"]
            for key in ("original", "edited"):
                if r.get(key):
                    r[key] = {**r[key], "byproducts": [upgrade_byproduct(b) for b in r[key]["byproducts"]]}
        s.setdefault("staff", [])  # 舊 session 沒有名單：回傳時補預設值，不改寫檔案
        return s

    # ---- 呼叫模型並套用結果 ---------------------------------------------

    def _run(self, s: dict, rules: Rules) -> Outcome:
        reply, res = self._call(build_messages(rules, s["inputs"], s["exchanges"]))
        if reply.status == "clarify":
            s["exchanges"].append({"at": _now(), "coach": reply.as_history(), "manager": None})
            s["status"], s["pending"] = "clarify", {"question": reply.question}
        else:
            s["status"], s["pending"] = "vfp", None
            s["result"] = {
                "created_at": _now(),
                "rules_version": rules.version,
                "rules_sha8": rules.sha256[:8],
                "model": res.model,
                "original": {"vfp": reply.vfp, "byproducts": reply.byproducts},
                "edited": None,
            }
        self._save(s)
        log.info("session=%s status=%s", s["id"], s["status"])
        out = self._outcome(s)
        out.rules = _rules_info(rules)
        out.llm_result = res
        return out

    def _outcome(self, s: dict) -> Outcome:
        if s["status"] == "clarify":
            return Outcome(s["id"], "clarify", question=s["pending"]["question"])
        r = s["result"]
        cur = current_result(s)
        return Outcome(
            s["id"],
            "vfp",
            vfp=cur["vfp"],
            byproducts=cur["byproducts"],
            edited=r["edited"] is not None,
            rules={"version": r["rules_version"], "sha8": r["rules_sha8"]},
        )

    def _call(self, messages: list[dict[str, str]]) -> tuple[CoachReply, LLMResult]:
        try:
            res = self._llm_factory().complete(messages)
        except Exception as e:  # 缺金鑰、網路、供應商錯誤都一樣處理
            log.warning("模型呼叫失敗：%s", type(e).__name__)  # 只記類型：訊息可能含端點或請求內容
            # 缺環境變數是我們自己丟的 RuntimeError（只有變數名稱），可以直接說；其他例外一律用通用訊息
            message = str(e) if type(e) is RuntimeError else MODEL_DOWN_MESSAGE
            raise LLMUnavailable(message, debug=f"{type(e).__name__}: {e}") from e
        reply = parse_reply(res.text)
        if reply.status == "unknown":
            log.warning("模型回覆無法解析：%s", reply.parse_error)
            raise BadModelReply(
                f"模型回覆無法解析（{reply.parse_error}），請再送出一次", debug=f"{reply.parse_error}：{res.text[:300]}"
            )
        return reply, res

    def _save(self, s: dict) -> None:
        s["updated_at"] = _now()
        self.store.save(s)

    @contextmanager
    def _locked(self, session_id: str):
        # 同一個 session 的請求一次只處理一個，避免兩個請求同時讀舊檔再互相覆蓋
        with self._locks_guard:
            lock = self._locks.setdefault(session_id, threading.Lock())
        with lock:
            yield
