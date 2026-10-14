"""對話流程（狀態機）。API 與命令列共用這一層，不碰 HTTP 也不碰檔案格式。

session["status"]：
  clarify  等主管回答一個問題（POST answer）。session["pending"] 說明在問什麼：
           intake         理想/現況資訊不足（規則 2）
           shaky_item     信心分數 < 80，問「最讓你沒把握的是哪一項？」（規則 6.1）
           upstream_confirm  模型提議調整上游目標，等主管同意與否
           adjust_info    調整階段資訊仍不足
           weekly         週回報資訊不足（規則 7.5）
  plan     有一版等級表，等主管給信心分數（POST confidence）
  ready    信心 >= 80，執行中，等週回報（POST weekly-report）；週回報產出新表後回到 plan

模型呼叫失敗或回覆不能用時一律丟例外，而且不存檔：session 維持呼叫前的樣子，可以直接重試。
"""

from __future__ import annotations

import difflib
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Callable

from coach.llm import LLMClient, LLMResult
from coach.prompt import (
    ADJUST_CONTRACT,
    WEEKLY_CONTRACT,
    CoachReply,
    adjust_context,
    build_phase_messages,
    intake_input,
    parse_reply,
    weekly_context,
)
from coach.rules import Rules, load_rules
from coach.store import SessionStore

CONFIDENCE_THRESHOLD = 80
SHAKY_QUESTION = "最讓你沒把握的是哪一項？"
READY_MESSAGE = (
    "已進入執行。一週後請回來回報：實際狀況、本週成果分數（0～100）、哪些達成、哪些沒達成、原因。"
)

_ADJUST_KINDS = {"shaky_item", "upstream_confirm", "adjust_info"}


class FlowError(Exception):
    status_code = 400


class InvalidInput(FlowError):
    status_code = 422


class WrongState(FlowError):
    status_code = 409


class BadModelReply(FlowError):
    status_code = 502


class LLMUnavailable(FlowError):
    status_code = 503


