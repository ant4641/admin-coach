"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const Voice = require("../../coach/web/voice.js");

test("mergeTranscript：原有文字保留，辨識結果接在後面", () => {
  assert.equal(Voice.mergeTranscript("我們賣", "好吃的麵包", ""), "我們賣好吃的麵包");
  assert.equal(Voice.mergeTranscript("", "好吃", "的麵包"), "好吃的麵包");
});

test("mergeTranscript：暫時結果被覆蓋、不累加（每次都由原有文字重算）", () => {
  const base = "我們賣";
  const a = Voice.mergeTranscript(base, "", "好");
  const b = Voice.mergeTranscript(base, "", "好吃");
  const c = Voice.mergeTranscript(base, "好吃的麵包", "");
  assert.deepEqual([a, b, c], ["我們賣好", "我們賣好吃", "我們賣好吃的麵包"]);
});

test("mergeTranscript：空結果與前後空白", () => {
  assert.equal(Voice.mergeTranscript("原文", "", ""), "原文");
  assert.equal(Voice.mergeTranscript("原文", "  ", " "), "原文");
  assert.equal(Voice.mergeTranscript("", "", ""), "");
  assert.equal(Voice.mergeTranscript("原文 ", "  好吃  ", ""), "原文 好吃");
  assert.equal(Voice.mergeTranscript(undefined, undefined, undefined), "");
  assert.equal(Voice.mergeTranscript("abc", "def", ""), "abc def"); // 英數相接補空白
});

test("splitResults：分成已確定與暫時", () => {
  const r = (t, isFinal) => Object.assign([{ transcript: t }], { isFinal });
  assert.deepEqual(Voice.splitResults([r("好吃", true), r("的麵包", false)]), { finalText: "好吃", interimText: "的麵包" });
  assert.deepEqual(Voice.splitResults(null), { finalText: "", interimText: "" });
});

test("errorMessage／hintText／recognitionCtor", () => {
  assert.equal(Voice.errorMessage("not-allowed"), "請允許使用麥克風（網址列左側的圖示）");
  assert.equal(Voice.errorMessage("service-not-allowed"), Voice.errorMessage("not-allowed"));
  assert.equal(Voice.errorMessage("no-speech"), "沒聽到聲音，再試一次");
  assert.equal(Voice.errorMessage("audio-capture"), "找不到麥克風");
  assert.equal(Voice.errorMessage("network"), "語音辨識需要網路，也可以改用鍵盤上的麥克風鍵");
  assert.equal(Voice.errorMessage("whatever"), "語音輸入暫時無法使用，可改用鍵盤上的麥克風鍵");
  assert.equal(Voice.hintText(true, false), "按麥克風說話，也可以用鍵盤上的語音輸入");
  assert.equal(Voice.hintText(false, true), "可用鍵盤上的麥克風鍵語音輸入");
  assert.equal(Voice.hintText(false, false), "Windows 可按 Win+H 語音輸入");
  const C = function () {};
  assert.equal(Voice.recognitionCtor({ isSecureContext: true, webkitSpeechRecognition: C }), C);
  assert.equal(Voice.recognitionCtor({ isSecureContext: false, SpeechRecognition: C }), null);
  assert.equal(Voice.recognitionCtor({ isSecureContext: true }), null);
});
