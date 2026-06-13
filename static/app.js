function $(id) { return document.getElementById(id); }

function fmtInt(v) {
  if (v === null || v === undefined || v === "") return "";
  const n = Number(v);
  if (!Number.isFinite(n)) return String(v);
  return n.toLocaleString("ko-KR");
}

function fmtSigned(v) {
  if (v === null || v === undefined || v === "") return "";
  const n = Number(v);
  if (!Number.isFinite(n)) return String(v);
  const s = n.toLocaleString("ko-KR");
  return n > 0 ? `+${s}` : s;
}

function toDateKey(yyyy_mm_dd) {
  // "YYYY-MM-DD" 형태는 문자열 정렬이 시간 정렬과 동일
  return String(yyyy_mm_dd || "").slice(0, 10);
}

function setStatus(kind, msg) {
  const el = $("status");
  el.className = `status ${kind || ""}`.trim();
  el.textContent = msg || "";
}

function buildKv(k, v) {
  const wrap = document.createElement("div");
  wrap.className = "kv";
  const kk = document.createElement("div");
  kk.className = "k";
  kk.textContent = k;
  const vv = document.createElement("div");
  vv.className = "v mono";
  vv.textContent = v;
  wrap.appendChild(kk);
  wrap.appendChild(vv);
  return wrap;
}

function clearTbody(tblId) {
  const tbody = $(tblId).querySelector("tbody");
  tbody.innerHTML = "";
  return tbody;
}

function addRow(tbody, cells, classes = []) {
  const tr = document.createElement("tr");
  cells.forEach((c, i) => {
    const td = document.createElement("td");
    td.textContent = c;
    if (classes[i]) td.className = classes[i];
    tr.appendChild(td);
  });
  tbody.appendChild(tr);
}

function addEmptyRow(tbody, colSpan, message) {
  const tr = document.createElement("tr");
  const td = document.createElement("td");
  td.colSpan = colSpan;
  td.className = "mono";
  td.style.color = "rgba(159,176,208,0.95)";
  td.style.padding = "14px 10px";
  td.textContent = message;
  tr.appendChild(td);
  tbody.appendChild(tr);
}

function renderPatternsTable(analysis) {
  const tbl = $("tblPatterns");
  if (!tbl) return;
  const tbody = tbl.querySelector("tbody");
  if (!tbody) return;
  tbody.innerHTML = "";

  const a = analysis || {};
  const foreign = Array.isArray(a.foreign_down2_up2_dates) ? a.foreign_down2_up2_dates : [];
  const inst = Array.isArray(a.institution_down2_up2_dates) ? a.institution_down2_up2_dates : [];
  const matched = new Set(Array.isArray(a.matched_down2_up2_dates) ? a.matched_down2_up2_dates : []);

  const n = Math.max(foreign.length, inst.length, matched.size);
  const matchedArr = Array.from(matched).sort();

  const rows = Math.max(n, 1);
  for (let i = 0; i < rows; i++) {
    const f = foreign[i] || "";
    const it = inst[i] || "";
    const m = matchedArr[i] || "";
    addRow(
      tbody,
      [f, it, m],
      ["mono", "mono", "mono"]
    );
  }

  if (foreign.length === 0 && inst.length === 0 && matched.size === 0) {
    addEmptyRow(tbody, 3, "감지된 패턴이 없습니다. (기간을 늘려보세요)");
  }
}

function fmtPct(v) {
  const n = Number(v);
  if (!Number.isFinite(n)) return "";
  const s = n.toFixed(2);
  return (n > 0 ? `+${s}` : s);
}

