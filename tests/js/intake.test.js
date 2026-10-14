"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const Intake = require("../../coach/web/intake.js");

// 產業清單：第 20 項是「其他」
const ctx = { industryCount: 20, isOther: (n) => n === 20 };

// 依序答完指定的幾題，回傳狀態；每題都必須驗證通過
function answer(state, ...texts) {
  let s = state;
  for (const t of texts) {
    const r = Intake.submit(s, t, ctx);
    assert.equal(r.ok, true, `「${t}」應該通過`);
    s = r.state;
  }
  return s;
}

const upToSales = () => answer(Intake.create(), "林主管", "9", "做設計", "還可以"); // 之後是第 5 題（分數）

test("第 1 題不能後退", () => {
  const s = Intake.create();
  assert.equal(Intake.canBack(s), false);
  assert.equal(Intake.back(s).ok, false);
});

test("第 2～6 題都能後退", () => {
  let s = Intake.create();
  for (const t of ["林主管", "9", "做設計", "還可以", "6"]) {
    s = answer(s, t);
    assert.equal(Intake.canBack(s), true, `答完「${t}」之後是下一題，應可後退`);
  }
});

test("六題都必填：空白與只有空白都不能前進，狀態不變", () => {
  for (const text of ["", "   ", "\n\t "]) {
    const s = Intake.create();
    const r = Intake.submit(s, text, ctx);
    assert.equal(r.ok, false);
    assert.equal(r.hint, "這題需要回答喔");
    assert.equal(s.step, 0);
  }
  // 每一題都一樣
  let s = Intake.create();
  const good = ["林主管", "9", "做設計", "還可以", "6", "原因"];
  for (let i = 0; i < 6; i++) {
    assert.equal(Intake.submit(s, "  ", ctx).ok, false, `第 ${i + 1} 題空白應失敗`);
    s = answer(s, good[i]);
  }
  assert.equal(s.done, true);
});

test("答案前後空白會被去掉", () => {
  const s = answer(Intake.create(), "  林主管  ");
  assert.equal(s.values.name, "林主管");
});

test("產業編號必須是 1～20 的整數", () => {
  const s = answer(Intake.create(), "林主管");
  for (const bad of ["0", "21", "9.5", "abc", "-1", "九"]) {
    const r = Intake.submit(s, bad, ctx);
    assert.equal(r.ok, false, `「${bad}」不該通過`);
    assert.match(r.hint, /1～20/);
  }
});

test("分數不合法時不可前進", () => {
  const s = upToSales();
  assert.equal(Intake.currentKey(s), "score");
  for (const bad of ["0", "11", "5.5", "abc", "-3", "", "十"]) {
    const r = Intake.submit(s, bad, ctx);
    assert.equal(r.ok, false, `「${bad}」不該通過`);
  }
  assert.equal(Intake.submit(s, "abc", ctx).hint, "分數請填 1～10 的整數喔");
  assert.equal(Intake.submit(s, "1", ctx).ok, true);
  assert.equal(Intake.submit(s, "10", ctx).ok, true);
});

test("後退後各題答案都保留，並成為預填值", () => {
  let s = answer(Intake.create(), "林主管", "9", "做設計", "還可以");
  const back = Intake.back(s);
  assert.equal(back.ok, true);
  s = back.state;
  assert.equal(Intake.currentKey(s), "sales");
  assert.equal(Intake.prefill(s), "還可以");
  assert.deepEqual(s.values, { name: "林主管", industry: 9, industry_other: "", product: "做設計", sales: "還可以" });
  s = Intake.back(s).state; // 回到產品題（第 3 題）
  assert.equal(Intake.prefill(s), "做設計");
});