@dataclass
class Outcome:
    """每一步回給呼叫端的內容。"""

    session_id: str
    status: str  # clarify | plan | ready
    message: str
    kind: str | None = None  # status=clarify 時：intake | shaky_item | upstream_confirm | adjust_info | weekly
    missing: list[str] = field(default_factory=list)
    version: int | None = None  # 這一步產出（或目前）的等級表版本
    changes: list[dict] = field(default_factory=list)  # 與上一版相比改了哪裡（模型說明，含原因）
    diff: str | None = None  # 與上一版的逐行差異（程式算的，不靠模型）
    analysis: dict | None = None  # 週回報的語意分析
    upstream: dict | None = None
    upstream_decision: str | None = None
    advice: str = ""
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

    def start(self, name: str, ideal: str, current: str) -> Outcome:
        s = {
            "id": self.store.new_id(),
            "created_at": _now(),
            "name": name,
            "ideal": ideal,
            "current": current,
            "status": "clarify",
            "pending": None,
            "intake": {"exchanges": []},
            "versions": [],
            "confidences": [],
            "weekly_reports": [],
        }
        return self._run_intake(s, self._rules_loader())

    def answer(self, session_id: str, text: str) -> Outcome:
        text = text.strip()
        if not text:
            raise InvalidInput("回答不可為空")
        with self._locked(session_id):
            s = self.store.load(session_id)
            pending = s.get("pending")
            if s["status"] != "clarify" or not pending:
                raise WrongState(f"目前狀態是 {s['status']}，沒有待回答的問題")
            kind = pending["kind"]
            if kind == "intake":
                exchanges = s["intake"]["exchanges"]
            elif kind in _ADJUST_KINDS:
                exchanges = s["confidences"][-1]["exchanges"]
            else:
                exchanges = s["weekly_reports"][-1]["exchanges"]
            exchanges[-1]["manager"] = text
            exchanges[-1]["answered_at"] = _now()
            rules = self._rules_loader()
            if kind == "intake":
                return self._run_intake(s, rules)
            if kind in _ADJUST_KINDS:
                return self._run_adjust(s, rules)
            return self._run_weekly(s, rules)

    def confidence(self, session_id: str, score: int) -> Outcome:
        _check_score(score, "信心分數")
        with self._locked(session_id):
            s = self.store.load(session_id)
            if s["status"] != "plan":
                hints = {
                    "clarify": "請先回答待回答的問題",
                    "ready": "已經進入執行；下一步是週回報，不能再給分",
                }
                raise WrongState(f"目前狀態是 {s['status']}，現在不能給信心分數：{hints.get(s['status'], '')}")
            version = s["versions"][-1]
            rec = {
                "version": version["no"],
                "score": score,
                "at": _now(),
                "exchanges": [],
                "result_version": None,  # 這一輪若產出了新版本，記在這裡
            }
            s["confidences"].append(rec)
            if score >= CONFIDENCE_THRESHOLD:
                rec["outcome"] = "ready"
                s["status"], s["pending"] = "ready", None
                self._save(s)
                return Outcome(s["id"], "ready", READY_MESSAGE, version=version["no"], rules=_stored_rules(version))
            rec["outcome"] = "adjusting"
            coach = {"status": "clarify", "kind": "info", "missing": [], "message": SHAKY_QUESTION}
            rec["exchanges"].append({"at": rec["at"], "coach": coach, "manager": None})
            s["status"], s["pending"] = "clarify", {"kind": "shaky_item", "question": SHAKY_QUESTION}
            self._save(s)
            return Outcome(
                s["id"], "clarify", SHAKY_QUESTION, kind="shaky_item", version=version["no"], rules=_stored_rules(version)
            )

    def weekly_report(
        self, session_id: str, actual: str, score: int, achieved: str = "", not_achieved: str = "", reason: str = ""
    ) -> Outcome:
        _check_score(score, "本週成果分數")
        if not actual.strip():
            raise InvalidInput("實際狀況不可為空")
        with self._locked(session_id):
            s = self.store.load(session_id)
            if s["status"] != "ready":
                raise WrongState(f"目前狀態是 {s['status']}；週回報只能在信心分數 >= {CONFIDENCE_THRESHOLD}、進入執行（ready）之後送")
            s["weekly_reports"].append(
                {
                    "no": len(s["weekly_reports"]) + 1,
                    "at": _now(),
                    "based_on_version": s["versions"][-1]["no"],
                    "actual": actual.strip(),
                    "score": score,
                    "achieved": achieved.strip(),
                    "not_achieved": not_achieved.strip(),
                    "reason": reason.strip(),
                    "exchanges": [],
                    "analysis": None,
                    "result_version": None,
                }
            )
            return self._run_weekly(s, self._rules_loader())

    def get(self, session_id: str) -> dict:
        return self.store.load(session_id)

    def seed(self, name: str, ideal: str, current: str, table: str, score: int) -> Outcome:
        """直接用主管提供的等級表建立「已進入執行」的 session，不呼叫模型。

        給週回報測試案例用：週回報通常是針對某一張具體的表寫的，重新生成一張表會對不上。
        """
        _check_score(score, "信心分數")
        if score < CONFIDENCE_THRESHOLD:
            raise InvalidInput(f"要進入執行，信心分數至少 {CONFIDENCE_THRESHOLD}")
        now = _now()
        version = {
            "no": 1,
            "created_at": now,
            "trigger": "provided",
            "based_on": None,
            "rules_version": None,
            "rules_sha8": None,
            "model": None,
            "table": table,
            "changes": [],
            "diff_from_prev": None,
            "upstream_decision": None,
            "advice": "",
        }
        s = {
            "id": self.store.new_id(),
            "created_at": now,
            "name": name,
            "ideal": ideal,
            "current": current,
            "status": "ready",
            "pending": None,
            "intake": {"exchanges": []},
            "versions": [version],
            "confidences": [
                {"version": 1, "score": score, "at": now, "exchanges": [], "result_version": None, "outcome": "ready"}
            ],
            "weekly_reports": [],
        }
        self._save(s)
        return Outcome(s["id"], "ready", READY_MESSAGE, version=1)

    # ---- 各階段：呼叫模型並套用結果 ---------------------------------------

    def _run_intake(self, s: dict, rules: Rules) -> Outcome:
        ex = s["intake"]["exchanges"]
        msgs = build_phase_messages(rules, None, intake_input(s["name"], s["ideal"], s["current"]), ex)
        reply, res = self._call(msgs)
        if reply.status == "clarify":
            return self._ask(s, ex, "intake", reply, rules, res)
        return self._add_version(s, rules, reply, res, trigger="initial")

    def _run_adjust(self, s: dict, rules: Rules) -> Outcome:
        rec = s["confidences"][-1]
        base = s["versions"][rec["version"] - 1]
        asked_upstream = any(e["coach"].get("kind") == "upstream_confirm" for e in rec["exchanges"])
        ctx = adjust_context(
            s["name"], s["ideal"], s["current"], base["no"], base["table"], rec["score"], asked_upstream
        )
        reply, res = self._call(build_phase_messages(rules, ADJUST_CONTRACT, ctx, rec["exchanges"]))
        if reply.status == "clarify":
            if reply.kind not in ("info", "upstream_confirm"):
                reply.kind = "info"
            if reply.kind == "upstream_confirm" and asked_upstream:
                raise BadModelReply("上游目標已經提議過一次，模型又提議了一次；請重試")
            kind = "upstream_confirm" if reply.kind == "upstream_confirm" else "adjust_info"
            return self._ask(s, rec["exchanges"], kind, reply, rules, res)
        if reply.upstream_decision == "declined" and not reply.advice.strip():
            raise BadModelReply("主管不同意調整上游時，模型必須附上建議（advice），但它沒給；請重試")
        out = self._add_version(s, rules, reply, res, trigger="adjust", based_on=base["no"])
        rec["result_version"] = out.version
        self._save(s)
        return out

    def _run_weekly(self, s: dict, rules: Rules) -> Outcome:
        rep = s["weekly_reports"][-1]
        base = s["versions"][rep["based_on_version"] - 1]
        ctx = weekly_context(
            s["name"], s["ideal"], s["current"], base["no"], base["table"], rep, s["weekly_reports"][:-1]
        )
        reply, res = self._call(build_phase_messages(rules, WEEKLY_CONTRACT, ctx, rep["exchanges"]))
        if reply.status == "clarify":
            return self._ask(s, rep["exchanges"], "weekly", reply, rules, res)
        rep["analysis"] = reply.analysis
        out = self._add_version(s, rules, reply, res, trigger="weekly", based_on=base["no"])
        rep["result_version"] = out.version
        self._save(s)
        return out

    # ---- 共用小工具 -----------------------------------------------------

    def _call(self, messages: list[dict[str, str]]) -> tuple[CoachReply, LLMResult]:
        try:
            res = self._llm_factory().complete(messages)
        except Exception as e:  # 缺金鑰、網路、供應商錯誤都一樣處理
            raise LLMUnavailable(f"{type(e).__name__}: {e}") from e
        reply = parse_reply(res.text)
        if reply.status == "unknown":
            raise BadModelReply(f"模型回覆無法解析（{reply.parse_error}）：{res.text[:300]}")
        if not reply.message.strip():
            raise BadModelReply("模型回覆的 message 是空的")
        return reply, res

    def _ask(self, s: dict, exchanges: list[dict], kind: str, reply: CoachReply, rules: Rules, res: LLMResult) -> Outcome:
        exchanges.append({"at": _now(), "coach": reply.as_history(), "manager": None})
        s["status"], s["pending"] = "clarify", {"kind": kind, "question": reply.message}
        self._save(s)
        return Outcome(
            s["id"], "clarify", reply.message, kind=kind, missing=reply.missing, upstream=reply.upstream,
            version=s["versions"][-1]["no"] if s["versions"] else None, rules=_rules_info(rules), llm_result=res,
        )

    def _add_version(
        self, s: dict, rules: Rules, reply: CoachReply, res: LLMResult, trigger: str, based_on: int | None = None
    ) -> Outcome:
        prev = s["versions"][-1] if s["versions"] else None
        no = len(s["versions"]) + 1
        diff = None
        if prev:
            diff = "\n".join(
                difflib.unified_diff(
                    prev["table"].splitlines(), reply.message.splitlines(), f"第 {prev['no']} 版", f"第 {no} 版", lineterm="", n=1
                )
            )
        s["versions"].append(
            {
                "no": no,
                "created_at": _now(),
                "trigger": trigger,  # initial | adjust | weekly | provided
                "based_on": based_on,
                "rules_version": rules.version,
                "rules_sha8": rules.sha256[:8],
                "model": res.model,
                "table": reply.message,
                "changes": reply.changes,
                "diff_from_prev": diff,
                "upstream_decision": reply.upstream_decision,
                "advice": reply.advice,
            }
        )
        s["status"], s["pending"] = "plan", None
        self._save(s)
        return Outcome(
            s["id"], "plan", reply.message, version=no, changes=reply.changes, diff=diff, analysis=reply.analysis,
            upstream_decision=reply.upstream_decision, advice=reply.advice, rules=_rules_info(rules), llm_result=res,
        )

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


def _check_score(score: int, label: str) -> None:
    if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 100:
        raise InvalidInput(f"{label}必須是 0～100 的整數")


def _stored_rules(version: dict) -> dict:
    return {"version": version.get("rules_version"), "sha8": version.get("rules_sha8")}
