"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

globalThis.window = globalThis;
require("../../coach/web/export-image.js");

// 假的 canvas context：每個字寬 10
const ctx = { font: "", measureText: (t) => ({ width: Array.from(t).length * 10 }) };

test("圖片匯出：task／detail／product 分欄計算，列高依內容", () => {
  const data = {
    vfp: "V",
    byproducts: [
      { task: "備料", detail: "", product: "備好的食材" },
      { task: "烘烤", detail: "預熱烤箱，依配方設定溫度與時間".repeat(6), product: "出爐麵包" },
      { task: "交付", detail: "交給客人", product: "長".repeat(120) },
    ],
  };
  const L = window.exportJpeg.layout(ctx, data);
  const [a, b, c] = L.rows;
  assert.deepEqual(a.taskLines, ["備料"]);
  assert.deepEqual(a.detailLines, []);
  assert.deepEqual(a.productLines, ["備好的食材"]);
  assert.ok(b.detailLines.length > 1 && b.h > a.h, "detail 長 → 列高變高");
  assert.deepEqual(c.detailLines, ["交給客人"]);
  assert.ok(c.productLines.length > 1 && c.h > a.h, "product 長 → 列高變高");
  assert.equal(L.rows.length, 3);
});

const fmt = (x) => JSON.stringify(x);

test("匯出資料整理：新格式、舊格式、缺值都不含 undefined／null", () => {
  const out = window.exportJpeg.prepare({
    vfp: "V",
    byproducts: [
      { task: " 備料 ", detail: "先備好", product: "食材" }, // 新格式
      { name: "烘烤", description: "預熱烤箱" }, // 舊格式：name→task、description→detail、product 空白
      { task: "交付" }, // 缺 detail、product
      {}, // 全缺
      { task: null, detail: undefined, product: 5 },
      null,
    ],
  });
  assert.deepEqual(out.byproducts[0], { task: "備料", detail: "先備好", product: "食材", owner: "（未指派）", ownerRole: "" });
  assert.deepEqual(out.byproducts[1], { task: "烘烤", detail: "預熱烤箱", product: "（未填）", owner: "（未指派）", ownerRole: "" });
  assert.deepEqual(out.byproducts[2], { task: "交付", detail: "", product: "（未填）", owner: "（未指派）", ownerRole: "" });
  assert.deepEqual(out.byproducts[3], { task: "（未填）", detail: "", product: "（未填）", owner: "（未指派）", ownerRole: "" });
  assert.deepEqual(out.byproducts[4], { task: "（未填）", detail: "", product: "5", owner: "（未指派）", ownerRole: "" });
  assert.equal(out.vfp, "V");
  assert.ok(!/undefined|null/.test(fmt(out)));
  assert.equal(window.exportJpeg.prepare({}).vfp, "（未填）");
});

test("圖片表頭是序號／工作項目／副產品，且排版不含 undefined", () => {
  assert.deepEqual(window.exportJpeg.HEADERS, ["序號", "工作項目", "副產品", "負責人"]);
  const L = window.exportJpeg.layout(ctx, window.exportJpeg.prepare({ vfp: "V", byproducts: [{ name: "舊", description: "d" }, {}] }));
  assert.ok(!/undefined|null/.test(fmt(L.rows)));
});

test("負責人欄：姓名一行、職稱另一行（小字）；沒職稱不留空行；沒指派顯示（未指派）；不含 undefined", () => {
  const out = window.exportJpeg.prepare({
    vfp: "V",
    byproducts: [
      { task: "a", detail: "", product: "p", owner: " 王小明 ", ownerRole: " 店長 " },
      { task: "b", product: "q" },
      { task: "c", product: "r", owner: null },
      { task: "d", product: "s", owner: "李小華", ownerRole: "" },
      { task: "e", product: "t", owner: "", ownerRole: "不該出現" },
    ],
  });
  assert.deepEqual(out.byproducts.map((b) => b.owner), ["王小明", "（未指派）", "（未指派）", "李小華", "（未指派）"]);
  assert.deepEqual(out.byproducts.map((b) => b.ownerRole), ["店長", "", "", "", ""]);
  const L = window.exportJpeg.layout(ctx, out);
  assert.deepEqual(L.rows[0].ownerLines, ["王小明"]);
  assert.deepEqual(L.rows[0].roleLines, ["店長"]);
  assert.deepEqual(L.rows[3].roleLines, [], "沒有職稱不留空行");
  assert.ok(L.rows[0].ownerH > L.rows[3].ownerH, "有職稱比較高");
  assert.ok(!/undefined|null/.test(JSON.stringify(L.rows)));
  const long = window.exportJpeg.layout(ctx, window.exportJpeg.prepare({ vfp: "V", byproducts: [{ task: "a", product: "p", owner: "王", ownerRole: "長".repeat(60) }] }));
  assert.ok(long.rows[0].roleLines.length > 1 && long.rows[0].h > L.rows[0].h, "職稱太長 → 換行、列高變高，不會被切掉");
});
