"use strict";

const $ = (id) => document.getElementById(id);

// 頁尾版本字樣：取自載入這支 app.js 時的 ?v=（後端依檔案修改時間帶入），方便確認瀏覽器載入的是不是最新程式
(function showVersion() {
  try {
    const v = Number(new URL(document.currentScript.src).searchParams.get("v"));
    const el = document.getElementById("app-version");
    if (el && v) {
      const d = new Date(v * 1000);
      const p = (n) => String(n).padStart(2, "0");
      el.textContent = `v${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`;
    }
  } catch {
    /* 沒有版本參數就不顯示 */
  }
})();

// ---- sessionStorage（讀寫都包 try，私密視窗等情況不能讓頁面壞掉）----------------

const KEY_SESSION = "coach_session";
const KEY_INTAKE = "coach_intake";
const store = {
  get(k) {
    try {
      return sessionStorage.getItem(k);
    } catch {
      return null;
    }
  },
  set(k, v) {
    try {
      sessionStorage.setItem(k, v);
    } catch {
      /* 忽略 */
    }
  },
  del(k) {
    try {
      sessionStorage.removeItem(k);
    } catch {
      /* 忽略 */
    }
  },
};

// ---- API ---------------------------------------------------------------

// 錯誤訊息一律簡短中文：502 / 503 的原始訊息很長，不直接顯示
async function request(method, path, body) {
  let res;
  try {
    res = await fetch(path, {
      method,
      headers: { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw apiError("無法連線到伺服器，請稍後再試", 0);
  }
  if (res.ok) return res;
  let data = null;
  try {
    data = await res.json();
  } catch {
    /* 沒有 JSON 內容 */
  }
  if (res.status === 502) throw apiError("教練的回覆格式有誤，請再按一次送出", 502);
  if (res.status === 503) throw apiError("目前無法連線到模型，請稍後再試", 503);
  if (res.status === 429) throw apiError("目前使用的人比較多，請稍後再試", 429);
  if (data && typeof data.detail === "string") {
    const err = apiError(data.detail, res.status);
    if (Array.isArray(data.errors)) err.errors = data.errors;
    throw err;
  }
  throw apiError(`請求失敗（${res.status}）`, res.status);
}

function apiError(message, status) {
  const e = new Error(message);
  e.status = status;
  return e;
}

async function api(method, path, body) {
  return (await request(method, path, body)).json();
}

// ---- 共用小工具 ----------------------------------------------------------

function showError(msg) {
  const box = $("error");
  box.textContent = msg || "";
  box.hidden = !msg;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function ymd(d = new Date()) {
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}`;
}

function autoSize(el) {
  el.style.height = "auto";
  el.style.height = `${el.scrollHeight + 2}px`;
}

// ---- 路由：#/ → #/chat → #/result ----------------------------------------

const PAGES = { "#/": "home", "#/chat": "chat", "#/result": "result" };
let currentPage = null;
let revealNext = false; // 下一次進結果頁要不要播揭曉動畫
let navToken = 0; // 每次換頁加一；非同步動作回來時用來判斷使用者是否已離開

const LEAVE_MESSAGE = "你有尚未儲存的修改，確定要離開嗎？";

async function route() {
  stopVoice(true);
  const hash = location.hash;
  if (!PAGES[hash]) {
    location.replace("#/");
    return;
  }
  const next = PAGES[hash];
  if (currentPage === "result" && next === "result") return; // 擋回來的 hashchange，不重畫
  if (currentPage === "result" && next !== "result" && isDirty() && !confirm(LEAVE_MESSAGE)) {
    location.hash = "#/result"; // 瀏覽器上一頁被取消：留在結果頁
    return;
  }
  navToken += 1;
  const token = navToken;
  currentPage = next;
  showError("");
  closeMenu();
  document.body.className = `page-${next}`;
  document.querySelectorAll("[data-page]").forEach((el) => {
    el.hidden = el.dataset.page !== next;
  });
  window.scrollTo(0, 0);
  if (next !== "result") revealNext = false;
  if (next === "chat") await enterChat(token);
  if (next === "result") await enterResult(token);
}

window.addEventListener("hashchange", route);

function go(hash) {
  if (location.hash === hash) route();
  else location.hash = hash;
}

// ---- 對話頁：泡泡 --------------------------------------------------------

const chat = $("chat");

function addBubble(who, text, extra) {
  const div = document.createElement("div");
  div.className = `bubble ${who === "coach" ? "coach" : "manager"}`;
  const label = document.createElement("span");
  label.className = "who";
  label.textContent = who === "coach" ? "教練" : "你";
  const body = document.createElement("span");
  body.textContent = text;
  div.append(label, body);
  if (extra) div.appendChild(extra);
  chat.appendChild(div);
  div.scrollIntoView({ block: "nearest", behavior: "smooth" });
  return div;
}

function setLoading(text) {
  $("loading").hidden = !text;
  if (text) {
    $("loading-text").textContent = text;
    $("loading").scrollIntoView({ block: "nearest", behavior: "smooth" });
  }
}

let finishing = false; // 收到 vfp 後的「教練整理中…」期間，不讓 setBusy 把載入提示關掉

function setBusy(busy) {
  if (busy) stopVoice(true);
  $("mic-btn").disabled = busy;
  $("answer-btn").disabled = busy;
  $("prev-btn").disabled = busy; // 送出途中不能回頭
  if (!finishing) setLoading(busy ? "教練思考中…" : null);
}

function clearChat() {
  stopVoice(true);
  chat.replaceChildren();
  setLoading(null);
  $("chat-done").hidden = true;
  $("answer-form").hidden = true;
  $("answer").value = "";
}

// 輸入框下方的一行提醒（教練口吻）；空字串就隱藏
function setAnswerHint(text) {
  $("answer-hint").textContent = text || "";
  $("answer-hint").hidden = !text;
}

function showForm({ placeholder }) {
  $("answer-form").hidden = false;
  $("answer").placeholder = placeholder;
  setAnswerHint("");
  stopVoice(true);
  updateVoiceHint();
  $("answer").focus({ preventScroll: true });
}

// ---- 語音輸入（瀏覽器內建的 SpeechRecognition；純函式在 voice.js）--------------------

const SpeechCtor = Voice.recognitionCtor(window);
let recog = null; // 目前進行中的辨識；null＝待命

function updateVoiceHint() {
  const coarse = Boolean(window.matchMedia && window.matchMedia("(pointer: coarse)").matches);
  $("voice-hint").textContent = Voice.hintText(Boolean(SpeechCtor), coarse);
}

function setListeningUi(on) {
  const btn = $("mic-btn");
  btn.classList.toggle("listening", on);
  btn.setAttribute("aria-pressed", on ? "true" : "false");
  $("voice-hint").textContent = on ? "聆聽中，請說話…再按一次停止" : "";
  if (!on) updateVoiceHint();
}

// discard＝true：直接放棄（切題、送出、離開頁面）；false：使用者再按一次，讓辨識把已聽到的收尾
function stopVoice(discard) {
  if (!recog) return;
  const r = recog;
  if (discard) {
    recog = null;
    setListeningUi(false);
    try {
      r.abort();
    } catch {
      /* 忽略 */
    }
  } else {
    try {
      r.stop();
    } catch {
      /* 忽略 */
    }
  }
}

function startVoice() {
  if (!SpeechCtor || recog || $("mic-btn").disabled) return;
  const box = $("answer");
  const base = box.value; // 按下當下已有的文字，辨識結果接在它後面
  const r = new SpeechCtor();
  r.lang = "zh-TW";
  r.interimResults = true;
  r.continuous = false;
  r.maxAlternatives = 1;
  r.onresult = (ev) => {
    if (recog !== r) return;
    const { finalText, interimText } = Voice.splitResults(ev.results); // 每次都由整段重算，暫時結果被覆蓋、不會累加
    box.value = Voice.mergeTranscript(base, finalText, interimText);
    setAnswerHint("");
  };
  r.onerror = (ev) => {
    if (recog !== r) return;
    if (ev.error !== "aborted") setAnswerHint(Voice.errorMessage(ev.error));
  };
  r.onend = () => {
    if (recog !== r) return;
    recog = null;
    setListeningUi(false);
    box.focus({ preventScroll: true });
  };
  recog = r;
  setListeningUi(true);
  try {
    r.start();
  } catch {
    recog = null;
    setListeningUi(false);
    setAnswerHint(Voice.errorMessage(""));
  }
}

if (SpeechCtor) {
  $("mic-btn").hidden = false;
  $("mic-btn").addEventListener("click", () => (recog ? stopVoice(false) : startVoice()));
}
window.addEventListener("pagehide", () => stopVoice(true));

// ---- 前端寫死的六題（文字與規則檔第 2 章一致）-------------------------------

// 每題一個物件：
//   question     教練問句
//   placeholder  輸入框提示
//   hint         說明（選用）：只在「目前這一題」顯示，回答後消失
//   examples     舉例（選用，陣列）：只是文字，不能點、不會自動填入
// 必填與驗證規則在 intake.js。要替其他題加說明或舉例，只改這裡。hint／examples 的字要與規則檔 coach_rules.md 一致（有測試比對）。
const QUESTIONS = [
  { key: "name", question: "該如何稱呼您？", placeholder: "例如：楊主管、林先生、張董" },
  { key: "industry", question: "你在哪個產業？", placeholder: "輸入編號，或點選上面的項目" },
  {
    key: "product",
    question: "你覺得自己/團隊/公司在賣什麼產品？",
    placeholder: "實體物品或無實體的服務都可以",
    hint: "寫越詳盡越好",
    examples: ["好吃的麵", "讓人吃了想再來買的麵包"],
  },
  { key: "sales", question: "賣的如何？", placeholder: "不一定要有數據，至少說好或壞的程度" },
  { key: "score", question: "為自己的營運打個分數（1~10分）？", placeholder: "輸入 1～10 的整數" },
  { key: "reason", question: "為何給這個分數？", placeholder: "簡短說明即可" },
];
const OTHER_PROMPT = "請填寫你的產業";

// 第 2～6 題（含「請填寫你的產業」）的問句開頭加稱呼；QUESTIONS 裡維持原文，稱呼只在顯示時組合（intake.js）
const askText = (index, question, name) => Intake.displayQuestion(index, question, name);
const BLANK = "（未填）"; // 還原由 API 建立、欄位是空白的 session 時顯示

let industries = [];
let mode = "intake"; // intake：還在問六題 | session：已建立 session，回答追問
let intake = Intake.create(); // 六題的狀態與規則都在 intake.js（不依賴 DOM 的純函式），這裡只負責畫面

function saveIntake() {
  store.set(KEY_INTAKE, JSON.stringify(intake));
}

function loadIntake() {
  try {
    return Intake.restore(JSON.parse(store.get(KEY_INTAKE)));
  } catch {
    return Intake.create(); // 沒有或壞掉就重來
  }
}

async function ensureIndustries() {
  if (industries.length) return;
  industries = await api("GET", "/industries");
}

function isOtherNo(n) {
  return industries[n - 1] && industries[n - 1].name.startsWith("其他");
}

function industryDisplay(v) {
  const n = v.industry;
  const name = industries[n - 1] ? industries[n - 1].name : "";
  return v.industry_other ? `其他：${v.industry_other}` : `${n} ${name}`;
}

function answerDisplay(i, v) {
  const key = QUESTIONS[i].key;
  if (key === "industry") return industryDisplay(v);
  const val = v[key];
  return val === undefined || val === "" || val === null ? BLANK : String(val);
}

function industryListNode() {
  const ol = document.createElement("ol");
  ol.className = "bubble-list";
  industries.forEach((it) => {
    const li = document.createElement("li");
    li.value = it.no;
    li.textContent = it.name;
    li.tabIndex = 0;
    const pick = () => {
      if (ol.classList.contains("locked")) return;
      $("answer").value = String(it.no);
      $("answer-form").requestSubmit();
    };
    li.addEventListener("click", pick);
    li.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " ") {
        ev.preventDefault();
        pick();
      }
    });
    ol.appendChild(li);
  });
  return ol;
}

function lockIndustryLists() {
  chat.querySelectorAll(".bubble-list").forEach((ol) => ol.classList.add("locked"));
}

// 問目前這一題（六題或「其他」的追問）
// 題目的說明與舉例（小字，在同一個泡泡內）；沒有就回傳 null
function helpNode(q) {
  const lines = [];
  if (q.hint) lines.push(q.hint);
  if (q.examples && q.examples.length) lines.push(`例如：${q.examples.join("　/　")}`);
  if (!lines.length) return null;
  const box = document.createElement("div");
  box.className = "q-help";
  lines.forEach((line) => {
    const p = document.createElement("p");
    p.textContent = line;
    box.appendChild(p);
  });
  return box;
}

// 這一題回答成功後，泡泡只留問句
function clearQuestionHelp() {
  chat.querySelectorAll(".q-help").forEach((el) => el.remove());
}

function intakeCtx() {
  return { industryCount: industries.length, isOther: (n) => Boolean(isOtherNo(n)) };
}

function updatePrevButton() {
  $("prev-btn").hidden = !(mode === "intake" && Intake.canBack(intake));
}

// 問目前這一題（六題或「其他」的追問），輸入框預填先前的回答
function askCurrent() {
  stopVoice(true);
  if (intake.other) {
    addBubble("coach", askText(1, OTHER_PROMPT, intake.values.name));
    showForm({ placeholder: "請填寫你的產業" });
  } else {
    const q = QUESTIONS[intake.step];
    addBubble("coach", askText(intake.step, q.question, intake.values.name), q.key === "industry" ? industryListNode() : helpNode(q));
    showForm({ placeholder: q.placeholder });
  }
  const box = $("answer");
  box.value = Intake.prefill(intake);
  box.setSelectionRange(box.value.length, box.value.length);
  updatePrevButton();
}

// 依狀態重畫整段六題對話：已回答的題（不含目前這一題）各一組泡泡，再問目前這一題
function renderIntake() {
  clearChat();
  for (let i = 0; i < intake.step; i++) {
    addBubble("coach", askText(i, QUESTIONS[i].question, intake.values.name));
    addBubble("manager", answerDisplay(i, intake.values));
  }
  if (intake.other) {
    // 產業已選「其他」、還沒填文字：把產業題與回答補上再追問
    addBubble("coach", askText(1, QUESTIONS[1].question, intake.values.name));
    addBubble("manager", `${intake.values.industry} ${industries[intake.values.industry - 1].name}`);
  }
  askCurrent();
}

function startIntake() {
  mode = "intake";
  intake = loadIntake();
  renderIntake();
}

// 剛送出的這一題，在對話裡顯示成什麼
function submittedDisplay(prev, next) {
  const v = next.values;
  if (prev.other) return v.industry_other;
  const key = Intake.currentKey(prev);
  if (key === "industry") return `${v.industry} ${industries[v.industry - 1].name}`;
  return String(v[key]);
}

function startBody(v) {
  const num = (x) => (x === undefined || x === "" ? null : x);
  return {
    name: v.name,
    industry: v.industry,
    industry_other: v.industry_other || "",
    product: v.product || "",
    sales: v.sales || "",
    score: num(v.score),
    reason: v.reason || "",
  };
}

async function submitIntake(text) {
  const result = Intake.submit(intake, text, intakeCtx());
  if (!result.ok) {
    // 不送出、不加泡泡：提醒顯示在輸入框下方，輸入框保留原文與焦點
    setAnswerHint(result.hint);
    $("answer").focus();
    return;
  }
  setAnswerHint("");
  const bubble = addBubble("manager", submittedDisplay(intake, result.state));
  lockIndustryLists();
  if (!result.state.done) {
    clearQuestionHelp();
    intake = result.state;
    saveIntake();
    askCurrent();
    return;
  }
  // 六題問完：建立 session。失敗時拿掉剛加的泡泡，狀態不變、輸入框保留原文，可重送
  setBusy(true);
  try {
    const reply = await api("POST", "/sessions", startBody(result.state.values));
    clearQuestionHelp();
    intake = Intake.create();
    store.del(KEY_INTAKE);
    store.set(KEY_SESSION, reply.session_id);
    mode = "session";
    updatePrevButton();
    $("answer").value = "";
    handleReply(reply);
  } catch (e) {
    bubble.remove();
    showError(e.message);
  } finally {
    setBusy(false);
  }
}

$("prev-btn").addEventListener("click", () => {
  if ($("prev-btn").disabled || mode !== "intake") return;
  stopVoice(true);
  const r = Intake.back(intake);
  if (!r.ok) return;
  showError("");
  intake = r.state;
  saveIntake();
  renderIntake();
});

// ---- 建立 session 之後：追問與回答 -----------------------------------------

function handleReply(reply) {
  if (reply.status === "clarify") {
    addBubble("coach", reply.question);
    showForm({ placeholder: "輸入你的回答" });
  } else {
    finishConversation(); // 對話頁不顯示 VFP，也不把它傳進來
  }
}

// 對話頁（泡泡、按鈕、aria、title）不出現 VFP 與副產品內容；要看結果得打開結果頁
const DONE_MESSAGE = "我整理好了，準備好看你的最終有價值產品了嗎？";
const OPEN_RESULT = "打開最終結果";

async function finishConversation() {
  $("answer-form").hidden = true;
  const token = navToken;
  finishing = true;
  setLoading("教練整理中…");
  await sleep(1000);
  finishing = false;
  setLoading(null);
  if (token !== navToken) return; // 這 1 秒內使用者已經離開，不自動做任何事
  addBubble("coach", DONE_MESSAGE);
  $("done-label").hidden = true;
  $("to-result").textContent = OPEN_RESULT;
  $("chat-done").hidden = false;
  $("to-result").focus({ preventScroll: true });
}

async function submitAnswer(text) {
  if (!text) {
    setAnswerHint(Intake.EMPTY_HINT);
    $("answer").focus();
    return;
  }
  setAnswerHint("");
  const bubble = addBubble("manager", text);
  setBusy(true);
  try {
    const reply = await api("POST", `/sessions/${store.get(KEY_SESSION)}/answer`, { answer: text });
    $("answer").value = "";
    handleReply(reply);
  } catch (e) {
    bubble.remove();
    showError(e.message);
  } finally {
    setBusy(false);
  }
}

$("answer-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  if ($("answer-btn").disabled) return;
  stopVoice(true);
  showError("");
  const text = $("answer").value.trim();
  if (mode === "intake") await submitIntake(text);
  else await submitAnswer(text);
});

// Enter 送出、Shift+Enter 換行；正在用輸入法選字時不送出
$("answer").addEventListener("input", () => setAnswerHint(""));
$("answer").addEventListener("keydown", (ev) => {
  if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing && ev.keyCode !== 229) {
    ev.preventDefault();
    $("answer-form").requestSubmit();
  }
});

$("to-result").addEventListener("click", () => {
  revealNext = true; // 只有從這顆按鈕進入結果頁才播揭曉動畫
  go("#/result");
});

// ---- 進入對話頁：依 sessionStorage 還原 --------------------------------------

function replaySession(s) {
  const v = s.inputs;
  QUESTIONS.forEach((q, i) => {
    addBubble("coach", askText(i, q.question, v.name));
    let shown;
    if (q.key === "industry") shown = v.industry;
    else if (q.key === "score") shown = v.score === null ? BLANK : String(v.score);
    else shown = v[q.key] || BLANK;
    addBubble("manager", shown);
  });
  s.exchanges.forEach((ex) => {
    addBubble("coach", ex.coach.question);
    if (ex.manager !== null && ex.manager !== undefined) addBubble("manager", ex.manager);
  });
  if (s.status === "vfp") {
    addBubble("coach", DONE_MESSAGE);
    $("done-label").hidden = false;
    $("to-result").textContent = OPEN_RESULT;
    $("chat-done").hidden = false;
    $("to-result").focus({ preventScroll: true });
  } else {
    showForm({ placeholder: "輸入你的回答" });
  }
}

async function enterChat(token) {
  clearChat();
  try {
    await ensureIndustries();
  } catch (e) {
    showError(`${e.message}（產業清單載入失敗，請重新整理頁面）`);
    return;
  }
  if (token !== navToken) return;
  const sid = store.get(KEY_SESSION);
  if (sid) {
    try {
      const s = await api("GET", `/sessions/${sid}`);
      if (token !== navToken) return;
      mode = "session";
      replaySession(s);
      return;
    } catch (e) {
      if (e.status !== 404) {
        showError(e.message);
        return;
      }
      store.del(KEY_SESSION); // 伺服器上已經沒有這個 session：當作新的開始
    }
  }
  if (token !== navToken) return;
  startIntake();
}

// ---- 結果頁 --------------------------------------------------------------

const result = { rows: [], staff: [], saved: "", inputs: {} };

let tempIdSeq = 0;
const newTempId = () => `new-${Date.now().toString(36)}-${++tempIdSeq}`; // 新員工在存檔前的暫時 id，存檔後換成系統產生的

const blankRow = () => ({ task: "", detail: "", product: "", assignee: null, errors: {} });
const trimRow = (r) => ({ task: r.task.trim(), detail: r.detail.trim(), product: r.product.trim(), assignee: r.assignee || null });
const trimStaff = (p) => ({ id: p.id, name: p.name.trim(), role: p.role.trim() });
const staffById = (id) => result.staff.find((p) => p.id === id);
const staffName = (p) => Staff.staffLabel(p) || "（未命名）";

// 指派用員工姓名記錄（姓名不重複），這樣暫時 id 換成正式 id 不會讓畫面誤判成「有修改」
function snapshot() {
  return JSON.stringify({
    vfp: $("vfp").value.trim(),
    rows: result.rows.map((r) => {
      const p = staffById(r.assignee);
      return [r.task.trim(), r.detail.trim(), r.product.trim(), p ? p.name.trim() : ""];
    }),
    staff: result.staff.map((p) => [p.name.trim(), p.role.trim()]),
  });
}

function isDirty() {
  return !$("page-result").hidden && result.saved !== "" && snapshot() !== result.saved;
}

function markDirty() {
  $("save-msg").textContent = "";
}

async function enterResult(token) {
  const sid = store.get(KEY_SESSION);
  if (!sid) {
    go("#/");
    return;
  }
  let s;
  try {
    s = await api("GET", `/sessions/${sid}`);
  } catch (e) {
    if (e.status === 404) {
      store.del(KEY_SESSION);
      go("#/");
    } else {
      showError(e.message);
    }
    return;
  }
  if (token !== navToken) return;
  if (s.status !== "vfp") {
    go("#/chat");
    return;
  }
  const cur = s.result.edited || s.result.original;
  result.inputs = s.inputs || {};
  result.staff = (s.staff || []).map((p) => ({ id: p.id, name: p.name || "", role: p.role || "" }));
  result.rows = cur.byproducts.map((b) => ({ ...blankRow(), task: b.task || "", detail: b.detail || "", product: b.product || "", assignee: b.assignee || null }));
  updateStaffButton();
  updateErrorSummary();
  $("vfp").value = cur.vfp;
  $("save-msg").textContent = "";
  renderRows();
  autoSize($("vfp"));
  result.saved = snapshot();
  if (revealNext) startReveal();
  revealNext = false;
}

// ---- 揭曉動畫：金色 VFP 列淡入＋上浮，其他列依序淡入；只播一次 -----------------

let revealTimer = null;

function startReveal() {
  if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) return; // 減少動態：直接顯示
  const card = $("result-card");
  card.classList.add("reveal");
  document.querySelectorAll("#rows tr").forEach((tr, i) => {
    tr.style.animationDelay = `${Math.min(150 + i * 40, 700)}ms`; // 每列約 40ms，整體不超過 1 秒
  });
  clearTimeout(revealTimer);
  revealTimer = setTimeout(stopReveal, 1100);
}

function stopReveal() {
  clearTimeout(revealTimer);
  $("result-card").classList.remove("reveal");
  document.querySelectorAll("#rows tr").forEach((tr) => (tr.style.animationDelay = ""));
}

function clearFieldError(obj, name, el) {
  if (!obj.errors || !obj.errors[name]) return;
  delete obj.errors[name];
  el.classList.remove("invalid");
  el.removeAttribute("aria-invalid");
  el.removeAttribute("aria-describedby");
  const msg = el.dataset.errId && document.getElementById(el.dataset.errId);
  if (msg) msg.remove();
  updateErrorSummary();
}

// 在控制項下方加錯誤小字，並標上 aria-invalid／aria-describedby
function markInvalid(el, id, message) {
  el.classList.add("invalid");
  el.setAttribute("aria-invalid", "true");
  el.setAttribute("aria-describedby", id);
  el.dataset.errId = id;
  const p = document.createElement("p");
  p.className = "field-error";
  p.id = id;
  p.textContent = message;
  return p;
}

// 回傳 DocumentFragment：輸入框，若這格有錯誤再加一行紅字
function field(index, name, label, className, placeholder) {
  const row = result.rows[index];
  const ta = document.createElement("textarea");
  ta.rows = 1;
  ta.className = className;
  ta.value = row[name];
  if (placeholder) ta.placeholder = placeholder;
  ta.setAttribute("aria-label", `第 ${index + 1} 項${label}`);
  ta.addEventListener("input", () => {
    row[name] = ta.value;
    autoSize(ta);
    markDirty();
    clearFieldError(row, name, ta);
  });
  const frag = document.createDocumentFragment();
  frag.appendChild(ta);
  if (row.errors[name]) frag.appendChild(markInvalid(ta, `err-${index}-${name}`, row.errors[name]));
  return frag;
}

// 負責人下拉：未指派＋員工名單；名單為空時停用，點擊開啟名單面板
function assigneeControl(index) {
  const row = result.rows[index];
  const sel = document.createElement("select");
  sel.className = "assignee-select";
  sel.setAttribute("aria-label", `第 ${index + 1} 項負責人`);
  if (!result.staff.length) {
    sel.add(new Option("請先新增員工名單", ""));
    sel.classList.add("is-disabled");
    sel.setAttribute("aria-disabled", "true");
    const open = (ev) => {
      ev.preventDefault();
      openStaffDialog();
    };
    sel.addEventListener("mousedown", open);
    sel.addEventListener("keydown", (ev) => {
      if (["Enter", " ", "ArrowDown", "ArrowUp"].includes(ev.key)) open(ev);
    });
  } else {
    sel.add(new Option("未指派", ""));
    result.staff.forEach((p) => sel.add(new Option(staffName(p), p.id)));
    sel.value = staffById(row.assignee) ? row.assignee : "";
    sel.addEventListener("change", () => {
      row.assignee = sel.value || null;
      markDirty();
      clearFieldError(row, "assignee", sel);
    });
  }
  const frag = document.createDocumentFragment();
  frag.appendChild(sel);
  if (row.errors.assignee) frag.appendChild(markInvalid(sel, `err-${index}-assignee`, row.errors.assignee));
  return frag;
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
  stopReveal(); // 編輯造成重畫時不要重播
  const tbody = $("rows");
  tbody.replaceChildren();
  result.rows.forEach((_, i) => {
    const tr = document.createElement("tr");
    const act = document.createElement("td");
    act.className = "act";
    const box = document.createElement("div");
    box.className = "row-actions";
    const move = (delta) => {
      const j = i + delta;
      [result.rows[i], result.rows[j]] = [result.rows[j], result.rows[i]]; // 列物件整個換位置，負責人與錯誤標示跟著走
      markDirty();
      renderRows();
    };
    box.append(
      iconButton("上移", "↑", i === 0, () => move(-1)),
      iconButton("下移", "↓", i === result.rows.length - 1, () => move(1)),
      iconButton("刪除", "✕", false, () => {
        result.rows.splice(i, 1);
        markDirty();
        renderRows();
        updateErrorSummary();
      })
    );
    act.appendChild(box);
    const seq = document.createElement("td");
    seq.className = "seq";
    seq.textContent = String(i + 1);
    const taskTd = document.createElement("td");
    taskTd.className = "name";
    taskTd.append(field(i, "task", "工作項目", "task"), field(i, "detail", "工作內容", "detail", "工作內容說明"));
    const productTd = document.createElement("td");
    productTd.className = "desc";
    productTd.appendChild(field(i, "product", "副產品", "product", "（未填）"));
    const assigneeTd = document.createElement("td");
    assigneeTd.className = "assignee";
    assigneeTd.appendChild(assigneeControl(i));
    tr.append(seq, taskTd, productTd, assigneeTd, act);
    tbody.appendChild(tr);
  });
  tbody.querySelectorAll("textarea").forEach(autoSize);
}

$("vfp").addEventListener("input", () => {
  autoSize($("vfp"));
  markDirty();
});

$("add-btn").addEventListener("click", () => {
  result.rows.push(blankRow());
  markDirty();
  renderRows();
  const last = $("rows").lastElementChild;
  if (last) last.querySelector("textarea").focus();
});

// ---- 儲存驗證：錯誤標在欄位旁邊 -------------------------------------------

function countErrors() {
  return result.rows.reduce((n, r) => n + Object.keys(r.errors).length, 0) + result.staff.filter((p) => p.error).length;
}

function updateErrorSummary() {
  const n = countErrors();
  const el = $("form-errors");
  el.textContent = n ? `有 ${n} 處需要修正，已標示在表格中` : "";
  el.hidden = !n;
}

function clearAllErrors() {
  result.rows.forEach((r) => (r.errors = {}));
  result.staff.forEach((p) => delete p.error);
}

const applyRowErrors = (errors) => errors.forEach((e) => result.rows[e.row - 1] && (result.rows[e.row - 1].errors[e.field] = e.message));
const applyStaffErrors = (errors) => errors.forEach((e) => result.staff[e.row - 1] && (result.staff[e.row - 1].error = e.message));

// 重畫並把焦點放到第一個有錯的格子
function showErrors() {
  renderRows();
  updateErrorSummary();
  const hasStaffErr = result.staff.some((p) => p.error);
  const first = document.querySelector('#rows [aria-invalid="true"]');
  if (first) {
    first.scrollIntoView({ block: "center", behavior: "smooth" });
    first.focus({ preventScroll: true });
  } else if (hasStaffErr) {
    openStaffDialog();
  }
}

// 前端驗證：有任何錯誤就標示並回傳 false（不呼叫後端）
function validateBeforeSave() {
  clearAllErrors();
  const rowErrors = Staff.validateRows(result.rows);
  const staffErrors = Staff.validateStaff(result.staff);
  applyRowErrors(rowErrors);
  applyStaffErrors(staffErrors);
  if (!rowErrors.length && !staffErrors.length) {
    updateErrorSummary();
    return true;
  }
  showErrors();
  return false;
}

// 送出員工名單；回來的名單順序與送出的相同，用位置把暫時 id 換成系統產生的 id，並同步表格裡的指派
async function pushStaff() {
  const data = await api("PUT", `/sessions/${store.get(KEY_SESSION)}/staff`, { staff: result.staff.map(trimStaff) });
  const idMap = {};
  data.staff.forEach((p, i) => (idMap[result.staff[i].id] = p.id));
  result.rows.forEach((r) => {
    if (r.assignee && idMap[r.assignee]) r.assignee = idMap[r.assignee];
  });
  result.staff = data.staff.map((p) => ({ id: p.id, name: p.name, role: p.role }));
  updateStaffButton();
}

// 後端 422：用 errors 標在對應的格子；沒有 errors 就只顯示簡短訊息
function handleServerError(e, step) {
  if (e.status === 422 && Array.isArray(e.errors) && e.errors.length) {
    if (step === "staff") applyStaffErrors(e.errors);
    else applyRowErrors(e.errors);
    showErrors();
    renderStaffList();
  }
  showError(e.message);
}

// 儲存：成功回傳 true；失敗顯示錯誤並回傳 false。先驗證，通過才送（先員工名單，再副產品與指派）
async function save() {
  showError("");
  if (!validateBeforeSave()) return false;
  const btn = $("save-btn");
  btn.disabled = true;
  const label = btn.textContent;
  btn.textContent = "儲存中…";
  $("save-msg").textContent = "";
  let step = "staff";
  try {
    await pushStaff();
    step = "rows";
    await api("PUT", `/sessions/${store.get(KEY_SESSION)}/byproducts`, {
      vfp: $("vfp").value.trim(),
      byproducts: result.rows.map(trimRow),
    });
    result.saved = snapshot();
    renderRows();
    renderStaffList();
    $("save-msg").textContent = "已儲存";
    return true;
  } catch (e) {
    handleServerError(e, step);
    return false;
  } finally {
    btn.disabled = false;
    btn.textContent = label;
  }
}

// ---- 員工名單面板 -----------------------------------------------------------

const staffDlg = $("staff-dialog");

function updateStaffButton() {
  $("staff-btn").textContent = `員工名單（${result.staff.length} 人）`;
}

function setStaffMsg(text) {
  $("staff-msg").textContent = text || "";
}

function renderStaffList() {
  const ul = $("staff-list");
  ul.replaceChildren();
  result.staff.forEach((p, i) => {
    const li = document.createElement("li");
    li.className = "staff-row";
    const name = document.createElement("input");
    name.type = "text";
    name.value = p.name;
    name.placeholder = "姓名";
    name.setAttribute("aria-label", `第 ${i + 1} 位姓名`);
    name.addEventListener("input", () => {
      p.name = name.value;
      markDirty();
      if (p.error) {
        delete p.error;
        name.classList.remove("invalid");
        name.removeAttribute("aria-invalid");
        name.removeAttribute("aria-describedby");
        const m = document.getElementById(`staff-err-${i}`);
        if (m) m.remove();
        updateErrorSummary();
      }
    });
    const role = document.createElement("input");
    role.type = "text";
    role.className = "role";
    role.value = p.role;
    role.placeholder = "職稱（選填）";
    role.setAttribute("aria-label", `第 ${i + 1} 位職稱`);
    role.addEventListener("input", () => {
      p.role = role.value;
      markDirty();
    });
    const del = iconButton(`刪除第 ${i + 1} 位`, "✕", false, () => {
      result.staff.splice(i, 1);
      result.rows.forEach((r) => {
        if (r.assignee === p.id) r.assignee = null; // 刪掉的員工，指派自動改成未指派
      });
      markDirty();
      updateStaffButton();
      renderStaffList();
    });
    li.append(name, role, del);
    if (p.error) li.appendChild(markInvalid(name, `staff-err-${i}`, p.error));
    ul.appendChild(li);
  });
}

function openStaffDialog() {
  setStaffMsg("");
  renderStaffList();
  if (typeof staffDlg.showModal === "function") {
    if (!staffDlg.open) staffDlg.showModal();
  } else {
    staffDlg.setAttribute("open", "");
  }
  const bad = staffDlg.querySelector('[aria-invalid="true"]');
  (bad || $("staff-input")).focus();
}

staffDlg.addEventListener("close", () => {
  updateStaffButton();
  renderRows(); // 名單變了，下拉選單要跟著更新
  $("staff-btn").focus();
});

function addNames(names) {
  const r = Staff.mergeStaff(result.staff, names.map((name) => ({ name, role: "" })), newTempId);
  result.staff = r.staff;
  if (r.added) markDirty();
  setStaffMsg(names.length ? Staff.importSummary(r) : "");
  updateStaffButton();
  renderStaffList();
}

function addFromInput() {
  const box = $("staff-input");
  const names = Staff.parseNames(box.value);
  if (!names.length) return;
  addNames(names);
  box.value = "";
  box.focus();
}

$("staff-btn").addEventListener("click", openStaffDialog);
$("staff-add-btn").addEventListener("click", addFromInput);
$("staff-input").addEventListener("keydown", (ev) => {
  if (ev.key === "Enter" && !ev.isComposing && ev.keyCode !== 229) {
    ev.preventDefault();
    addFromInput();
  }
});
$("staff-input").addEventListener("paste", (ev) => {
  const text = (ev.clipboardData && ev.clipboardData.getData("text")) || "";
  const names = Staff.parseNames(text);
  if (names.length > 1) {
    ev.preventDefault();
    addNames(names);
  }
});

$("staff-import-btn").addEventListener("click", () => $("staff-file").click());
$("staff-file").addEventListener("change", async (ev) => {
  const file = ev.target.files && ev.target.files[0];
  ev.target.value = "";
  if (!file) return;
  try {
    const text = Staff.decodeBytes(new Uint8Array(await file.arrayBuffer()));
    const people = Staff.parseStaffCsv(text);
    if (!people.length) {
      setStaffMsg("檔案裡沒有讀到任何姓名");
      return;
    }
    const r = Staff.mergeStaff(result.staff, people, newTempId);
    result.staff = r.staff;
    if (r.added) markDirty();
    setStaffMsg(Staff.importSummary(r));
    updateStaffButton();
    renderStaffList();
  } catch {
    setStaffMsg("讀不到這個檔案，請確認是 .csv 或 .txt");
  }
});

$("staff-template-btn").addEventListener("click", () => {
  downloadBlob(new Blob([Staff.templateCsv()], { type: "text/csv;charset=utf-8" }), "員工名單範本.csv");
});

$("staff-close-btn").addEventListener("click", () => staffDlg.close());

// 面板的儲存：只驗證並送出名單
$("staff-save-btn").addEventListener("click", async () => {
  showError("");
  result.staff.forEach((p) => delete p.error);
  const errors = Staff.validateStaff(result.staff);
  if (errors.length) {
    applyStaffErrors(errors);
    renderStaffList();
    updateErrorSummary();
    setStaffMsg(`有 ${errors.length} 處需要修正`);
    const bad = staffDlg.querySelector('[aria-invalid="true"]');
    if (bad) bad.focus();
    return;
  }
  try {
    await pushStaff();
    const o = JSON.parse(result.saved || "{}");
    o.staff = JSON.parse(snapshot()).staff; // 名單已存，不再算「尚未儲存」
    if (result.saved) result.saved = JSON.stringify(o);
    updateErrorSummary();
    renderStaffList();
    setStaffMsg("名單已儲存");
  } catch (e) {
    if (e.status === 422 && Array.isArray(e.errors)) {
      applyStaffErrors(e.errors);
      renderStaffList();
      updateErrorSummary();
    }
    setStaffMsg(e.message);
  }
});

$("save-btn").addEventListener("click", save);

// ---- 匯出選單（鍵盤可用，點外面關閉）-----------------------------------------

const menu = $("export-menu");
const menuButtons = () => Array.from(menu.querySelectorAll("button"));

function openMenu() {
  menu.hidden = false;
  $("export-btn").setAttribute("aria-expanded", "true");
  menuButtons()[0].focus();
}

function closeMenu(returnFocus = false) {
  if (menu.hidden) return;
  menu.hidden = true;
  $("export-btn").setAttribute("aria-expanded", "false");
  if (returnFocus) $("export-btn").focus();
}

$("export-btn").addEventListener("click", () => (menu.hidden ? openMenu() : closeMenu()));
$("export-btn").addEventListener("keydown", (ev) => {
  if (ev.key === "ArrowDown") {
    ev.preventDefault();
    openMenu();
  }
});
menu.addEventListener("keydown", (ev) => {
  const items = menuButtons();
  const i = items.indexOf(document.activeElement);
  if (ev.key === "ArrowDown") {
    ev.preventDefault();
    items[(i + 1) % items.length].focus();
  } else if (ev.key === "ArrowUp") {
    ev.preventDefault();
    items[(i - 1 + items.length) % items.length].focus();
  } else if (ev.key === "Escape") {
    ev.preventDefault();
    closeMenu(true);
  } else if (ev.key === "Tab") {
    closeMenu();
  }
});
document.addEventListener("click", (ev) => {
  if (!ev.target.closest(".menu-wrap")) closeMenu();
});
menu.addEventListener("click", (ev) => {
  const item = ev.target.closest("button[data-export]");
  if (!item) return;
  closeMenu(true);
  exportAs(item.dataset.export);
});

// 匯出前有未儲存的修改，先自動儲存；失敗就中止
async function exportAs(kind) {
  if (isDirty() && !(await save())) return;
  showError("");
  try {
    if (kind === "xlsx") await exportXlsx();
    else if (typeof window.exportJpeg === "function") await window.exportJpeg(currentExportData());
    else throw apiError("圖片匯出尚未載入", 0);
  } catch (e) {
    showError(e.message || "匯出失敗，請再試一次");
  }
}

function currentExportData() {
  return {
    vfp: $("vfp").value.trim(),
    byproducts: result.rows.map((r) => {
      const p = staffById(r.assignee);
      return { ...trimRow(r), owner: p ? Staff.staffLabel(p) : "" };
    }),
    industry: result.inputs.industry || "",
    name: result.inputs.name || "",
    date: new Date(),
  };
}

function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}
window.downloadBlob = downloadBlob;
window.ymd = ymd;

async function exportXlsx() {
  const res = await request("GET", `/sessions/${store.get(KEY_SESSION)}/export.xlsx`);
  downloadBlob(await res.blob(), `管到飛_產品鑑定_${ymd()}.xlsx`);
}

// ---- 對話頁：重置 ----------------------------------------------------------

function clearFlowStorage() {
  store.del(KEY_SESSION);
  store.del(KEY_INTAKE);
  intake = Intake.create();
  mode = "intake";
}

$("reset-btn").addEventListener("click", () => {
  const hasProgress = Boolean(store.get(KEY_SESSION)) || Intake.hasProgress(intake) || Boolean($("answer").value.trim());
  if (hasProgress && !confirm("目前的對話會被清除，確定要重新開始嗎？")) return;
  clearFlowStorage();
  go("#/");
});

// ---- 結果頁底部按鈕 --------------------------------------------------------

function confirmLeave() {
  return !isDirty() || confirm(LEAVE_MESSAGE);
}

$("back-btn").addEventListener("click", () => {
  if (confirmLeave()) {
    result.saved = snapshot(); // 已經確認要放棄修改，後續換頁不再詢問
    go("#/chat");
  }
});

$("home-btn").addEventListener("click", () => {
  if (!confirmLeave()) return;
  result.saved = snapshot();
  clearFlowStorage();
  go("#/");
});

route();
