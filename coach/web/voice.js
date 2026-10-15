"use strict";

// 語音輸入的純函式（不碰 DOM）：辨識結果合併、錯誤訊息、提示文字、支援判斷。
// 瀏覽器用 window.Voice，node 測試用 require。

(function (root) {
  const ASCII_WORD = /[A-Za-z0-9]/;

  // 原有文字（按下麥克風當下輸入框裡的字）＋已確定片段＋暫時片段。
  // 暫時片段每次都是「目前整段」，所以呼叫端每次都傳最新的，不會累加。
  function mergeTranscript(base, finalText, interimText) {
    const b = typeof base === "string" ? base : "";
    const spoken = `${finalText || ""}${interimText || ""}`.trim();
    if (!spoken) return b;
    // 兩邊都是英數時補一個空白，中文不加
    const needSpace = b && !/\s$/.test(b) && ASCII_WORD.test(b.slice(-1)) && ASCII_WORD.test(spoken[0]);
    return `${b}${needSpace ? " " : ""}${spoken}`;
  }

  // 從 SpeechRecognition 的 results（類陣列，每項有 isFinal 與 [0].transcript）切出已確定與暫時兩段
  function splitResults(results) {
    let finalText = "";
    let interimText = "";
    for (let i = 0; i < (results ? results.length : 0); i++) {
      const r = results[i];
      const t = r && r[0] ? r[0].transcript : "";
      if (r && r.isFinal) finalText += t;
      else interimText += t;
    }
    return { finalText, interimText };
  }

  function errorMessage(code) {
    switch (code) {
      case "not-allowed":
      case "service-not-allowed":
        return "請允許使用麥克風（網址列左側的圖示）";
      case "no-speech":
        return "沒聽到聲音，再試一次";
      case "audio-capture":
        return "找不到麥克風";
      case "network":
        return "語音辨識需要網路，也可以改用鍵盤上的麥克風鍵";
      default:
        return "語音輸入暫時無法使用，可改用鍵盤上的麥克風鍵";
    }
  }

  // 輸入框下方的小字提示
  function hintText(buttonShown, coarsePointer) {
    if (buttonShown) return "按麥克風說話，也可以用鍵盤上的語音輸入";
    return coarsePointer ? "可用鍵盤上的麥克風鍵語音輸入" : "Windows 可按 Win+H 語音輸入";
  }

  // 安全環境（https 或 localhost）且瀏覽器有語音辨識才支援；回傳建構子或 null
  function recognitionCtor(win) {
    if (!win || !win.isSecureContext) return null;
    return win.SpeechRecognition || win.webkitSpeechRecognition || null;
  }

  const Voice = { mergeTranscript, splitResults, errorMessage, hintText, recognitionCtor };
  if (typeof module !== "undefined" && module.exports) module.exports = Voice;
  else root.Voice = Voice;
})(typeof window !== "undefined" ? window : globalThis);
