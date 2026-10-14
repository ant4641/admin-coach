"""匯出 Excel（.xlsx）。內容與版面由程式決定，與規則檔無關。"""

from __future__ import annotations

import io
import math
from datetime import date
from urllib.parse import quote

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.styles.borders import Border, Side

from coach.display import width

XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
SHEET_NAME = "產品鑑定"

NAVY_900 = "0C3467"
NAVY_800 = "0F325E"
GOLD_LABEL = "F4C84D"  # VFP 格（深金）
GOLD_CONTENT = "FBE08A"  # VFP 內容格（淡金）
YELLOW_400 = "F4C84D"
SKY_100 = "DCEBFA"
MIST_100 = "E8EEF5"
SLATE_700 = "2D4A6E"
GRAY_600 = "5B6B80"

COL_WIDTHS = {"A": 8, "B": 28, "C": 60}  # 字元寬度（中文字算 2）


def download_filename(day: date) -> str:
    return f"管到飛_產品鑑定_{day:%Y%m%d}.xlsx"


def content_disposition(day: date) -> str:
    """中文檔名用 filename*=UTF-8''...；另附 ASCII 備用檔名給舊瀏覽器。"""
    ascii_name = f"coach_{day:%Y%m%d}.xlsx"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(download_filename(day))}"


def industry_short(industry: str) -> str:
    """產業只顯示括號前的名稱；「其他：xxx」照原樣。"""
    industry = (industry or "").strip()
    if industry.startswith("其他"):
        return industry
    return industry.split("（", 1)[0].strip()


def info_line(inputs: dict | None, day: date) -> str:
    """「產業：…　稱呼：…　日期：YYYY-MM-DD」；沒有的欄位整個省略。"""
    inputs = inputs or {}
    parts = []
    industry = industry_short(inputs.get("industry") or "")
    if industry:
        parts.append(f"產業：{industry}")
    name = (inputs.get("name") or "").strip()
    if name:
        parts.append(f"稱呼：{name}")
    parts.append(f"日期：{day:%Y-%m-%d}")
    return "　".join(parts)


def _lines(text: str, chars: int) -> int:
    """估計換行後的行數（寬度以中文字 2、英數 1 計算）。"""
    total = 0
    for para in (text or "").split("\n"):
        total += max(1, math.ceil(width(para) / max(chars - 2, 1)))
    return total


def _height(*cells: tuple[str, int]) -> float:
    return 20 * max(_lines(t, c) for t, c in cells) + 6


def build_xlsx(vfp: str, byproducts: list[dict], day: date, inputs: dict | None = None) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = SHEET_NAME
    for col, w in COL_WIDTHS.items():
        ws.column_dimensions[col].width = w

    thin = Side(style="thin", color="9CC3EA")
    box = Border(left=thin, right=thin, top=thin, bottom=thin)
    center = Alignment(horizontal="center", vertical="top", wrap_text=True)
    left = Alignment(horizontal="left", vertical="top", wrap_text=True)
    head_fill = PatternFill("solid", fgColor=NAVY_800)
    head_font = Font(bold=True, color="FFFFFF")
    body_font = Font(color=SLATE_700)

    # 第 1 列：資訊（產業、稱呼、日期），在表格上方
    ws["A1"] = info_line(inputs, day)
    ws.merge_cells("A1:C1")
    ws["A1"].font = Font(size=10, color=GRAY_600)
    ws["A1"].alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 22

    # 第 2 列：VFP 與內容（內容跨 B:C），金色，navy 粗體
    ws["A2"], ws["B2"] = "VFP", vfp
    ws.merge_cells("B2:C2")
    gold_font = Font(bold=True, color=NAVY_900)
    ws["A2"].fill, ws["A2"].font = PatternFill("solid", fgColor=GOLD_LABEL), gold_font
    ws["B2"].fill, ws["B2"].font = PatternFill("solid", fgColor=GOLD_CONTENT), gold_font
    for ref in ("A2", "B2", "C2"):
        ws[ref].alignment, ws[ref].border = center, box
    ws.row_dimensions[2].height = _height((vfp, COL_WIDTHS["B"] + COL_WIDTHS["C"]))

    # 第 3 列：表頭（下緣黃色粗線）
    heavy = Border(left=thin, right=thin, top=thin, bottom=Side(style="thick", color=YELLOW_400))
    for ref, text in (("A3", "序號"), ("B3", "副產品"), ("C3", "說明")):
        ws[ref] = text
        ws[ref].fill, ws[ref].font, ws[ref].alignment, ws[ref].border = head_fill, head_font, center, heavy

    # 副產品：一列一個，依序
    row = 4
    for i, b in enumerate(byproducts, start=1):
        fill = PatternFill("solid", fgColor=MIST_100 if i % 2 else "FFFFFF")
        for col, value, align in (("A", i, center), ("B", b["name"], center), ("C", b["description"], left)):
            c = ws[f"{col}{row}"]
            c.value, c.alignment, c.fill, c.font, c.border = value, align, fill, body_font, box
        ws.row_dimensions[row].height = _height((b["name"], COL_WIDTHS["B"]), (b["description"], COL_WIDTHS["C"]))
        row += 1

    # 頁尾小字
    foot = row + 1
    ws[f"A{foot}"] = f"由管到飛產生，日期：{day:%Y-%m-%d}"
    ws.merge_cells(f"A{foot}:C{foot}")
    ws[f"A{foot}"].font = Font(size=9, color=GRAY_600)
    ws[f"A{foot}"].alignment = Alignment(horizontal="center", vertical="center")

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