test("前進、後退保留答案：修改一題後，後面已回答的內容仍是預填值，且不會自動前進", () => {
  // 答到第 6 題（還沒答），再一路退回稱呼題修改
  let s = answer(Intake.create(), "林主管", "9", "做設計", "還可以", "6");
  for (let i = 0; i < 5; i++) s = Intake.back(s).state;
  assert.equal(s.step, 0);
  assert.equal(Intake.prefill(s), "林主管");
  s = answer(s, "王主管");
  assert.equal(s.step, 1, "只前進一題，不會自動送出後面的");
  assert.equal(s.done, false);
  assert.equal(Intake.prefill(s), "9", "產業題預填先前的編號");
  s = answer(s, "9");
  assert.equal(Intake.prefill(s), "做設計");
  s = answer(s, "做設計");
  assert.equal(Intake.prefill(s), "還可以");
  s = answer(s, "還可以");
  assert.equal(Intake.prefill(s), "6");
  assert.equal(s.values.name, "王主管");
});

test("後退後重新驗證：預填值不合法時一樣不能前進", () => {
  let s = answer(Intake.create(), "林主管", "9", "做設計", "還可以", "6");
  s = Intake.back(s).state; // 回到分數題
  assert.equal(Intake.prefill(s), "6");
  assert.equal(Intake.submit(s, "99", ctx).ok, false);
  assert.equal(Intake.submit(s, "", ctx).ok, false);
  assert.equal(Intake.submit(s, "7", ctx).ok, true);
});

test("產業選 20：同一步再追問自填內容，且自填內容必填", () => {
  let s = answer(Intake.create(), "林主管");
  const r = Intake.submit(s, "20", ctx);
  assert.equal(r.ok, true);
  assert.equal(r.asksOther, true);
  s = r.state;
  assert.equal(s.other, true);
  assert.equal(s.step, 1, "還在產業這一步");
  assert.equal(Intake.submit(s, "  ", ctx).ok, false);
  s = answer(s, "修繕");
  assert.equal(s.other, false);
  assert.equal(s.step, 2);
  assert.equal(s.values.industry_other, "修繕");
});

test("在「請填寫你的產業」按上一題：回到產業題，預填先前選的編號", () => {
  let s = answer(Intake.create(), "林主管", "20");
  assert.equal(s.other, true);
  const b = Intake.back(s);
  assert.equal(b.ok, true);
  s = b.state;
  assert.equal(s.other, false);
  assert.equal(s.step, 1);
  assert.equal(Intake.prefill(s), "20");
});

test("從第 3 題上一題：回到產業題，預填先前的編號；選 20 再送出時預填自填內容", () => {
  let s = answer(Intake.create(), "林主管", "20", "修繕");
  assert.equal(s.step, 2);
  s = Intake.back(s).state;
  assert.equal(s.step, 1);
  assert.equal(s.other, false);
  assert.equal(Intake.prefill(s), "20");
  const r = Intake.submit(s, "20", ctx);
  assert.equal(r.asksOther, true);
  assert.equal(Intake.prefill(r.state), "修繕", "自填內容預填先前的");
  assert.equal(Intake.submit(r.state, "修繕", ctx).state.step, 2);
});

test("改選別的編號時清除先前的自填內容", () => {
  let s = answer(Intake.create(), "林主管", "20", "修繕", "做修繕");
  s = Intake.back(Intake.back(s).state).state; // 第 4 題 → 第 3 題 → 產業題
  assert.equal(s.step, 1);
  s = answer(s, "9");
  assert.equal(s.values.industry, 9);
  assert.equal(s.values.industry_other, "");
  assert.equal(s.step, 2);
  // 之後即使再選 20，也不會帶出舊的自填內容
  s = Intake.back(s).state;
  const r = Intake.submit(s, "20", ctx);
  assert.equal(Intake.prefill(r.state), "");
});

test("改選別的編號再改回 20：自填內容已被清除，要重填", () => {
  let s = answer(Intake.create(), "林主管", "20", "修繕");
  s = Intake.back(s).state;
  s = answer(s, "5"); // 改選 5 → 清除
  s = Intake.back(s).state;
  const r = Intake.submit(s, "20", ctx);
  assert.equal(Intake.prefill(r.state), "");
});

test("全部回答完後不可後退，也不能再送出", () => {
  const s = answer(Intake.create(), "林主管", "9", "做設計", "還可以", "6", "剛好");
  assert.equal(s.done, true);
  assert.equal(Intake.canBack(s), false);
  assert.equal(Intake.back(s).ok, false);
  assert.equal(Intake.submit(s, "再答一題", ctx).ok, false);
});

