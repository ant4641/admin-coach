"use strict";

// 對話頁六題的狀態邏輯：不依賴 DOM 的純函式（狀態都是新物件，不改傳進來的）。
// 瀏覽器用 window.Intake，node 測試用 require。
//
// 狀態：{ step, values, other, done }
//   step    目前第幾題（0～5）；6＝六題都答完
//   values  各題答案：name、industry（編號）、industry_other、product、sales、score（數字）、reason
//           回頭修改時，後面各題已回答的內容會留著，當作預填值（不會自動送出）
//   other   產業選了「其他」，正在問「請填寫你的產業」（和產業題視為同一步）
//   done    六題都答完（呼叫 POST /sessions 之前的那一刻）；之後不能再後退

(function (root) {
  const FIELDS = [
    { key: "name", required: true },
    { key: "industry", required: true },
    { key: "product", required: true },
    { key: "sales", required: true },
    { key: "score", required: true },
    { key: "reason", required: true },
  ];
  const INDUSTRY_STEP = 1;
  const EMPTY_HINT = "這題需要回答喔";
  const SCORE_HINT = "分數請填 1～10 的整數喔";

  const CN_DIGITS = { 零: 0, 〇: 0, 一: 1, 二: 2, 兩: 2, 三: 3, 四: 4, 五: 5, 六: 6, 七: 7, 八: 8, 九: 9 };

  // 口語數字轉阿拉伯數字（一～二十、兩、十一～十九、「第八項」「8號」，去掉前後標點與空白）；轉不出來就原文回傳
  function normalizeNumber(text) {
    const raw = String(text === undefined || text === null ? "" : text);
    const t = raw
      .replace(/[\s\u3000]+/g, "")
      .replace(/^(?:(?![-+－＋])[\p{P}\p{S}])+|[\p{P}\p{S}]+$/gu, "")
      .replace(/^第/, "")
      .replace(/(號碼|號|項|題|個|分)$/, "");
    if (/^\d+$/.test(t)) return t;
    if (t === "十") return "10";
    if (t === "二十" || t === "廿") return "20";
    let m = t.match(/^十([一二三四五六七八九])$/);
    if (m) return String(10 + CN_DIGITS[m[1]]);
    m = t.match(/^二十([一二三四五六七八九])$/);
    if (m) return String(20 + CN_DIGITS[m[1]]);
    if (/^[一二兩三四五六七八九]$/.test(t)) return String(CN_DIGITS[t]);
    return raw;
  }

  function create() {
    return { step: 0, values: {}, other: false, done: false };
  }

  // 從 sessionStorage 讀回來的東西不一定可信：形狀不對就重來
  function restore(raw) {
    if (!raw || typeof raw !== "object" || !raw.values || typeof raw.values !== "object") return create();
    const step = raw.step;
    if (!Number.isInteger(step) || step < 0 || step >= FIELDS.length) return create();
    return { step, values: { ...raw.values }, other: Boolean(raw.other) && step === INDUSTRY_STEP, done: false };
  }

  function currentKey(state) {
    return FIELDS[state.step] ? FIELDS[state.step].key : null;
  }

  // ctx: { industryCount, isOther(n) }；回傳 { ok, hint } 或 { ok, value }
  function validate(state, text, ctx) {
    const t = String(text === undefined || text === null ? "" : text).trim();
    const field = FIELDS[state.step];
    if (!field) return { ok: false, hint: "" };
    if (!t) return { ok: false, hint: EMPTY_HINT }; // 六題（含「請填寫你的產業」）都必填
    if (state.other) return { ok: true, value: t };
    if (field.key === "industry") {
      const nt = normalizeNumber(t); // 轉換先做，驗證後做
      const n = /^\d+$/.test(nt) ? Number(nt) : 0;
      if (n < 1 || n > ctx.industryCount) {
        return { ok: false, hint: `請輸入 1～${ctx.industryCount} 的編號，或直接點選清單項目。` };
      }
      return { ok: true, value: n };
    }
    if (field.key === "score") {
      const nt = normalizeNumber(t);
      const n = /^\d+$/.test(nt) ? Number(nt) : 0;
      if (n < 1 || n > 10) return { ok: false, hint: SCORE_HINT };
      return { ok: true, value: n };
    }
    return { ok: true, value: t };
  }

  // 送出目前這一題。成功回 { ok:true, state, asksOther }；驗證不過回 { ok:false, hint }，狀態不變
  function submit(state, text, ctx) {
    if (state.done) return { ok: false, hint: "" };
    const v = validate(state, text, ctx);
    if (!v.ok) return v;
    const values = { ...state.values };
    let { step, other } = state;
    let asksOther = false;
    if (state.other) {
      values.industry_other = v.value;
      other = false;
      step += 1;
    } else if (FIELDS[step].key === "industry") {
      if (values.industry !== v.value) values.industry_other = ""; // 改選別的編號：清掉先前的自填內容
      values.industry = v.value;
      if (ctx.isOther(v.value)) {
        other = true; // 選「其他」：同一步再追問自填內容（若先前填過，會預填）
        asksOther = true;
      } else {
        values.industry_other = "";
        step += 1;
      }
    } else {
      values[FIELDS[step].key] = v.value;
      step += 1;
    }
    return { ok: true, state: { step, values, other, done: step >= FIELDS.length }, asksOther };
  }

  function canBack(state) {
    if (state.done) return false;
    return state.other || state.step > 0;
  }

  // 上一題：保留所有已填的答案。產業題與「請填寫你的產業」視為同一步
  function back(state) {
    if (!canBack(state)) return { ok: false };
    let { step, other } = state;
    if (other) {
      other = false; // 從「請填寫你的產業」回到產業題
    } else if (step === INDUSTRY_STEP + 1) {
      step = INDUSTRY_STEP; // 第 3 題回到產業題
    } else {
      step -= 1;
    }
    return { ok: true, state: { step, values: { ...state.values }, other, done: false } };
  }

  // 目前這一題輸入框的預填值
  function prefill(state) {
    if (state.other) return state.values.industry_other || "";
    const key = currentKey(state);
    const v = key ? state.values[key] : undefined;
    return v === undefined || v === null ? "" : String(v);
  }

  // 顯示用的問句：第 2～6 題（index 1～5）開頭加「{稱呼}，」；第 1 題不加。
  // 稱呼去掉前後空白；沒有稱呼就不加（不會出現開頭逗號或 undefined）。question 本身不被改動
  function displayQuestion(index, question, name) {
    const n = typeof name === "string" ? name.trim() : "";
    return index > 0 && n ? `${n}，${question}` : question;
  }

  function hasProgress(state) {
    if (state.step > 0 || state.other) return true;
    return Object.values(state.values).some((v) => v !== undefined && v !== null && v !== "");
  }

  const Intake = { normalizeNumber, displayQuestion, FIELDS, EMPTY_HINT, SCORE_HINT, create, restore, validate, submit, canBack, back, prefill, hasProgress, currentKey };
  if (typeof module !== "undefined" && module.exports) module.exports = Intake;
  else root.Intake = Intake;
})(typeof window !== "undefined" ? window : globalThis);