function renderSimulation(simulation) {
  const tbl = $("tblSim");
  const noteEl = $("simNote");
  if (!tbl) return;
  const tbody = tbl.querySelector("tbody");
  if (!tbody) return;
  tbody.innerHTML = "";
  if (noteEl) noteEl.textContent = "";

  const sim = simulation || {};
  const rows = Array.isArray(sim.rows) ? sim.rows : [];

  if (rows.length === 0) {
    addEmptyRow(tbody, 9, sim.note || "일치 신호가 없어 시뮬레이션 결과가 없습니다.");
    if (noteEl && sim.assumption) noteEl.textContent = `가정: ${sim.assumption}`;
    return;
  }

  rows.forEach(r => {
    addRow(
      tbody,
      [
        r.signal_dt || "",
        r.buy_dt || "",
        fmtInt(r.buy_open),
        fmtPct(r.max_profit_pct),
        r.max_profit_dt || "",
        fmtPct(r.max_loss_pct),
        r.max_loss_dt || "",
        fmtPct(r.latest_pct),
        r.latest_dt || "",
      ],
      ["mono", "mono", "right mono", "right mono", "mono", "right mono", "mono", "right mono", "mono"]
    );
  });

  if (noteEl && sim.assumption) noteEl.textContent = `가정: ${sim.assumption}`;
}

function qtyClass(q) {
  const n = Number(q);
  if (!Number.isFinite(n) || n === 0) return "right mono";
  return `right mono ${n > 0 ? "pos" : "neg"}`;
}

let lastData = null;

function setupCanvas(canvas) {
  const dpr = Math.max(1, Math.floor(window.devicePixelRatio || 1));
  const rect = canvas.getBoundingClientRect();
  const w = Math.max(10, Math.floor(rect.width));
  const h = Math.max(10, Math.floor(rect.height));
  canvas.width = w * dpr;
  canvas.height = h * dpr;
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, w, h };
}

function drawFrame(ctx, w, h) {
  ctx.clearRect(0, 0, w, h);

  // grid
  ctx.strokeStyle = "rgba(255,255,255,0.06)";
  ctx.lineWidth = 1;
  for (let i = 1; i <= 4; i++) {
    const y = Math.round((h * i) / 5);
    ctx.beginPath();
    ctx.moveTo(0, y);
    ctx.lineTo(w, y);
    ctx.stroke();
  }
}

function drawText(ctx, text, x, y, align = "left", color = "rgba(231,238,252,0.75)") {
  ctx.fillStyle = color;
  ctx.font = "12px ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, 'Noto Sans KR', Arial";
  ctx.textAlign = align;
  ctx.textBaseline = "middle";
  ctx.fillText(text, x, y);
}

