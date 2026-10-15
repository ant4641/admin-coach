"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const Staff = require("../../coach/web/staff.js");

let n = 0;
const newId = () => `new-${++n}`;

test("validateRows：task／product 空白或只有空白字元會出錯，detail 空白合法", () => {
  const rows = [
    { task: "", detail: "", product: "p" },
    { task: "t", detail: "", product: "   " },
    { task: " \u3000 ", detail: "d", product: "\t" },
    { task: "t", detail: "", product: "p" },
  ];
  assert.deepEqual(Staff.validateRows(rows), [
    { row: 1, field: "task", message: "工作項目不得為空" },
    { row: 2, field: "product", message: "副產品不得為空" },
    { row: 3, field: "task", message: "工作項目不得為空" },
    { row: 3, field: "product", message: "副產品不得為空" },
  ]);
});

test("validateRows：全部合法 → 沒有錯誤", () => {
  assert.deepEqual(Staff.validateRows([{ task: "a", detail: "", product: "b", assignee: null }]), []);
  assert.deepEqual(Staff.validateRows([]), []);
});

test("validateStaff：姓名空白、重複、過長、超過上限", () => {
  const e = Staff.validateStaff([{ name: "甲" }, { name: " " }, { name: "甲" }, { name: "長".repeat(21) }, { name: "乙", role: "" }]);
  assert.deepEqual(e, [
    { row: 2, field: "name", message: "姓名不得為空" },
    { row: 3, field: "name", message: "這個姓名已經在名單中" },
    { row: 4, field: "name", message: "姓名最多 20 字" },
  ]);
  const many = Array.from({ length: 51 }, (_, i) => ({ name: `人${i}` }));
  assert.equal(Staff.validateStaff(many).at(-1).message, "員工最多 50 人");
  assert.deepEqual(Staff.validateStaff(many.slice(0, 50)), []);
});

test("parseNames：換行、逗號、頓號分隔，空白略過", () => {
  assert.deepEqual(Staff.parseNames("小明、小華,小美，小強\n\n 阿花 \r\n"), ["小明", "小華", "小美", "小強", "阿花"]);
  assert.deepEqual(Staff.parseNames("   "), []);
  assert.deepEqual(Staff.parseNames(undefined), []);
});

test("parseStaffCsv：BOM、CRLF、引號、空行、標題略過", () => {
  const text = '\uFEFF姓名,職稱\r\n王小明,店長\r\n\r\n"李,小華","店員 ""資深"""\r\n,\r\n陳大文\r\n';
  assert.deepEqual(Staff.parseStaffCsv(text), [
    { name: "王小明", role: "店長" },
    { name: "李,小華", role: '店員 "資深"' },
    { name: "陳大文", role: "" },
  ]);
  for (const h of ["名字", "員工", "Name", "NAME"]) assert.deepEqual(Staff.parseStaffCsv(`${h},職稱\n甲,乙`), [{ name: "甲", role: "乙" }]);
  assert.deepEqual(Staff.parseStaffCsv("甲,乙\n"), [{ name: "甲", role: "乙" }]); // 第一列不是標題就要保留
  assert.deepEqual(Staff.parseStaffCsv(""), []);
});

test("parseCsv：引號內的換行", () => {
  assert.deepEqual(Staff.parseCsv('"a\nb",c\nd'), [["a\nb", "c"], ["d"]]);
});

test("decodeBytes：UTF-8 直接用，Big5 亂碼就改用 big5", () => {
  const utf8 = new TextEncoder().encode("王小明");
  assert.equal(Staff.decodeBytes(utf8), "王小明");
  assert.equal(Staff.decodeBytes(new TextEncoder().encode("\uFEFF姓名")).replace("\uFEFF", ""), "姓名");
  let big5Ok = true;
  try {
    new TextDecoder("big5");
  } catch {
    big5Ok = false;
  }
  if (big5Ok) assert.equal(Staff.decodeBytes(new Uint8Array([0xa4, 0xa4, 0xa4, 0xe5])), "中文"); // Big5 的「中文」，不是合法 UTF-8
});

test("mergeStaff：重複、過長、超過上限、摘要", () => {
  const existing = [{ id: "a", name: "甲", role: "" }];
  const r = Staff.mergeStaff(existing, [{ name: "甲" }, { name: "乙", role: "店員" }, { name: "乙" }, { name: "長".repeat(21) }, { name: " " }], newId);
  assert.equal(r.added, 1);
  assert.equal(r.duplicates, 2);
  assert.equal(r.tooLong, 1);
  assert.deepEqual(r.staff.map((p) => p.name), ["甲", "乙"]);
  assert.equal(r.staff[1].role, "店員");
  assert.equal(existing.length, 1, "不改傳進來的名單");
  assert.equal(Staff.importSummary({ added: 8, duplicates: 2, tooLong: 0, overLimit: 0 }), "匯入 8 人，略過 2 筆重複");

  const full = Array.from({ length: 49 }, (_, i) => ({ id: `s${i}`, name: `人${i}`, role: "" }));
  const r2 = Staff.mergeStaff(full, [{ name: "新1" }, { name: "新2" }, { name: "新3" }], newId);
  assert.equal(r2.staff.length, 50);
  assert.equal(r2.added, 1);
  assert.equal(r2.overLimit, 2);
  assert.match(Staff.importSummary(r2), /超過 50 人上限，略過 2 筆/);
});

test("staffLabel／templateCsv", () => {
  assert.equal(Staff.staffLabel({ name: "王", role: "店長" }), "王（店長）");
  assert.equal(Staff.staffLabel({ name: "王", role: " " }), "王");
  const t = Staff.templateCsv();
  assert.ok(t.startsWith("\uFEFF姓名,職稱\r\n"));
  assert.equal(Staff.parseStaffCsv(t).length, 2);
});
