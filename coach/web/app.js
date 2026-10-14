"use strict";

const $ = (id) => document.getElementById(id);

const state = { sessionId: null, industries: [], rows: [] };

// ---- API ---------------------------------------------------------------

// 錯誤訊息一律簡短中文：502 / 503 的原始訊息很長，不直接顯示
async function api(method, path, body) {
  let res;
  try {
    res = await fetch(path, {
      method,
      headers: { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new Error("無法連線到伺服器，請稍後再試");
  }
  let data = null;
  try {
    data = await res.json();
  } catch {
    /* 沒有 JSON 內容 */
  }
  if (res.ok) return data;
  if (res.status === 502) throw new Error("教練的回覆格式有誤，請再按一次送出");
  if (res.status === 503) throw new Error("目前無法連線到模型，請稍後再試");
  if (data && typeof data.detail === "string") throw new Error(data.detail);
  throw new Error(`請求失敗（${res.status}）`);
}

// ---- 共用小工具 ----------------------------------------------------------

function showError(msg) {
  const box = $("error");
  box.textContent = msg || "";
  box.hidden = !msg;
}

function setBusy(busy, button, busyText) {
  button.disabled = busy;
  if (busyText) {
    button.dataset.label = button.dataset.label || button.textContent;
    button.textContent = busy ? busyText : button.dataset.label;
  }
  $("loading").hidden = !(busy && button.id === "answer-btn");
}

function addBubble(who, text) {
  const div = document.createElement("div");
  div.className = `bubble ${who === "coach" ? "coach" : "manager"}`;
  const label = document.createElement("span");
  label.className = "who";
  label.textContent = who === "coach" ? "教練" : "你";
  const body = document.createElement("span");
  body.textContent = text;
  div.append(label, body);
  $("chat").appendChild(div);
  div.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

function autoSize(el) {
  el.style.height = "auto";
  el.style.height = `${el.scrollHeight + 2}px`;
}

// ---- 1. 表單 -------------------------------------------------------------

function currentIndustry() {
  const n = parseInt($("industry").value, 10);
  return Number.isInteger(n) && n >= 1 && n <= state.industries.length ? n : null;
}

function isOther(n) {
  return n !== null && state.industries[n - 1].name.startsWith("其他");
}

function refreshIndustry() {
  const n = currentIndustry();
  document.querySelectorAll("#industry-list li").forEach((li) => {
    li.classList.toggle("selected", Number(li.dataset.no) === n);
  });
  $("other-field").hidden = !isOther(n);
}

async function loadIndustries() {
  try {
    state.industries = await api("GET", "/industries");
  } catch (e) {
    showError(`${e.message}（產業清單載入失敗，請重新整理頁面）`);
    return;
  }
  const list = $("industry-list");
  state.industries.forEach((it) => {
    const li = document.createElement("li");
    li.value = it.no;
    li.dataset.no = it.no;
    li.textContent = it.name;
    li.addEventListener("click", () => {
      $("industry").value = it.no;
      refreshIndustry();
    });
    list.appendChild(li);
  });
}

function readForm() {
  const name = $("name").value.trim();
  if (!name) throw new Error("請填寫稱呼");
  const industry = currentIndustry();
  if (industry === null) throw new Error(`產業請輸入 1～${state.industries.length || 20} 的編號`);
  const other = $("industry-other").value.trim();
  if (isOther(industry) && !other) throw new Error("選「其他」時請填寫你的產業");
  const scoreText = $("score").value.trim();
  let score = null;
  if (scoreText) {
    score = Number(scoreText);
    if (!Number.isInteger(score) || score < 1 || score > 10) throw new Error("營運自評請輸入 1～10 的整數，或留空");
  }
  return {
    name,
    industry,
    industry_other: isOther(industry) ? other : "",
    product: $("product").value.trim(),
    sales: $("sales").value.trim(),
    score,
    reason: $("reason").value.trim(),
  };
}

$("industry").addEventListener("input", refreshIndustry);

$("start-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  showError("");
  let body;
  try {
    body = readForm();
  } catch (e) {
    showError(e.message);
    return;
  }
  const btn = $("start-btn");
  setBusy(true, btn, "教練思考中…");
  try {
    const reply = await api("POST", "/sessions", body);
    state.sessionId = reply.session_id;
    $("form-card").hidden = true;
    $("chat-card").hidden = false;
    handleReply(reply);
  } catch (e) {
    showError(e.message);
  } finally {
    setBusy(false, btn, "教練思考中…");
  }
});

// ---- 2. 對話 -------------------------------------------------------------

function handleReply(reply) {
  if (reply.status === "clarify") {
    addBubble("coach", reply.question);
    $("answer-form").hidden = false;
    $("answer").value = "";
    $("answer").focus();
  } else {
    $("answer-form").hidden = true;
    showResult(reply);
  }
}

$("answer-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  showError("");
  const text = $("answer").value.trim();
  if (!text) {
    showError("請先輸入回答");
    return;
  }
  const btn = $("answer-btn");
  setBusy(true, btn);
  try {
    const reply = await api("POST", `/sessions/${state.sessionId}/answer`, { answer: text });
    addBubble("manager", text); // 成功才留在紀錄裡；失敗時輸入框保留原文可重送
    handleReply(reply);
  } catch (e) {
    showError(e.message);
  } finally {
    setBusy(false, btn);
  }
});

// ---- 3. 結果（可編輯）-----------------------------------------------------

function showResult(reply) {
  state.rows = reply.byproducts.map((b) => ({ name: b.name, description: b.description }));
  $("vfp").value = reply.vfp;
  $("result-card").hidden = false;
  renderRows();
  autoSize($("vfp"));
  $("result-card").scrollIntoView({ block: "start", behavior: "smooth" });
}

function markDirty() {
  $("save-msg").textContent = "";
}

function cell(className, field, index) {
  const td = document.createElement("td");
  td.className = className;
  const ta = document.createElement("textarea");
  ta.rows = 1;
  ta.value = state.rows[index][field];
  ta.setAttribute("aria-label", field === "name" ? `副產品 ${index + 1} 名稱` : `副產品 ${index + 1} 說明`);
  ta.addEventListener("input", () => {
    state.rows[index][field] = ta.value;
    autoSize(ta);
    markDirty();
  });
  td.appendChild(ta);
  return td;
}

function iconButton(label, text, disabled, onClick) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = "icon-btn";
  b.textContent = text;
  b.title = label;
  b.setAttribute("aria-label", label);
  b.disabled = disabled;
  b.addEventListener("click", onClick);
  return b;
}

function renderRows() {
  const tbody = $("rows");
  tbody.replaceChildren();
  state.rows.forEach((_, i) => {
    const tr = document.createElement("tr");
    const act = document.createElement("td");
    const box = document.createElement("div");
    box.className = "row-actions";
    const move = (delta) => {
      const j = i + delta;
      [state.rows[i], state.rows[j]] = [state.rows[j], state.rows[i]];
      markDirty();
      renderRows();
    };
    box.append(
      iconButton("上移", "↑", i === 0, () => move(-1)),
      iconButton("下移", "↓", i === state.rows.length - 1, () => move(1)),
      iconButton("刪除", "✕", false, () => {
        state.rows.splice(i, 1);
        markDirty();
        renderRows();
      })
    );
    act.appendChild(box);
    tr.append(cell("name", "name", i), cell("desc", "description", i), act);
    tbody.appendChild(tr);
  });
  tbody.querySelectorAll("textarea").forEach(autoSize);
}

$("vfp").addEventListener("input", () => {
  autoSize($("vfp"));
  markDirty();
});

$("add-btn").addEventListener("click", () => {
  state.rows.push({ name: "", description: "" });
  markDirty();
  renderRows();
  const last = $("rows").lastElementChild;
  if (last) last.querySelector("textarea").focus();
});

$("save-btn").addEventListener("click", async () => {
  showError("");
  const btn = $("save-btn");
  $("save-msg").textContent = "";
  setBusy(true, btn, "儲存中…");
  try {
    const body = {
      vfp: $("vfp").value.trim(),
      byproducts: state.rows.map((r) => ({ name: r.name.trim(), description: r.description.trim() })),
    };
    await api("PUT", `/sessions/${state.sessionId}/byproducts`, body);
    $("save-msg").textContent = "已儲存";
  } catch (e) {
    showError(e.message);
  } finally {
    setBusy(false, btn, "儲存中…");
  }
});

loadIndustries();