test("狀態是不可變的：submit 與 back 不會改到傳進來的物件", () => {
  const s = answer(Intake.create(), "林主管", "9");
  const snapshot = JSON.stringify(s);
  Intake.submit(s, "做設計", ctx);
  Intake.back(s);
  assert.equal(JSON.stringify(s), snapshot);
});

test("restore：壞掉的暫存資料會重來；合法的會還原（含產業 20 的追問步驟與預填值）", () => {
  for (const bad of [null, undefined, "x", {}, { step: 9, values: {} }, { step: -1, values: {} }, { step: "1", values: {} }]) {
    assert.deepEqual(Intake.restore(bad), Intake.create());
  }
  const saved = JSON.parse(JSON.stringify(answer(Intake.create(), "林主管", "20")));
  const r = Intake.restore(saved);
  assert.equal(r.other, true);
  assert.equal(r.step, 1);
  assert.equal(r.values.industry, 20);
  const mid = Intake.back(answer(Intake.create(), "林主管", "9", "做設計")).state;
  assert.equal(Intake.prefill(Intake.restore(JSON.parse(JSON.stringify(mid)))), "做設計");
});

test("hasProgress：全新的沒有，答過或有預填值就有", () => {
  assert.equal(Intake.hasProgress(Intake.create()), false);
  assert.equal(Intake.hasProgress(answer(Intake.create(), "林主管")), true);
  const back = Intake.back(answer(Intake.create(), "林主管")).state; // 回到第 1 題，但有預填值
  assert.equal(Intake.hasProgress(back), true);
});

test("displayQuestion：第 1 題不加稱呼", () => {
  assert.equal(Intake.displayQuestion(0, "該如何稱呼您？", "陳姐"), "該如何稱呼您？");
});

test("displayQuestion：第 2～6 題（含選 20 的追問）開頭加「稱呼，」", () => {
  const questions = [
    "該如何稱呼您？",
    "你在哪個產業？",
    "你覺得自己/團隊/公司在賣什麼產品？",
    "賣的如何？",
    "為自己的營運打個分數（1~10分）？",
    "為何給這個分數？",
  ];
  for (let i = 1; i < questions.length; i++) {
    assert.equal(Intake.displayQuestion(i, questions[i], "陳姐"), `陳姐，${questions[i]}`);
  }
  assert.equal(Intake.displayQuestion(1, "請填寫你的產業", "陳姐"), "陳姐，請填寫你的產業");
});

test("displayQuestion：稱呼前後空白會去掉", () => {
  assert.equal(Intake.displayQuestion(2, "賣的如何？", "  陳姐 \t"), "陳姐，賣的如何？");
});

test("displayQuestion：沒有稱呼時不會出現開頭逗號或 undefined", () => {
  for (const name of [undefined, null, "", "   ", 5]) {
    const text = Intake.displayQuestion(3, "賣的如何？", name);
    assert.equal(text, "賣的如何？");
    assert.ok(!text.startsWith("，") && !text.includes("undefined"));
  }
});

test("displayQuestion：不會重複加兩次（同一題文字、同一稱呼，結果一致；原文不被改動）", () => {
  const q = "你在哪個產業？";
  const once = Intake.displayQuestion(1, q, "陳姐");
  assert.equal(Intake.displayQuestion(1, q, "陳姐"), once);
  assert.equal(q, "你在哪個產業？");
  assert.equal(once.split("陳姐，").length - 1, 1);
});

test("改了稱呼之後，後面的題目用新稱呼", () => {
  let s = answer(Intake.create(), "陳姐", "9");
  s = Intake.back(Intake.back(s).state).state; // 回到第 1 題
  s = answer(s, "  林主管 ");
  assert.equal(s.values.name, "林主管");
  assert.equal(Intake.displayQuestion(s.step, "你在哪個產業？", s.values.name), "林主管，你在哪個產業？");
});
