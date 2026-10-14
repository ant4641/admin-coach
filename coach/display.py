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


def render_table(vfp: str, byproducts: list[dict], max_name: int = 24, max_desc: int = 48) -> str:
    name_w = max([width("副產品"), *(min(width(b["name"]), max_name) for b in byproducts)])
    desc_w = max([width("說明"), min(width(vfp), max_desc), *(min(width(b["description"]), max_desc) for b in byproducts)])

    def line(fill: str) -> str:
        return f"+{fill * (name_w + 2)}+{fill * (desc_w + 2)}+"

    def row(left: str, right: str) -> list[str]:
        a, b = _wrap(left, name_w), _wrap(right, desc_w)
        out = []
        for i in range(max(len(a), len(b))):
            x = a[i] if i < len(a) else ""
            y = b[i] if i < len(b) else ""
            out.append(f"| {x}{' ' * (name_w - width(x))} | {y}{' ' * (desc_w - width(y))} |")
        return out

    lines = [line("-"), *row("VFP", vfp), line("-"), *row("副產品", "說明"), line("=")]
    for b in byproducts:
        lines += [*row(b["name"], b["description"]), line("-")]
    return "\n".join(lines)