function drawLineChart(canvas, points, opts = {}) {
  const { ctx, w, h } = setupCanvas(canvas);
  drawFrame(ctx, w, h);

  if (!points || points.length < 2) {
    drawText(ctx, "표시할 데이터가 없습니다.", w / 2, h / 2, "center", "rgba(159,176,208,0.95)");
    return;
  }

  const padL = 44, padR = 12, padT = 12, padB = 26;
  const innerW = w - padL - padR;
  const innerH = h - padT - padB;

  const ys = points.map(p => p.y).filter(Number.isFinite);
  let ymin = Math.min(...ys);
  let ymax = Math.max(...ys);
  if (ymin === ymax) { ymin -= 1; ymax += 1; }
  const yPad = (ymax - ymin) * 0.08;
  ymin -= yPad; ymax += yPad;

  const xAt = (i) => padL + (innerW * i) / (points.length - 1);
  const yAt = (y) => padT + innerH * (1 - (y - ymin) / (ymax - ymin));

  // y labels
  drawText(ctx, fmtInt(Math.round(ymax)), padL - 8, padT + 2, "right");
  drawText(ctx, fmtInt(Math.round((ymax + ymin) / 2)), padL - 8, padT + innerH / 2, "right");
  drawText(ctx, fmtInt(Math.round(ymin)), padL - 8, padT + innerH - 2, "right");

  // x labels (start/end)
  const first = points[0].xLabel || "";
  const last = points[points.length - 1].xLabel || "";
  drawText(ctx, first, padL, h - 12, "left", "rgba(159,176,208,0.95)");
  drawText(ctx, last, w - padR, h - 12, "right", "rgba(159,176,208,0.95)");

  // line
  ctx.strokeStyle = opts.color || "rgba(106,169,255,0.95)";
  ctx.lineWidth = 2;
  ctx.beginPath();
  points.forEach((p, i) => {
    const x = xAt(i);
    const y = yAt(p.y);
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();
}

function drawMultiLineChart(canvas, seriesList, opts = {}) {
  const { ctx, w, h } = setupCanvas(canvas);
  drawFrame(ctx, w, h);

  const validSeries = (seriesList || []).filter(s => Array.isArray(s.points) && s.points.length >= 2);
  if (validSeries.length === 0) {
    drawText(ctx, "표시할 데이터가 없습니다. (체크박스를 선택하세요)", w / 2, h / 2, "center", "rgba(159,176,208,0.95)");
    return;
  }

  // 동일한 x축(날짜)을 가정. 첫 시리즈의 xLabel을 사용
  const basePoints = validSeries[0].points;
  const n = basePoints.length;

  const padL = 44, padR = 12, padT = 12, padB = 26;
  const innerW = w - padL - padR;
  const innerH = h - padT - padB;

  let ys = [];
  validSeries.forEach(s => {
    ys = ys.concat(s.points.map(p => p.y).filter(Number.isFinite));
  });
  let ymin = Math.min(...ys);
  let ymax = Math.max(...ys);
  if (ymin === ymax) { ymin -= 1; ymax += 1; }
  const yPad = (ymax - ymin) * 0.08;
  ymin -= yPad; ymax += yPad;

  const xAt = (i) => padL + (innerW * i) / (n - 1);
  const yAt = (y) => padT + innerH * (1 - (y - ymin) / (ymax - ymin));

  // y labels
  drawText(ctx, fmtInt(Math.round(ymax)), padL - 8, padT + 2, "right");
  drawText(ctx, fmtInt(Math.round((ymax + ymin) / 2)), padL - 8, padT + innerH / 2, "right");
  drawText(ctx, fmtInt(Math.round(ymin)), padL - 8, padT + innerH - 2, "right");

  // x labels (start/end)
  const first = basePoints[0].xLabel || "";
  const last = basePoints[n - 1].xLabel || "";
  drawText(ctx, first, padL, h - 12, "left", "rgba(159,176,208,0.95)");
  drawText(ctx, last, w - padR, h - 12, "right", "rgba(159,176,208,0.95)");

  // lines
  validSeries.forEach(s => {
    ctx.strokeStyle = s.color || opts.color || "rgba(106,169,255,0.95)";
    ctx.lineWidth = s.width || 2;
    ctx.beginPath();
    s.points.forEach((p, i) => {
      const x = xAt(i);
      const y = yAt(p.y);
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
  });
}

function drawNetBars(canvas, dates, foreignMap, instMap, showF, showI) {
  const { ctx, w, h } = setupCanvas(canvas);
  drawFrame(ctx, w, h);

  if (!dates || dates.length === 0) {
    drawText(ctx, "표시할 데이터가 없습니다.", w / 2, h / 2, "center", "rgba(159,176,208,0.95)");
    return;
  }

  const padL = 44, padR = 12, padT = 12, padB = 26;
  const innerW = w - padL - padR;
  const innerH = h - padT - padB;

  // range (include 0)
  let vals = [0];
  if (showF) vals = vals.concat(dates.map(d => Number(foreignMap.get(d) || 0)));
  if (showI) vals = vals.concat(dates.map(d => Number(instMap.get(d) || 0)));
  vals = vals.filter(Number.isFinite);
  let vmin = Math.min(...vals);
  let vmax = Math.max(...vals);
  if (vmin === vmax) { vmin -= 1; vmax += 1; }
  const absMax = Math.max(Math.abs(vmin), Math.abs(vmax));
  vmin = -absMax; vmax = absMax; // 대칭으로 만들어 0 기준선이 중앙에 오게

  const yAt = (v) => padT + innerH * (1 - (v - vmin) / (vmax - vmin));
  const zeroY = yAt(0);

  // y labels
  drawText(ctx, fmtInt(Math.round(vmax)), padL - 8, padT + 2, "right");
  drawText(ctx, "0", padL - 8, zeroY, "right", "rgba(231,238,252,0.9)");
  drawText(ctx, fmtInt(Math.round(vmin)), padL - 8, padT + innerH - 2, "right");

  // x labels start/end
  drawText(ctx, dates[0], padL, h - 12, "left", "rgba(159,176,208,0.95)");
  drawText(ctx, dates[dates.length - 1], w - padR, h - 12, "right", "rgba(159,176,208,0.95)");

  // baseline
  ctx.strokeStyle = "rgba(255,255,255,0.18)";
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(padL, Math.round(zeroY) + 0.5);
  ctx.lineTo(w - padR, Math.round(zeroY) + 0.5);
  ctx.stroke();

  const n = dates.length;
  const step = innerW / n;
  const barW = Math.max(2, Math.min(18, step * 0.38));
  const off = barW * 0.65; // 두 시리즈 살짝 좌/우로 분리

  function drawOne(dateIdx, value, xCenter, upColor, dnColor, alpha = 0.7) {
    if (!Number.isFinite(value) || value === 0) return;
    const x = xCenter - barW / 2;
    const y0 = zeroY;
    const y1 = yAt(value);
    const top = Math.min(y0, y1);
    const height = Math.max(1, Math.abs(y1 - y0));
    ctx.fillStyle = (value >= 0 ? upColor : dnColor).replace("ALPHA", String(alpha));
    ctx.fillRect(x, top, barW, height);
  }

  for (let i = 0; i < n; i++) {
    const d = dates[i];
    const xCenter = padL + step * i + step / 2;

    if (showF) {
      const v = Number(foreignMap.get(d) || 0);
      drawOne(i, v, xCenter - off, "rgba(53,208,127,ALPHA)", "rgba(255,92,119,ALPHA)", 0.70);
    }
    if (showI) {
      const v = Number(instMap.get(d) || 0);
      drawOne(i, v, xCenter + off, "rgba(106,169,255,ALPHA)", "rgba(255,204,102,ALPHA)", 0.65);
    }
  }
}

function buildDatesAndMaps(foreignDaily, instDaily) {
  const dates = Array.from(new Set(
    foreignDaily.map(x => toDateKey(x.dt)).concat(instDaily.map(x => toDateKey(x.dt)))
  )).filter(Boolean).sort();

  const fMap = new Map(foreignDaily.map(x => [toDateKey(x.dt), Number(x.net_trade_qty) || 0]));
  const iMap = new Map(instDaily.map(x => [toDateKey(x.dt), Number(x.net_trade_qty) || 0]));
  return { dates, fMap, iMap };
}

function drawCumulative(canvas, dates, fMap, iMap, showF, showI) {
  // 누적합은 날짜 오름차순으로 계산
  let fCum = 0;
  let iCum = 0;
  const fPoints = [];
  const iPoints = [];

  dates.forEach(d => {
    const fv = Number(fMap.get(d) || 0);
    const iv = Number(iMap.get(d) || 0);
    fCum += Number.isFinite(fv) ? fv : 0;
    iCum += Number.isFinite(iv) ? iv : 0;
    if (showF) fPoints.push({ xLabel: d, y: fCum });
    if (showI) iPoints.push({ xLabel: d, y: iCum });
  });

  const series = [];
  if (showF) series.push({ points: fPoints, color: "rgba(53,208,127,0.95)", width: 2 });
  if (showI) series.push({ points: iPoints, color: "rgba(106,169,255,0.95)", width: 2 });

  drawMultiLineChart(canvas, series);
}

function applyVisibility() {
  if (!lastData) return;
  // 토글 시 차트만 다시 그림
  const showF = $("chkForeign").checked;
  const showI = $("chkInstitution").checked;

  // 테이블도 함께 표시/숨김
  const cf = $("cardForeign");
  const ci = $("cardInstitution");
  if (cf) cf.style.display = showF ? "" : "none";
  if (ci) ci.style.display = showI ? "" : "none";

  const foreignDaily = Array.isArray(lastData.foreign?.daily) ? lastData.foreign.daily : [];
  const instDaily = Array.isArray(lastData.institution?.daily) ? lastData.institution.daily : [];

  const { dates, fMap, iMap } = buildDatesAndMaps(foreignDaily, instDaily);

  drawNetBars($("netCanvas"), dates, fMap, iMap, showF, showI);
  drawCumulative($("cumCanvas"), dates, fMap, iMap, showF, showI);
}

function renderAll(data) {
  lastData = data;
  // Summary
  const sum = $("summary");
  sum.innerHTML = "";

  sum.appendChild(buildKv("종목", data.ticker || ""));
  sum.appendChild(buildKv("기간", `${data.from || ""} ~ ${data.to || ""}`));
  sum.appendChild(buildKv("일수(고유)", fmtInt(data.unique_days)));

  const f = data.foreign?.summary || {};
  const i = data.institution?.summary || {};
  sum.appendChild(buildKv("외국인 합계", fmtInt(f.total_net_trade_qty)));
  sum.appendChild(buildKv("기관 합계", fmtInt(i.total_net_trade_qty)));
  sum.appendChild(buildKv("외국인 매수/매도 우위일", `${fmtInt(f.buy_dominant_days)} / ${fmtInt(f.sell_dominant_days)}`));
  sum.appendChild(buildKv("기관 매수/매도 우위일", `${fmtInt(i.buy_dominant_days)} / ${fmtInt(i.sell_dominant_days)}`));

  // Note
  const note = data.meta?.note ? `안내: ${data.meta.note}` : "";
  $("note").textContent = note;

  // Tables
  const fTbody = clearTbody("tblForeign");
  const iTbody = clearTbody("tblInstitution");

  const foreignDaily = Array.isArray(data.foreign?.daily) ? data.foreign.daily : [];
  const instDaily = Array.isArray(data.institution?.daily) ? data.institution.daily : [];

  // 데이터 유무를 화면에서 바로 확인 가능하게(디버깅/가독성)
  sum.appendChild(buildKv("외국인 건수", fmtInt(foreignDaily.length)));
  sum.appendChild(buildKv("기관 건수", fmtInt(instDaily.length)));

  foreignDaily.forEach(x => {
    addRow(
      fTbody,
      [
        x.dt || "",
        fmtSigned(x.net_trade_qty),
        fmtInt(x.close_pric),
        fmtSigned(x.pre),
        x.frgnr_qota_rt ?? "",
      ],
      ["mono", qtyClass(x.net_trade_qty), "right mono", qtyClass(x.pre), "right mono"]
    );
  });
  if (foreignDaily.length === 0) {
    addEmptyRow(fTbody, 5, "외국인 데이터가 없습니다. (응답 필드/기간/종목을 확인하세요)");
  }

  instDaily.forEach(x => {
    addRow(
      iTbody,
      [x.dt || "", fmtSigned(x.net_trade_qty), fmtInt(x.close_pric), fmtSigned(x.pre)],
      ["mono", qtyClass(x.net_trade_qty), "right mono", qtyClass(x.pre)]
    );
  });
  if (instDaily.length === 0) {
    addEmptyRow(iTbody, 4, "기관 데이터가 없습니다. (응답 필드/기간/종목을 확인하세요)");
  }

  // Charts
  // Price (종가 라인): 외국인 daily에 종가가 있는 경우가 많아 우선 사용, 없으면 기관
  const priceSrc = foreignDaily.length ? foreignDaily : instDaily;
  const pricePoints = priceSrc
    .slice()
    .reverse()
    .map(x => {
      const y = Number(x.close_pric);
      const xLabel = toDateKey(x.dt);
      if (!xLabel || !Number.isFinite(y) || y <= 0) return null;
      return { xLabel, y };
    })
    .filter(Boolean);
  drawLineChart($("priceCanvas"), pricePoints, { color: "rgba(106,169,255,0.95)" });

  // Net bars (외국인/기관)
  const { dates, fMap, iMap } = buildDatesAndMaps(foreignDaily, instDaily);

  const showF = $("chkForeign").checked;
  const showI = $("chkInstitution").checked;
  drawNetBars($("netCanvas"), dates, fMap, iMap, showF, showI);
  drawCumulative($("cumCanvas"), dates, fMap, iMap, showF, showI);

  // Patterns table (누적 그래프 아래)
  renderPatternsTable(data.analysis);

  // Simulation results (패턴감지 아래)
  renderSimulation(data.simulation);
}

async function fetchBias(ticker, days) {
  const params = new URLSearchParams();
  params.set("ticker", ticker);
  if (days) params.set("days", String(days));
  const url = `/api/investor-bias?${params.toString()}`;

  const r = await fetch(url, { method: "GET" });
  const text = await r.text();
  let data;
  try { data = JSON.parse(text); } catch { data = { error: "InvalidJSON", detail: text }; }

  if (!r.ok) {
    const msg = data?.detail ? `${data.error || "Error"}: ${data.detail}` : (data?.error || `HTTP ${r.status}`);
    throw new Error(msg);
  }
  return data;
}

async function resolveTicker(q) {
  const params = new URLSearchParams();
  params.set("q", q);
  const url = `/api/resolve-ticker?${params.toString()}`;
  const r = await fetch(url, { method: "GET" });
  const text = await r.text();
  let data;
  try { data = JSON.parse(text); } catch { data = { ok: false, error: "InvalidJSON", detail: text }; }
  if (!r.ok || !data?.ok) {
    // 서버는 404로 "종목명을 다시확인해 주세요"를 내려줌
    const msg = data?.error || "종목명을 다시확인해 주세요";
    throw new Error(msg);
  }
  return data; // { ok:true, ticker, name }
}

function initDefaults() {
  const u = new URL(location.href);
  const ticker = u.searchParams.get("ticker") || "005930";
  const days = u.searchParams.get("days") || "90";
  $("ticker").value = ticker;
  $("days").value = days;
}

async function runQuery() {
  const input = ($("ticker").value || "").trim();
  const days = Number(($("days").value || "").trim());

  let ticker = input;
  // 6자리 숫자가 아니면 종목명으로 간주해 종목코드로 변환
  if (!/^\d{6}$/.test(ticker)) {
    try {
      setStatus("", "종목명 확인 중...");
      const resolved = await resolveTicker(ticker);
      ticker = resolved.ticker;
      // 입력칸은 사용자가 계속 종목명을 쓸 수 있게 유지하되, 상태에 변환 결과를 표시
      setStatus("ok", `종목명 매칭: ${resolved.name || input} → ${ticker}`);
    } catch (e) {
      setStatus("warn", e.message || "종목명을 다시확인해 주세요");
      return;
    }
  }

  setStatus("", "조회 중... (토큰 발급 및 데이터 수집에 수 초가 걸릴 수 있어요)");
  try {
    const data = await fetchBias(ticker, Number.isFinite(days) ? days : 90);
    renderAll(data);
    setStatus("ok", `완료: ${ticker} · ${data.from} ~ ${data.to} · ${data.unique_days}일`);
  } catch (e) {
    setStatus("err", `실패: ${e.message || String(e)}`);
  }
}

window.addEventListener("DOMContentLoaded", () => {
  $("serverBase").textContent = location.origin;
  initDefaults();

  $("btnFetch").addEventListener("click", runQuery);
  $("ticker").addEventListener("keydown", (e) => { if (e.key === "Enter") runQuery(); });
  $("days").addEventListener("keydown", (e) => { if (e.key === "Enter") runQuery(); });
  $("chkForeign").addEventListener("change", applyVisibility);
  $("chkInstitution").addEventListener("change", applyVisibility);

  // 첫 화면 자동 조회
  runQuery();
});

