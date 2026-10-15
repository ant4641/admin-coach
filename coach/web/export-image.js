"use strict";

// 匯出圖片（.jpg）：用 canvas 自己畫，不用任何外部函式庫。
// 配色與系統字型同 style.css；中文換行用 measureText 逐字測量。

(function () {
  const WIDTH = 1200; // 畫布寬度（CSS 像素）
  const SCALE = 2; // 輸出解析度倍數
  const QUALITY = 0.92;
  const FONT = '"Microsoft JhengHei", "PingFang TC", "Noto Sans TC", system-ui, sans-serif';

  const C = {
    navy900: "#0C3467",
    navy800: "#0F325E",
    slate700: "#2D4A6E",
    gray600: "#5B6B80",
    sky300: "#9CC3EA",
    sky100: "#DCEBFA",
    mist100: "#E8EEF5",
    yellow400: "#F4C84D",
    white: "#FFFFFF",
  };

  // VFP 列的金色（與網頁、Excel 一致）
  const GOLD = { labelFrom: "#F4C84D", labelTo: "#E0A92B", contentFrom: "#FFF3C9", contentTo: "#FBE08A", line: "#D9A21B" };

  const PAD = 48; // 左右留白
  const COLS = { seq: 80, name: 340, owner: 190 }; // 序號欄、工作項目欄寬；副產品欄吃剩下的；負責人欄在最右
  const DETAIL_PX = 16;
  const DETAIL_LINE_H = 24;
  const CELL_PAD_X = 16;
  const CELL_PAD_Y = 12;
  const LINE_H = 30;
  const BODY_PX = 20;

  const BLANK = "（未填）";
  const UNASSIGNED = "（未指派）";

  // 欄位值整理：只收字串（去頭尾空白）；undefined、null、數字以外的東西一律當空字串，不會出現 "undefined"
  function clean(v) {
    if (typeof v === "number") return String(v);
    return typeof v === "string" ? v.trim() : "";
  }

  // 單列整理成 {task, detail, product}；舊格式（name、description）照後端規則轉換：name→task、description→detail、product 空白
  function normalizeItem(it) {
    const o = it && typeof it === "object" ? it : {};
    if ("task" in o || "detail" in o || "product" in o) {
      return { task: clean(o.task), detail: clean(o.detail), product: clean(o.product), owner: clean(o.owner) };
    }
    return { task: clean(o.name), detail: clean(o.description), product: "", owner: clean(o.owner) };
  }

  // 匯出圖片用的資料：task、product 空白顯示「（未填）」，detail 空白就不畫小字
  function prepare(data) {
    const d = data || {};
    return {
      ...d,
      vfp: clean(d.vfp) || BLANK,
      byproducts: (Array.isArray(d.byproducts) ? d.byproducts : []).map((it) => {
        const r = normalizeItem(it);
        return { task: r.task || BLANK, detail: r.detail, product: r.product || BLANK, owner: r.owner || UNASSIGNED };
      }),
    };
  }

  // 產業只顯示括號前的名稱；「其他：xxx」照原樣
  function industryShort(industry) {
    const s = String(industry || "").trim();
    if (s.startsWith("其他")) return s;
    return s.split("（")[0].trim();
  }

  // 「產業：…　稱呼：…　日期：YYYY-MM-DD」；沒有的欄位整個省略，不會出現 undefined
  function infoLine(data) {
    const pad2 = (n) => String(n).padStart(2, "0");
    const d = data.date || new Date();
    const parts = [];
    const industry = industryShort(data.industry);
    const name = String(data.name || "").trim();
    if (industry) parts.push(`產業：${industry}`);
    if (name) parts.push(`稱呼：${name}`);
    parts.push(`日期：${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`);
    return parts.join("　");
  }

  // 逐字測量換行；\n 強制換行。回傳每一行的字串
  function wrapText(ctx, text, maxWidth) {
    const lines = [];
    for (const para of String(text).split("\n")) {
      let line = "";
      for (const ch of Array.from(para)) {
        if (line && ctx.measureText(line + ch).width > maxWidth) {
          lines.push(line);
          line = ch;
        } else {
          line += ch;
        }
      }
      lines.push(line);
    }
    return lines;
  }

  function font(ctx, weight, px) {
    ctx.font = `${weight} ${px}px ${FONT}`;
  }

  // 依內容算出每一列的行數與列高，並回傳整張圖的高度
  function layout(ctx, data) {
    const tableW = WIDTH - PAD * 2;
    const descW = tableW - COLS.seq - COLS.name - COLS.owner;

    font(ctx, "700", BODY_PX);
    const vfpLabelW = COLS.seq + COLS.name;
    const vfpTextW = tableW - vfpLabelW - CELL_PAD_X * 2;
    const vfpLines = wrapText(ctx, data.vfp, vfpTextW);
    const vfpH = Math.max(vfpLines.length * LINE_H, LINE_H) + CELL_PAD_Y * 2;

    const rows = data.byproducts.map((b, i) => {
      font(ctx, "700", BODY_PX);
      const taskLines = wrapText(ctx, b.task, COLS.name - CELL_PAD_X * 2);
      font(ctx, "400", DETAIL_PX);
      const detailLines = b.detail ? wrapText(ctx, b.detail, COLS.name - CELL_PAD_X * 2) : [];
      font(ctx, "400", BODY_PX);
      const productLines = wrapText(ctx, b.product, descW - CELL_PAD_X * 2);
      const ownerLines = wrapText(ctx, b.owner, COLS.owner - CELL_PAD_X * 2);
      const taskH = taskLines.length * LINE_H + detailLines.length * DETAIL_LINE_H;
      const h = Math.max(taskH, productLines.length * LINE_H, ownerLines.length * LINE_H, LINE_H) + CELL_PAD_Y * 2;
      return { seq: String(i + 1), taskLines, detailLines, taskH, productLines, ownerLines, h };
    });

    const titleBlock = 130; // 標題 + 日期 + 與表格的間距
    const headH = 52;
    const footerBlock = 40; // 表格下緣之後的留白（不再有頁尾字樣）
    const height = titleBlock + vfpH + headH + rows.reduce((s, r) => s + r.h, 0) + footerBlock;
    return { tableW, descW, vfpLines, vfpH, vfpLabelW, rows, titleBlock, headH, height };
  }

  function fillRect(ctx, x, y, w, h, color) {
    ctx.fillStyle = color;
    ctx.fillRect(x, y, w, h);
  }

  function drawLines(ctx, lines, x, y, align) {
    ctx.textAlign = align;
    ctx.textBaseline = "middle";
    lines.forEach((ln, i) => ctx.fillText(ln, x, y + LINE_H * i + LINE_H / 2));
  }

  function draw(ctx, data, L) {
    fillRect(ctx, 0, 0, WIDTH, L.height, C.white);

    // 標題與日期
    ctx.fillStyle = C.navy900;
    font(ctx, "700", 34);
    ctx.textAlign = "left";
    ctx.textBaseline = "alphabetic";
    ctx.fillText("管到飛｜最終有價值產品與副產品", PAD, 72);
    fillRect(ctx, PAD, 86, 56, 5, C.yellow400);
    ctx.fillStyle = C.gray600;
    font(ctx, "400", 18);
    ctx.fillText(infoLine(data), PAD, 118);

    let y = L.titleBlock;
    const x0 = PAD;

    // VFP 列
    const gradient = (x, w, from, to) => {
      const g = ctx.createLinearGradient(x, y, x + w, y + L.vfpH);
      g.addColorStop(0, from);
      g.addColorStop(1, to);
      return g;
    };
    ctx.fillStyle = gradient(x0, L.vfpLabelW, GOLD.labelFrom, GOLD.labelTo);
    ctx.fillRect(x0, y, L.vfpLabelW, L.vfpH);
    const cx = x0 + L.vfpLabelW;
    ctx.fillStyle = gradient(cx, L.tableW - L.vfpLabelW, GOLD.contentFrom, GOLD.contentTo);
    ctx.fillRect(cx, y, L.tableW - L.vfpLabelW, L.vfpH);
    fillRect(ctx, x0, y + L.vfpH - 2, L.tableW, 2, GOLD.line);
    ctx.fillStyle = C.navy900;
    font(ctx, "700", BODY_PX);
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText("VFP", x0 + L.vfpLabelW / 2, y + L.vfpH / 2);
    ctx.fillStyle = C.navy900;
    font(ctx, "700", BODY_PX);
    const vfpBlockH = L.vfpLines.length * LINE_H;
    drawLines(ctx, L.vfpLines, x0 + L.vfpLabelW + CELL_PAD_X, y + (L.vfpH - vfpBlockH) / 2, "left");
    y += L.vfpH;

    // 表頭（下緣黃色粗線）
    fillRect(ctx, x0, y, L.tableW, L.headH, C.navy800);
    ctx.fillStyle = C.white;
    font(ctx, "700", BODY_PX);
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(window.exportJpeg.HEADERS[0], x0 + COLS.seq / 2, y + L.headH / 2);
    ctx.fillText(window.exportJpeg.HEADERS[1], x0 + COLS.seq + COLS.name / 2, y + L.headH / 2);
    ctx.fillText(window.exportJpeg.HEADERS[2], x0 + COLS.seq + COLS.name + L.descW / 2, y + L.headH / 2);
    ctx.fillText(window.exportJpeg.HEADERS[3], x0 + COLS.seq + COLS.name + L.descW + COLS.owner / 2, y + L.headH / 2);
    fillRect(ctx, x0, y + L.headH - 4, L.tableW, 4, C.yellow400);
    y += L.headH;

    // 內容列：序號、副產品置中，說明靠左；列交錯底色
    L.rows.forEach((r, i) => {
      fillRect(ctx, x0, y, L.tableW, r.h, i % 2 === 0 ? C.mist100 : C.white);
      const blockH = (n) => n * LINE_H;
      ctx.fillStyle = C.slate700;
      font(ctx, "400", BODY_PX);
      drawLines(ctx, [r.seq], x0 + COLS.seq / 2, y + (r.h - LINE_H) / 2, "center");
      // 工作項目：task 粗體，下方 detail 小字灰色；靠左
      const tx = x0 + COLS.seq + CELL_PAD_X;
      let ty = y + (r.h - r.taskH) / 2;
      ctx.fillStyle = C.navy900;
      font(ctx, "700", BODY_PX);
      drawLines(ctx, r.taskLines, tx, ty, "left");
      ty += r.taskLines.length * LINE_H;
      ctx.fillStyle = C.gray600;
      font(ctx, "400", DETAIL_PX);
      r.detailLines.forEach((ln, k) => ctx.fillText(ln, tx, ty + DETAIL_LINE_H * k + DETAIL_LINE_H / 2));
      ctx.fillStyle = C.slate700;
      font(ctx, "400", BODY_PX);
      drawLines(ctx, r.productLines, x0 + COLS.seq + COLS.name + CELL_PAD_X, y + (r.h - r.productLines.length * LINE_H) / 2, "left");
      ctx.fillStyle = C.navy900;
      font(ctx, "400", BODY_PX);
      drawLines(ctx, r.ownerLines, x0 + COLS.seq + COLS.name + L.descW + COLS.owner / 2, y + (r.h - r.ownerLines.length * LINE_H) / 2, "center");
      y += r.h;
    });

    // 表格下緣細線
    fillRect(ctx, x0, y, L.tableW, 2, C.sky300);
  }

  function toBlob(canvas) {
    return new Promise((resolve, reject) => {
      canvas.toBlob((b) => (b ? resolve(b) : reject(new Error("無法產生圖片，請再試一次"))), "image/jpeg", QUALITY);
    });
  }

  // data: { vfp, byproducts:[{task, detail, product, owner}], industry, name, date: Date }
  window.exportJpeg = async function exportJpeg(rawData) {
    const data = prepare(rawData);
    if (document.fonts && document.fonts.ready) await document.fonts.ready;
    const measure = document.createElement("canvas").getContext("2d");
    const L = layout(measure, data);
    const canvas = document.createElement("canvas");
    canvas.width = WIDTH * SCALE;
    canvas.height = Math.ceil(L.height * SCALE);
    const ctx = canvas.getContext("2d");
    if (!ctx) throw new Error("這個瀏覽器無法產生圖片");
    ctx.scale(SCALE, SCALE);
    draw(ctx, data, { ...L, height: Math.ceil(L.height) });
    const blob = await toBlob(canvas);
    window.downloadBlob(blob, `管到飛_產品鑑定_${window.ymd(data.date)}.jpg`);
  };
  window.exportJpeg.infoLine = infoLine;
  window.exportJpeg.layout = layout; // 給測試用
  window.exportJpeg.prepare = prepare;
  window.exportJpeg.normalizeItem = normalizeItem;
  window.exportJpeg.HEADERS = ["序號", "工作項目", "副產品", "負責人"]; // 給手動檢查用
})();
