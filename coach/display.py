"""命令列顯示：把 vfp 與副產品畫成文字表格（中文字寬度對齊）。顯示由程式負責，與規則檔無關。"""

from __future__ import annotations

import unicodedata


def width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def _wrap(text: str, limit: int) -> list[str]:
    lines: list[str] = []
    for para in text.splitlines() or [""]:
        cur, cur_w = "", 0
        for ch in para:
            w = width(ch)
            if cur_w + w > limit:
                lines.append(cur)
                cur, cur_w = "", 0
            cur += ch
            cur_w += w
        lines.append(cur)
    return lines


def _task_cell(b: dict) -> str:
    return f"{b['task']}\n{b['detail']}" if b.get("detail") else b["task"]


def _cell_width(text: str, limit: int) -> int:
    return max(min(width(line), limit) for line in text.splitlines() or [""])


def render_table(vfp: str, byproducts: list[dict], max_task: int = 44, max_product: int = 36) -> str:
    """兩欄：工作項目（task，下一行接 detail）、副產品（product）；VFP 列放在最上面。"""
    left_w = max([width("工作項目"), width("VFP"), *(_cell_width(_task_cell(b), max_task) for b in byproducts)])
    right_w = max([width("副產品"), min(width(vfp), max_product), *(_cell_width(b["product"], max_product) for b in byproducts)])

    def line(fill: str) -> str:
        return f"+{fill * (left_w + 2)}+{fill * (right_w + 2)}+"

    def row(left: str, right: str) -> list[str]:
        a, b = _wrap(left, left_w), _wrap(right, right_w)
        out = []
        for i in range(max(len(a), len(b))):
            x = a[i] if i < len(a) else ""
            y = b[i] if i < len(b) else ""
            out.append(f"| {x}{' ' * (left_w - width(x))} | {y}{' ' * (right_w - width(y))} |")
        return out

    lines = [line("-"), *row("VFP", vfp), line("-"), *row("工作項目", "副產品"), line("=")]
    for b in byproducts:
        lines += [*row(_task_cell(b), b["product"]), line("-")]
    return "\n".join(lines)
