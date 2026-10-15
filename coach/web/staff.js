"use strict";

// 員工名單與結果表的解析、驗證：不依賴 DOM 的純函式。
// 瀏覽器用 window.Staff，node 測試用 require。規則與後端（coach/staff.py、coach/prompt.py）一致。

(function (root) {
  const MAX_STAFF = 50;
  const MAX_NAME = 20;
  const HEADER_WORDS = ["姓名", "名字", "員工", "name"];

  // ---- 驗證 ---------------------------------------------------------------

  const str = (v) => (typeof v === "string" ? v.trim() : "");

  // 結果表：task、product 去前後空白後不可空；detail 可空。回傳 [{row, field, message}]（row 從 1 開始）
  function validateRows(rows) {
    const errors = [];
    (rows || []).forEach((r, i) => {
      if (!str(r && r.task)) errors.push({ row: i + 1, field: "task", message: "工作項目不得為空" });
      if (!str(r && r.product)) errors.push({ row: i + 1, field: "product", message: "副產品不得為空" });
    });
    return errors;
  }

  // 員工名單：姓名不得為空、最多 20 字、不可重複；總數上限 50。回傳 [{row, field:"name", message}]
  function validateStaff(staff) {
    const errors = [];
    const seen = new Set();
    (staff || []).forEach((p, i) => {
      const name = str(p && p.name);
      if (!name) errors.push({ row: i + 1, field: "name", message: "姓名不得為空" });
      else if (Array.from(name).length > MAX_NAME) errors.push({ row: i + 1, field: "name", message: `姓名最多 ${MAX_NAME} 字` });
      else if (seen.has(name)) errors.push({ row: i + 1, field: "name", message: "這個姓名已經在名單中" });
      if (name) seen.add(name);
    });
    if ((staff || []).length > MAX_STAFF) errors.push({ row: MAX_STAFF + 1, field: "name", message: `員工最多 ${MAX_STAFF} 人` });
    return errors;
  }

  // ---- 解析 ---------------------------------------------------------------

  // 手動輸入／貼上：換行、逗號（, ，）、頓號（、）、分號、Tab 都當分隔；空白項略過
  function parseNames(text) {
    return String(text === undefined || text === null ? "" : text)
      .split(/[\r\n,，、;；\t]+/)
      .map((s) => s.trim())
      .filter(Boolean);
  }

  // 簡易 CSV：支援雙引號（含引號內的逗號、換行、"" 跳脫）、CRLF、BOM；全空白的列略過
  function parseCsv(text) {
    let s = String(text === undefined || text === null ? "" : text);
    if (s.charCodeAt(0) === 0xfeff) s = s.slice(1);
    const rows = [];
    let row = [];
    let cell = "";
    let quoted = false;
    const endCell = () => {
      row.push(cell);
      cell = "";
    };
    const endRow = () => {
      endCell();
      if (row.some((c) => c.trim() !== "")) rows.push(row.map((c) => c.trim()));
      row = [];
    };
    for (let i = 0; i < s.length; i++) {
      const ch = s[i];
      if (quoted) {
        if (ch === '"') {
          if (s[i + 1] === '"') {
            cell += '"';
            i++;
          } else {
            quoted = false;
          }
        } else {
          cell += ch;
        }
      } else if (ch === '"') {
        quoted = true;
      } else if (ch === ",") {
        endCell();
      } else if (ch === "\r") {
        if (s[i + 1] === "\n") i++;
        endRow();
      } else if (ch === "\n") {
        endRow();
      } else {
        cell += ch;
      }
    }
    if (cell !== "" || row.length) endRow();
    return rows;
  }

  // CSV 文字 → [{name, role}]；第一列第一欄若是「姓名／名字／員工／name」當標題略過
  function parseStaffCsv(text) {
    const rows = parseCsv(text);
    if (rows.length && HEADER_WORDS.includes(rows[0][0].toLowerCase())) rows.shift();
    return rows.map((r) => ({ name: r[0] || "", role: r[1] || "" })).filter((p) => p.name);
  }

  // 位元組 → 文字：先用嚴格的 UTF-8；失敗（亂碼）就改用 Big5（台灣的 Excel 常存成 Big5）
  function decodeBytes(bytes, Decoder) {
    const D = Decoder || TextDecoder;
    try {
      return new D("utf-8", { fatal: true }).decode(bytes);
    } catch {
      return new D("big5").decode(bytes);
    }
  }

  // ---- 合併名單 -----------------------------------------------------------

  // 把 incoming（[{name, role}]）加進 existing（[{id, name, role}]）。
  // 回傳 { staff, added, duplicates, tooLong, overLimit }；newId() 產生暫時 id
  function mergeStaff(existing, incoming, newId) {
    const staff = (existing || []).map((p) => ({ ...p }));
    const names = new Set(staff.map((p) => str(p.name)));
    const out = { staff, added: 0, duplicates: 0, tooLong: 0, overLimit: 0 };
    for (const p of incoming || []) {
      const name = str(p.name);
      if (!name) continue;
      if (Array.from(name).length > MAX_NAME) out.tooLong++;
      else if (names.has(name)) out.duplicates++;
      else if (staff.length >= MAX_STAFF) out.overLimit++;
      else {
        names.add(name);
        staff.push({ id: newId(), name, role: str(p.role) });
        out.added++;
      }
    }
    return out;
  }

  function importSummary(r) {
    const parts = [`匯入 ${r.added} 人`];
    if (r.duplicates) parts.push(`略過 ${r.duplicates} 筆重複`);
    if (r.tooLong) parts.push(`略過 ${r.tooLong} 筆姓名超過 ${MAX_NAME} 字`);
    if (r.overLimit) parts.push(`超過 ${MAX_STAFF} 人上限，略過 ${r.overLimit} 筆`);
    return parts.join("，");
  }

  // ---- 顯示與範本 ---------------------------------------------------------

  function staffLabel(p) {
    const name = str(p && p.name);
    const role = str(p && p.role);
    return role ? `${name}（${role}）` : name;
  }

  // 表格顯示用：姓名與職稱分開（職稱沒填就是空字串）
  function staffParts(p) {
    return { name: str(p && p.name), role: str(p && p.role) };
  }

  function templateCsv() {
    return "﻿姓名,職稱\r\n王小明,店長\r\n李小華,店員\r\n";
  }

  const Staff = { MAX_STAFF, MAX_NAME, validateRows, validateStaff, parseNames, parseCsv, parseStaffCsv, decodeBytes, mergeStaff, importSummary, staffLabel, staffParts, templateCsv };
  if (typeof module !== "undefined" && module.exports) module.exports = Staff;
  else root.Staff = Staff;
})(typeof window !== "undefined" ? window : globalThis);
