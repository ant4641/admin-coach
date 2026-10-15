"""員工名單的驗證。顯示與介面由前端負責；規則與規則檔無關。"""

from __future__ import annotations

import secrets

from coach.prompt import ValidationErrors

MAX_STAFF = 50
MAX_NAME = 20
MAX_ROLE = 30


def validate_staff(items, existing_ids: set[str]) -> list[dict]:
    """整份名單驗證並回傳乾淨版本。id 在 existing_ids 內就保留，否則（含空白）由系統產生。

    不合格丟 ValidationErrors，errors 的 row 是名單第幾列（從 1 開始），field 是 name。
    """
    if not isinstance(items, list):
        raise ValidationErrors("員工名單格式不正確")
    errors: list[dict] = []
    if len(items) > MAX_STAFF:
        errors.append({"row": MAX_STAFF + 1, "field": "name", "message": f"員工最多 {MAX_STAFF} 人"})
    out, seen_names, used_ids = [], set(), set()
    for i, it in enumerate(items, start=1):
        it = it if isinstance(it, dict) else {}
        name = it.get("name").strip() if isinstance(it.get("name"), str) else ""
        role = it.get("role").strip() if isinstance(it.get("role"), str) else ""
        sid = it.get("id") if isinstance(it.get("id"), str) else ""
        if not name:
            errors.append({"row": i, "field": "name", "message": "姓名不得為空"})
        elif len(name) > MAX_NAME:
            errors.append({"row": i, "field": "name", "message": f"姓名最多 {MAX_NAME} 字"})
        elif name in seen_names:
            errors.append({"row": i, "field": "name", "message": "這個姓名已經在名單中"})
        seen_names.add(name)
        if len(role) > MAX_ROLE:
            errors.append({"row": i, "field": "role", "message": f"職稱最多 {MAX_ROLE} 字"})
        if sid not in existing_ids or sid in used_ids:
            sid = ""
        out.append({"id": sid, "name": name, "role": role})
        if sid:
            used_ids.add(sid)
    if errors:
        first = errors[0]
        raise ValidationErrors(first["message"] if first["row"] > MAX_STAFF else f"第 {first['row']} 列：{first['message']}", errors)
    for p in out:
        while not p["id"]:
            cand = secrets.token_hex(3)
            if cand not in existing_ids and cand not in used_ids:
                p["id"] = cand
                used_ids.add(cand)
    return out
