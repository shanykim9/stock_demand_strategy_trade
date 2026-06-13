function $(id) { return document.getElementById(id); }

function getPlanExchange() {
  const a = $("planExchange");
  const v = (a && a.value) ? a.value : "AUTO";
  return String(v || "AUTO").trim();
}

function getChartExchange() {
  const b = $("exchange");
  const v = (b && b.value) ? b.value : "KRX";
  return String(v || "KRX").trim();
}

async function fetchLogs(tail=200) {
  const p = new URLSearchParams();
  p.set("tail", String(tail));
  p.set("pretty", "1");
  const r = await fetch(`/api/bot/log?${p.toString()}`);
  const data = await r.json();
  if (!r.ok || !data.ok) throw new Error(data.detail || data.error || "로그 조회 실패");
  return data; // {ok, events}
}

function fmtInt(v) {
  const n = Number(v);
  if (!Number.isFinite(n)) return String(v ?? "");
  return n.toLocaleString("ko-KR");
}

function fmtKstIso(s) {
  const t = String(s || "").trim();
  if (!t) return "";
  if (t.includes("T")) return t.replace(".000", "");
  return t;
}

function toDateKey(yyyy_mm_dd) {
  return String(yyyy_mm_dd || "").slice(0, 10);
}

function setStatus(kind, msg) {
  const el = $("status");
  el.className = `status ${kind || ""}`.trim();
  el.textContent = msg || "";
}

function setBotStatus(kind, msg) {
  const el = $("botStatus");
  el.className = `status ${kind || ""}`.trim();
  el.textContent = msg || "";
}

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

// =========================
// Chart theme (LIGHT)
// =========================
const CHART = {
  bg: "#f3f4f6",                 // 아주 옅은 회색
  border: "rgba(15,23,42,0.12)",
  grid: "rgba(15,23,42,0.10)",
  gridStrong: "rgba(15,23,42,0.18)",
  text: "rgba(15,23,42,0.92)",
  muted: "rgba(51,65,85,0.90)",
  up: "rgba(239,68,68,0.98)",    // 양봉=적색
  down: "rgba(37,99,235,0.98)",  // 음봉=청색
  volUp: "rgba(239,68,68,0.22)",
  volDown: "rgba(37,99,235,0.18)",
  line: "rgba(15,23,42,0.90)",
  point: "rgba(15,23,42,0.90)",
};

function drawGrid(ctx, w, h) {
  // 라이트 테마 격자
  ctx.strokeStyle = CHART.grid;
  ctx.lineWidth = 1;
  for (let i = 1; i <= 5; i++) {
    const y = Math.round((h * i) / 6);
    ctx.beginPath();
    ctx.moveTo(0, y);
    ctx.lineTo(w, y);
    ctx.stroke();
  }
}

function drawText(ctx, text, x, y, align="left", color="rgba(231,238,252,0.75)") {
  ctx.fillStyle = color;
  ctx.font = "12px ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, 'Noto Sans KR', Arial";
  ctx.textAlign = align;
  ctx.textBaseline = "middle";
  ctx.fillText(text, x, y);
}

function fillChartBackground(ctx, w, h) {
  // 라이트 배경
  ctx.fillStyle = CHART.bg;
  ctx.fillRect(0, 0, w, h);
  // 테두리
  ctx.strokeStyle = CHART.border;
  ctx.lineWidth = 1;
  ctx.strokeRect(0.5, 0.5, w - 1, h - 1);
}

function drawPanel(ctx, x, y, w, h, opts={}) {
  ctx.save();
  ctx.fillStyle = opts.bg || "rgba(0,0,0,0.18)";
  ctx.strokeStyle = opts.border || "rgba(255,255,255,0.10)";
  ctx.lineWidth = 1;
  ctx.fillRect(x, y, w, h);
  ctx.strokeRect(x + 0.5, y + 0.5, w - 1, h - 1);
  ctx.restore();
}

function drawVerticalBands(ctx, x0, y0, w, h, count, every=10) {
  // “세로 형태의 구간” (시간 축 밴드)로 시각적 구분을 명확히
  if (!Number.isFinite(count) || count <= 0) return;
  const step = w / count;
  ctx.save();
  for (let i = 0; i < count; i++) {
    if (i % every !== 0) continue;
    const x = x0 + i * step;
    // 밴드 폭: 2캔들 정도
    const bw = Math.max(8, step * 2);
    ctx.fillStyle = (Math.floor(i/every) % 2 === 0) ? "rgba(255,255,255,0.03)" : "rgba(0,0,0,0.08)";
    ctx.fillRect(x, y0, bw, h);
  }
  ctx.restore();
}

function drawHorizontalBands(ctx, x0, y0, w, h, bands=5) {
  // 가격 구간(밴드)을 더 또렷하게
  ctx.save();
  for (let i = 0; i < bands; i++) {
    const y = y0 + (h * i) / bands;
    const bh = h / bands;
    ctx.fillStyle = (i % 2 === 0) ? "rgba(255,255,255,0.03)" : "rgba(0,0,0,0.06)";
    ctx.fillRect(x0, y, w, bh);
  }
  ctx.restore();
}

function drawTextBox(ctx, text, x, y, align="left", opts={}) {
  const color = opts.color || "rgba(231,238,252,0.95)";
  const bg = opts.bg || "rgba(0,0,0,0.45)";
  const padX = opts.padX ?? 6;
  const padY = opts.padY ?? 4;
  const radius = opts.radius ?? 7;
  const font = opts.font || "12px ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, 'Noto Sans KR', Arial";
  const border = opts.border || null;
  const shadow = opts.shadow ?? true;

  ctx.save();
  ctx.font = font;
  ctx.textAlign = align;
  ctx.textBaseline = "middle";
  const metrics = ctx.measureText(text);
  const w = Math.ceil(metrics.width) + padX*2;
  const h = 22;
  const left = align === "right" ? (x - w) : align === "center" ? (x - w/2) : x;
  const top = y - h/2;

  if (shadow) {
    ctx.shadowColor = "rgba(0,0,0,0.35)";
    ctx.shadowBlur = 8;
    ctx.shadowOffsetY = 2;
  }

  // rounded rect
  ctx.fillStyle = bg;
  ctx.beginPath();
  const r = Math.min(radius, h/2, w/2);
  ctx.moveTo(left + r, top);
  ctx.arcTo(left + w, top, left + w, top + h, r);
  ctx.arcTo(left + w, top + h, left, top + h, r);
  ctx.arcTo(left, top + h, left, top, r);
  ctx.arcTo(left, top, left + w, top, r);
  ctx.closePath();
  ctx.fill();

  if (border) {
    ctx.shadowColor = "transparent";
    ctx.shadowBlur = 0;
    ctx.strokeStyle = border;
    ctx.lineWidth = 1;
    ctx.stroke();
  }

  // text
  ctx.shadowColor = "transparent";
  ctx.shadowBlur = 0;
  ctx.fillStyle = color;
  // align에 맞는 텍스트 X 좌표(우측 라벨이 안 보이던 버그 수정)
  const tx = align === "right" ? (left + w - padX) : align === "center" ? (left + w/2) : (left + padX);
  ctx.fillText(text, tx, y);
  ctx.restore();
}

function drawOutlinedText(ctx, text, x, y, align="left", opts={}) {
  const font = opts.font || "12px ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, 'Noto Sans KR', Arial";
  const color = opts.color || CHART.text;
  const outline = opts.outline || "rgba(255,255,255,0.95)";
  const outlineWidth = opts.outlineWidth ?? 4;
  ctx.save();
  ctx.font = font;
  ctx.textAlign = align;
  ctx.textBaseline = "middle";
  ctx.lineJoin = "round";
  ctx.miterLimit = 2;
  ctx.lineWidth = outlineWidth;
  ctx.strokeStyle = outline;
  ctx.strokeText(text, x, y);
  ctx.fillStyle = color;
  ctx.fillText(text, x, y);
  ctx.restore();
}

function fmtCompact(n) {
  const v = Number(n);
  if (!Number.isFinite(v)) return String(n ?? "");
  const abs = Math.abs(v);
  if (abs >= 1e12) return (v/1e12).toFixed(1) + "조";
  if (abs >= 1e8)  return (v/1e8).toFixed(1) + "억";
  if (abs >= 1e4)  return (v/1e4).toFixed(1) + "만";
  return String(Math.round(v));
}

function fmtUnitValue(v, unit) {
  const n = Number(v);
  const u = Number(unit) || 1;
  if (!Number.isFinite(n)) return String(v ?? "");
  const x = n / u;
  if (Math.abs(x) >= 100) return String(Math.round(x));
  if (Math.abs(x) >= 10) return x.toFixed(1);
  return x.toFixed(2);
}

// 라이트 테마에서는 별도 박스 없이 텍스트로 축 표시

function drawCloseLine(canvas, dataAsc) {
  const { ctx, w, h } = setupCanvas(canvas);
  ctx.clearRect(0, 0, w, h);
  fillChartBackground(ctx, w, h);
  drawGrid(ctx, w, h);
  if (!dataAsc || dataAsc.length < 2) {
    drawText(ctx, "표시할 데이터가 없습니다.", w/2, h/2, "center", CHART.muted);
    return;
  }
  const padL=64, padR=74, padT=12, padB=26;
  const innerW=w-padL-padR, innerH=h-padT-padB;
  const closes = dataAsc.map(d => Number(d.close)).filter(Number.isFinite);
  let ymin = Math.min(...closes), ymax = Math.max(...closes);
  if (ymin===ymax){ ymin-=1; ymax+=1; }
  const ypad=(ymax-ymin)*0.08; ymin-=ypad; ymax+=ypad;
  const xAt = (i)=> padL + innerW*i/(dataAsc.length-1);
  const yAt = (v)=> padT + innerH*(1-(v-ymin)/(ymax-ymin));

  // 가격 눈금 5개(정밀도↑)
  const ticks = 5;
  for (let i = 0; i < ticks; i++) {
    const t = i/(ticks-1); // 0..1
    const v = ymax - (ymax - ymin) * t;
    const y = yAt(v);
    drawText(ctx, fmtInt(Math.round(v)), 10, y, "left", CHART.text);
    drawText(ctx, fmtInt(Math.round(v)), w-10, y, "right", CHART.text);
  }
  drawText(ctx, dataAsc[0].dt, padL, h-12, "left", CHART.muted);
  drawText(ctx, dataAsc[dataAsc.length-1].dt, w-padR, h-12, "right", CHART.muted);

  ctx.strokeStyle = CHART.line;
  ctx.lineWidth=2;
  ctx.beginPath();
  dataAsc.forEach((d,i)=>{
    const x=xAt(i);
    const y=yAt(Number(d.close));
    if(i===0) ctx.moveTo(x,y);
    else ctx.lineTo(x,y);
  });
  ctx.stroke();

  // 마지막 날 종가 라벨(가독성↑)
  const last = dataAsc[dataAsc.length-1];
  const lastClose = Number(last?.close);
  if (Number.isFinite(lastClose)) {
    const i = dataAsc.length-1;
    const x = xAt(i);
    const y = yAt(lastClose);
    ctx.fillStyle = CHART.point;
    ctx.beginPath();
    ctx.arc(x, y, 3.8, 0, Math.PI*2);
    ctx.fill();
    const align = x > w - 140 ? "right" : "left";
    const label = `종가 ${fmtInt(Math.round(lastClose))}원`;
    drawOutlinedText(ctx, label, align === "right" ? x - 10 : x + 10, y - 14, align, {
      color: CHART.text,
      outlineWidth: 4,
    });
  }
}

function drawCandles(canvas, dataAsc) {
  const { ctx, w, h } = setupCanvas(canvas);
  ctx.clearRect(0, 0, w, h);
  fillChartBackground(ctx, w, h);
  drawGrid(ctx, w, h);
  if (!dataAsc || dataAsc.length < 2) {
    drawText(ctx, "표시할 데이터가 없습니다.", w/2, h/2, "center", CHART.muted);
    return;
  }

  const padL=64, padR=74, padT=12, padB=60; // 하단에 거래량 영역
  const volH = 90;
  const priceH = h - padT - padB - volH;
  const innerW = w - padL - padR;
  const priceY0 = padT;
  const volTop = padT + priceH + 12;
  const volBase = volTop + volH;

  const highs = dataAsc.map(d=>Number(d.high)).filter(Number.isFinite);
  const lows  = dataAsc.map(d=>Number(d.low)).filter(Number.isFinite);
  const vols  = dataAsc.map(d=>Number(d.volume)).filter(Number.isFinite);
  let ymin=Math.min(...lows), ymax=Math.max(...highs);
  if (ymin===ymax){ ymin-=1; ymax+=1; }
  const ypad=(ymax-ymin)*0.08; ymin-=ypad; ymax+=ypad;
  const vmax = Math.max(...vols, 1);

  const xStep = innerW / dataAsc.length;
  const candleW = Math.max(3, Math.min(10, xStep*0.65));

  const xAt = (i)=> padL + xStep*i + xStep/2;
  const yAt = (v)=> padT + priceH*(1-(v-ymin)/(ymax-ymin));

  // 라이트 테마에서는 불필요한 패널/밴드 없이 심플하게

  // 가격 눈금 5개(정밀도↑)
  {
    const ticks = 5;
    for (let i = 0; i < ticks; i++) {
      const t = i/(ticks-1);
      const v = ymax - (ymax - ymin) * t;
      const y = yAt(v);
      // 라인도 함께(구간이 더 명확히 보이도록)
      ctx.strokeStyle = CHART.gridStrong;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(padL, y);
      ctx.lineTo(w - padR, y);
      ctx.stroke();
      drawText(ctx, fmtInt(Math.round(v)), 10, y, "left", CHART.text);
      drawText(ctx, fmtInt(Math.round(v)), w-10, y, "right", CHART.text);
    }
  }
  drawText(ctx, dataAsc[0].dt, padL, h-12, "left", CHART.muted);
  drawText(ctx, dataAsc[dataAsc.length-1].dt, w-padR, h-12, "right", CHART.muted);

  // 축/단위 라벨
  drawText(ctx, "가격(원)", padL, padT-2, "left", CHART.muted);
  const volUnit = vmax >= 1e8 ? 1e8 : vmax >= 1e4 ? 1e4 : 1;
  const volUnitLabel = volUnit === 1e8 ? "억주" : volUnit === 1e4 ? "만주" : "주";
  drawText(ctx, `거래량(${volUnitLabel})`, padL, padT + priceH + 16, "left", CHART.muted);
  drawText(ctx, `최대 ${fmtUnitValue(vmax, volUnit)}${volUnitLabel}`, w-padR, padT + priceH + 16, "right", CHART.muted);

  // 거래량 눈금(0/50%/100%) + 가이드 라인
  const volMid = (volTop + volBase) / 2;
  ctx.strokeStyle = CHART.grid;
  ctx.lineWidth = 1;
  [volTop, volMid, volBase].forEach((yy) => {
    ctx.beginPath();
    ctx.moveTo(padL, yy);
    ctx.lineTo(w - padR, yy);
    ctx.stroke();
  });
  // 좌/우 모두 라벨을 넣어 "양쪽 숫자" 인지가 쉬워지게
  const volLabels = [
    { y: volTop,  pct: "100%", val: vmax },
    { y: volMid,  pct: "50%",  val: vmax/2 },
    { y: volBase, pct: "0%",   val: 0 },
  ];
  volLabels.forEach(({y, pct, val}) => {
    const txt = `${pct} (${fmtUnitValue(val, volUnit)}${volUnitLabel})`;
    drawText(ctx, txt, 10, y, "left", CHART.muted);
    drawText(ctx, txt, w-10, y, "right", CHART.muted);
  });

  // candles + volume
  dataAsc.forEach((d,i)=>{
    const o=Number(d.open), c=Number(d.close), hi=Number(d.high), lo=Number(d.low), v=Number(d.volume);
    if(!Number.isFinite(o+c+hi+lo+v)) return;
    const up = c>=o;
    const color = up ? CHART.up : CHART.down;
    const x = xAt(i);

    // wick
    ctx.strokeStyle=color;
    ctx.lineWidth=1;
    ctx.beginPath();
    ctx.moveTo(x, yAt(hi));
    ctx.lineTo(x, yAt(lo));
    ctx.stroke();

    // body
    const yO=yAt(o), yC=yAt(c);
    const top=Math.min(yO,yC);
    const bodyH=Math.max(1, Math.abs(yC-yO));
    ctx.fillStyle=color;
    ctx.fillRect(x - candleW/2, top, candleW, bodyH);

    // volume (하단)
    const vh = Math.max(1, (v / vmax) * (volH-6));
    ctx.fillStyle = up ? CHART.volUp : CHART.volDown;
    ctx.fillRect(x - candleW/2, volBase - vh, candleW, vh);
  });

  // 마지막 날 종가 라벨(캔들 차트에도 표시)
  const last = dataAsc[dataAsc.length-1];
  const lastClose = Number(last?.close);
  if (Number.isFinite(lastClose)) {
    const i = dataAsc.length-1;
    const x = xAt(i);
    const y = yAt(lastClose);
    ctx.fillStyle = CHART.point;
    ctx.beginPath();
    ctx.arc(x, y, 3.8, 0, Math.PI*2);
    ctx.fill();
    const align = x > w - 160 ? "right" : "left";
    drawOutlinedText(ctx, `종가 ${fmtInt(Math.round(lastClose))}원`, align === "right" ? x - 10 : x + 10, y, align, {
      color: CHART.text,
      outlineWidth: 4,
    });
  }
}

async function resolveTicker(q) {
  const params = new URLSearchParams();
  params.set("q", q);
  const r = await fetch(`/api/resolve-ticker?${params.toString()}`);
  const data = await r.json();
  if (!r.ok || !data.ok) throw new Error(data.error || "종목명을 다시확인해 주세요");
  return data; // {ok,ticker,name}
}

async function fetchOhlcv(tickerOrName) {
  const params = new URLSearchParams();
  params.set("ticker", tickerOrName);
  const r = await fetch(`/api/ohlcv?${params.toString()}`);
  const data = await r.json();
  if (!r.ok) throw new Error(data.detail || data.error || "조회 실패");
  return data;
}

async function savePlan() {
  const q = ($("ticker").value || "").trim();
  if (!q) { setBotStatus("warn", "종목코드/종목명을 입력하세요."); return; }
  const exchange = getPlanExchange();

  const payload = {
    ticker: q,
    exchange,
    qty: Number(($("qty").value || "1").trim()),
    buy_price: Number(($("buyPrice").value || "0").trim()),
    stop_loss: Number(($("stopLoss").value || "0").trim()),
    take_profit: Number(($("takeProfit").value || "0").trim()),
  };

  const r = await fetch("/api/bot/plan", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await r.json();
  if (!r.ok || !data.ok) throw new Error(data.detail || data.error || "저장 실패");
  const armed = (data.stored && data.stored.enabled === true);
  if (armed) {
    setBotStatus("ok", `전략 저장됨(ARM 유지): ${data.plan.name || data.plan.ticker} (${data.plan.ticker}) · 거래소=${data.plan.exchange}`);
  } else {
    setBotStatus("warn", `전략 저장됨(ARM:OFF): ${data.plan.name || data.plan.ticker} (${data.plan.ticker}) · '자동매매 시작'을 눌러야 실행됩니다.`);
  }
  // 저장 직후 상태 즉시 반영(ARM/OFF 등 UI 깜빡임 최소화)
  try { await pollStatus(); } catch { /* ignore */ }
}

async function botStart() {
  const r = await fetch("/api/bot/start", { method: "POST" });
  const data = await r.json();
  if (!r.ok || !data.ok) throw new Error(data.detail || data.error || "시작 실패");
  setBotStatus("ok", `활성화됨(ARM) · 모드=${data.status.mode || "-"}`);
  // start 응답 직후 status 폴링으로 badge 즉시 갱신
  try { await pollStatus(); } catch { /* ignore */ }
}

async function botStop() {
  const r = await fetch("/api/bot/stop", { method: "POST" });
  const data = await r.json();
  if (!r.ok || !data.ok) throw new Error(data.detail || data.error || "중지 실패");
  setBotStatus("warn", "비활성화됨(DISARM)");
  try { await pollStatus(); } catch { /* ignore */ }
}

async function cancelOrder(ordNo, stkCd, exchange, qty, trdeTp) {
  const payload = {
    ord_no: String(ordNo || "").trim(),
    stk_cd: String(stkCd || "").trim(),
    exchange: String(exchange || "").trim(),
    qty: (qty !== undefined && qty !== null) ? Number(qty) : undefined,
    trde_tp: String(trdeTp || "").trim(),
  };
  if (!payload.ord_no) throw new Error("주문번호(ord_no)가 없습니다.");
  if (!payload.stk_cd) throw new Error("종목코드(stk_cd)가 없습니다.");
  if (!payload.exchange) payload.exchange = "KRX";

  const r = await fetch("/api/bot/cancel", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await r.json();
  if (!r.ok || !data.ok) throw new Error(data.detail || data.error || "취소 실패");
  return data;
}

async function pollStatus() {
  try {
    const r = await fetch("/api/bot/status");
    const data = await r.json();
    if (!r.ok || !data.ok) return;
    const st = data.status || {};
    $("modeBadge").textContent = st.mode || (window.__BUYSELL_CONFIG__?.dryRun ? "DRY_RUN" : "LIVE");
    $("lastPrice").textContent = st.last_price ? fmtInt(st.last_price) : "-";
    $("posQty").textContent = String(st.position_qty ?? 0);
    if (st.last_bid) $("bestBid").textContent = fmtInt(st.last_bid);
    if (st.last_ask) $("bestAsk").textContent = fmtInt(st.last_ask);
    // 표시용 거래소: runner가 고른 active_exchange가 있으면 그 값을 우선 표시
    const activeEx = String(st.active_exchange || "").trim();
    const planEx = (st.plan && st.plan.exchange) ? String(st.plan.exchange).trim() : "";
    if ($("planExBadge")) $("planExBadge").textContent = activeEx || planEx || "-";
    if ($("armBadge")) $("armBadge").textContent = (st.enabled === true) ? "ON" : "OFF";

    // runner status
    const runnerAlive = (st.runner_alive === true);
    const age = (st.runner_heartbeat_age_sec !== undefined && st.runner_heartbeat_age_sec !== null)
      ? Number(st.runner_heartbeat_age_sec) : null;
    if ($("runnerBadge")) {
      $("runnerBadge").textContent = runnerAlive ? `ON${(age !== null ? ` (${age}s)` : "")}` : "OFF";
    }
    const rh = $("runnerHelp");
    if (rh) {
      const te = st.token_error || null;
      const hasTokenErr = te && (te.code || te.return_code || te.detail);

      if (!runnerAlive) {
        rh.style.display = "block";
        rh.textContent = "bot_runner가 실행 중이 아닙니다. UI 서버를 꺼도 자동매매를 지속하려면 bot_runner.py를 별도 실행(또는 작업 스케줄러/서비스 등록)해야 합니다.";
      } else if (hasTokenErr) {
        rh.style.display = "block";
        const code = te.code ? `코드=${te.code}` : "";
        const rc = te.return_code ? `return_code=${te.return_code}` : "";
        const hint = te.hint ? `힌트: ${te.hint}` : "";
        const parts = [code, rc].filter(Boolean).join(", ");
        rh.textContent = `토큰 오류로 실거래가 불가합니다. ${parts ? `(${parts}) ` : ""}${hint}`.trim();
      } else {
        rh.style.display = "none";
        rh.textContent = "";
      }
    }

    // last buy info (runner-provided)
    if ($("lastBuyOrdPx")) {
      const v = Number(st.last_buy_order_price || 0);
      $("lastBuyOrdPx").textContent = v > 0 ? fmtInt(v) : "-";
    }
    if ($("lastBuyFillPx")) {
      const v = Number(st.last_buy_fill_price || 0);
      $("lastBuyFillPx").textContent = v > 0 ? fmtInt(v) : "-";
    }
    if ($("lastBuyOrdNo")) {
      const v = String(st.last_buy_order_no || "").trim();
      $("lastBuyOrdNo").textContent = v ? v : "-";
    }

    // position detail box content (포지션 pill/버튼 클릭으로 토글)
    const posBox = $("posBox");
    if (posBox) {
      const qty = Number(st.position_qty || 0);
      const ordNo = String(st.last_buy_order_no || "").trim();
      const ordPx = Number(st.last_buy_order_price || 0);
      const fillPx = Number(st.last_buy_fill_price || 0);
      const ordAt = fmtKstIso(st.last_buy_order_at || "");
      const fillTm = String(st.last_buy_fill_time || "").trim();
      const plan = st.plan || {};
      const planBuy = Number(plan.buy_price || 0);
      const planSL = Number(plan.stop_loss || 0);
      const planTP = Number(plan.take_profit || 0);
      const planEx = String(plan.exchange || "").trim();

      const lines = [];
      lines.push(`보유수량: ${qty}`);
      if (planEx) lines.push(`전략 거래소: ${planEx}`);
      if (planBuy > 0) lines.push(`전략 매수가(입력): ${fmtInt(planBuy)}원`);
      if (planSL > 0) lines.push(`전략 손절가(입력): ${fmtInt(planSL)}원`);
      if (planTP > 0) lines.push(`전략 익절가(입력): ${fmtInt(planTP)}원`);
      if (ordNo) lines.push(`최근매수 주문번호: ${ordNo}`);
      if (ordPx > 0) lines.push(`최근매수 주문가: ${fmtInt(ordPx)}원`);
      if (ordAt) lines.push(`최근매수 주문시각: ${ordAt}`);
      if (fillPx > 0) lines.push(`최근매수 체결가: ${fmtInt(fillPx)}원`);
      if (fillTm) lines.push(`최근매수 체결시간(원본): ${fillTm}`);
      if (qty <= 0) lines.push("현재 포지션이 없습니다.");
      posBox.textContent = lines.join("\n") + "\n";
    }

    // ✅ 선택값을 서버 상태로 강제 덮어쓰지 않음(사용자 NXT/AUTO 선택이 KRX로 되돌아가는 문제 방지)

    // ------------------------------------------------------------
    // 종목 필터(엔트리/미체결/체결 공통)
    // ------------------------------------------------------------
    function normTicker(v) {
      const s = String(v || "");
      const d = s.replace(/\D/g, "").slice(0, 6);
      return d || s.trim();
    }
    const filterSel = $("symFilter");
    const entriesAll = Array.isArray(st.entries) ? st.entries.slice() : [];
    const booksAll = Array.isArray(st.books) ? st.books.slice() : [];

    // ticker->name 추정(엔트리 기반)
    const nameByTicker = new Map();
    for (const e of entriesAll) {
      const t = normTicker(e.ticker || "");
      if (!t) continue;
      const nm = String(e.name || "").trim();
      if (nm && !nameByTicker.has(t)) nameByTicker.set(t, nm);
    }
    // 후보 ticker 집합
    const tickerSet = new Set();
    for (const e of entriesAll) {
      const t = normTicker(e.ticker || "");
      if (t) tickerSet.add(t);
    }
    for (const b of booksAll) {
      const t = normTicker(b.ticker || "");
      if (t) tickerSet.add(t);
    }
    const tickers = Array.from(tickerSet.values()).sort();

    // 옵션 갱신(사용자 선택은 유지)
    let selected = filterSel ? String(filterSel.value || "ALL") : "ALL";
    if (filterSel) {
      const prev = selected;
      filterSel.innerHTML = "";
      const optAll = document.createElement("option");
      optAll.value = "ALL";
      optAll.textContent = `전체 (${tickers.length || 0})`;
      filterSel.appendChild(optAll);
      for (const t of tickers) {
        const nm = nameByTicker.get(t);
        const opt = document.createElement("option");
        opt.value = t;
        opt.textContent = nm ? `${nm} (${t})` : t;
        filterSel.appendChild(opt);
      }
      // 선택 복원
      if (prev && (prev === "ALL" || tickers.includes(prev))) {
        filterSel.value = prev;
        selected = prev;
      } else {
        filterSel.value = "ALL";
        selected = "ALL";
      }
      if (!filterSel.__bound) {
        filterSel.__bound = true;
        filterSel.addEventListener("change", () => { pollStatus(); });
      }
    }

    function matchesFilter(ticker6) {
      if (!selected || selected === "ALL") return true;
      return normTicker(ticker6) === selected;
    }

    // books → 그룹 목록 생성(없으면 기존 st.unfilled/st.fills로 fallback)
    const groups = [];
    if (booksAll.length) {
      for (const b of booksAll) {
        const t = normTicker(b.ticker || "");
        if (!t) continue;
        if (!matchesFilter(t)) continue;
        const ex = String(b.exchange || "").trim() || "-";
        const nm = nameByTicker.get(t);
        const label = nm ? `${nm} (${t}) · ${ex}` : `${t} · ${ex}`;
        groups.push({
          label,
          ticker: t,
          exchange: ex,
          unfilled: Array.isArray(b.unfilled) ? b.unfilled.slice() : [],
          fills: Array.isArray(b.fills) ? b.fills.slice() : [],
        });
      }
      groups.sort((a, b) => String(a.label).localeCompare(String(b.label)));
    } else {
      groups.push({
        label: "",
        ticker: "",
        exchange: "",
        unfilled: Array.isArray(st.unfilled) ? st.unfilled.slice() : [],
        fills: Array.isArray(st.fills) ? st.fills.slice() : [],
      });
    }

    // unfilled table
    const utbody = document.querySelector("#tblUnfilled tbody");
    if (utbody) {
      utbody.innerHTML = "";
      let anyRow = false;
      for (const g of groups) {
        const rows = (g.unfilled || []).slice(-15).reverse();
        if (rows.length === 0) continue;
        // 그룹 헤더(전체/다종목일 때만 표시)
        if ((selected === "ALL") && g.label) {
          const trh = document.createElement("tr");
          const tdh = document.createElement("td");
          tdh.colSpan = 7;
          tdh.className = "mono";
          tdh.style.fontWeight = "700";
          tdh.style.padding = "10px 10px";
          tdh.textContent = g.label;
          trh.appendChild(tdh);
          utbody.appendChild(trh);
        }
        rows.forEach(x => {
          const tr = document.createElement("tr");
        const dt = x.dt || x.date || x.ord_dt || x.ordDtm || x.ord_date || "";
        const tm = x.tm || x.time || x.ord_tm || x.ordTm || x.ord_time || x.ord_tm || x.ordTm || x.ord_tm || "";
        const ordNo = x.ord_no || x.ordNo || x.order_no || "";
        const side = x.io_tp_nm || x.trde_tp || x.side || x.bs_tp || "";
        const px = x.ord_pric || x.ord_pric || x.ord_uv || x.ord_prc || x.ord_price || "";
        const qty = x.ord_qty || x.qty || x.ordQty || "";
        const unfilled = x.oso_qty || x.nccs_qty || x.unfilled_qty || x.remain_qty || x.miche_qty || x.not_cntr_qty || "";
        const stkCd = x.stk_cd || x.stkCd || (st.plan ? st.plan.ticker : "") || "";
        let ex = String(x.stex_tp_txt || x.stex_tp || st.active_exchange || (st.plan ? st.plan.exchange : "") || "KRX").trim();
        // stex_tp가 "1/2/3"으로 오는 케이스 보정
        if (ex === "1") ex = "KRX";
        if (ex === "2") ex = "NXT";
        if (ex === "3") ex = "SOR";
        const trdeTp = String(x.trde_tp || "").trim();
        const cells = [
          `${dt} ${tm}`.trim(),
          String(ordNo),
          String(side),
          fmtInt(px),
          fmtInt(qty),
          fmtInt(unfilled),
        ];
        cells.forEach((c, i) => {
          const td = document.createElement("td");
          td.textContent = c;
          if (i >= 3) td.className = "right mono";
          else td.className = "mono";
          tr.appendChild(td);
        });

        // cancel button cell
        const tdBtn = document.createElement("td");
        tdBtn.className = "right";
        const btn = document.createElement("button");
        btn.className = "btn btnBad";
        btn.style.padding = "6px 10px";
        btn.style.fontSize = "12px";
        btn.textContent = "주문취소";
        btn.disabled = !ordNo;
        btn.addEventListener("click", async (ev) => {
          ev.preventDefault();
          ev.stopPropagation();
          if (!ordNo) return;
          const ok = confirm(`주문을 취소할까요?\n- 주문번호: ${ordNo}\n- 거래소: ${ex}\n- 종목: ${stkCd}`);
          if (!ok) return;
          btn.disabled = true;
          try {
            await cancelOrder(ordNo, stkCd, ex, Number(unfilled || qty || 1), trdeTp);
            setBotStatus("ok", `주문취소 요청 완료: ord_no=${ordNo}`);
            await pollStatus();
          } catch (e) {
            setBotStatus("err", `주문취소 실패: ${e.message || String(e)}`);
          } finally {
            btn.disabled = false;
          }
        });
        tdBtn.appendChild(btn);
        tr.appendChild(tdBtn);
        utbody.appendChild(tr);
          anyRow = true;
        });
      }
      if (!anyRow) {
        const tr = document.createElement("tr");
        const td = document.createElement("td");
        td.colSpan = 7;
        td.className = "mono";
        td.style.color = "rgba(159,176,208,0.95)";
        td.style.padding = "14px 10px";
        td.textContent = "미체결 내역이 없습니다.";
        tr.appendChild(td);
        utbody.appendChild(tr);
      }
    }

    // entries table (분할매수/TP·SL)
    const etbody = document.querySelector("#tblEntries tbody");
    if (etbody) {
      etbody.innerHTML = "";
      let entries = Array.isArray(st.entries) ? st.entries.slice() : [];
      // 보기 개선: 취소된 엔트리는 기본 숨김(미체결에서 취소하면 함께 사라져 보이게)
      entries = entries.filter(e => String(e?.status || "").toUpperCase() !== "CANCELLED");
      if (selected !== "ALL") {
        entries = entries.filter(e => normTicker(e.ticker || "") === selected);
      }
      // 최근 것이 아래로 쌓이는 걸 방지: 최신이 위로
      entries.sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")));

      function stLabel(s) {
        const x = String(s || "").toUpperCase();
        if (x === "PENDING") return "대기(매수전)";
        if (x === "BUY_SUBMITTED") return "매수주문";
        if (x === "FILLED") return "보유(감시중)";
        if (x === "SELL_SUBMITTED") return "매도주문";
        if (x === "CLOSED") return "종결";
        if (x === "CANCELLED") return "취소";
        return x || "-";
      }

      for (const e of entries.slice(0, 50)) {
        const tr = document.createElement("tr");
        const created = String(e.created_at || "").replace("T", " ").slice(0, 19);
        const ticker = String(e.ticker || "");
        const name = String(e.name || "");
        const exPlan = String(e.exchange || "");
        const exAct = String(e.active_exchange || "");
        const buyPx = Number(e.buy_price || 0);
        const qty = Number(e.qty || 0);
        const tp = Number(e.take_profit || 0);
        const sl = Number(e.stop_loss || 0);
        const status = stLabel(e.status);
        const bOrd = String(e.buy_ord_no || "");
        const bFill = Number(e.buy_fill_price || 0);
        const sOrd = String(e.sell_ord_no || "");
        const tpOrd = String(e.tp_ord_no || "");
        const slOrd = String(e.sl_ord_no || "");
        const closed = String(e.closed_at || "").replace("T", " ").slice(0, 19);

        const cells = [
          created || "-",
          name ? `${name} (${ticker})` : (ticker || "-"),
          exPlan || "-",
          exAct || "-",
          buyPx ? fmtInt(buyPx) : "-",
          qty ? fmtInt(qty) : "-",
          tp ? fmtInt(tp) : "-",
          sl ? fmtInt(sl) : "-",
          status,
          bOrd || "-",
          bFill ? fmtInt(bFill) : "-",
          (tpOrd || slOrd) ? `TP:${tpOrd || "-"} / SL:${slOrd || "-"}` : (sOrd || "-"),
          closed || "-",
        ];

        cells.forEach((c, i) => {
          const td = document.createElement("td");
          td.textContent = c;
          // numeric right align for price/qty columns
          if ([4,5,6,7,10].includes(i)) td.className = "right mono";
          else td.className = "mono";
          // FILLED 상태는 강조
          if (i === 8 && String(e.status || "").toUpperCase() === "FILLED") {
            td.style.fontWeight = "700";
          }
          tr.appendChild(td);
        });
        etbody.appendChild(tr);
      }

      if (etbody.children.length === 0) {
        const tr = document.createElement("tr");
        const td = document.createElement("td");
        td.colSpan = 13;
        td.className = "mono";
        td.style.color = "rgba(159,176,208,0.95)";
        td.style.padding = "14px 10px";
        td.textContent = "엔트리 내역이 없습니다. '전략 저장'을 누르면 엔트리가 생성됩니다.";
        tr.appendChild(td);
        etbody.appendChild(tr);
      }
    }

    // fills table
    const tbody = document.querySelector("#tblFills tbody");
    if (tbody) {
      tbody.innerHTML = "";
      let anyRow = false;
      for (const g of groups) {
        const rows = (g.fills || []).slice(-15).reverse();
        if (rows.length === 0) continue;
        if ((selected === "ALL") && g.label) {
          const trh = document.createElement("tr");
          const tdh = document.createElement("td");
          tdh.colSpan = 6;
          tdh.className = "mono";
          tdh.style.fontWeight = "700";
          tdh.style.padding = "10px 10px";
          tdh.textContent = g.label;
          trh.appendChild(tdh);
          tbody.appendChild(trh);
        }
        rows.forEach(x => {
          const tr = document.createElement("tr");
          const dt = x.dt || x.date || x.ord_dt || x.cntr_dt || "";
          const tm = x.tm || x.time || x.cntr_tm || x.ord_tm || x.ordTm || "";
          const ordNo = x.ord_no || x.ordNo || x.order_no || "";
          const side = x.io_tp_nm || x.trde_tp || x.side || x.bs_tp || "";
          const px = x.cntr_pric || x.cntr_prc || x.cntr_uv || x.cntr_price || x.exec_price || x.ord_pric || x.ord_uv || "";
          const qty = x.cntr_qty || x.cntr_qt || x.exec_qty || x.ord_qty || "";
          const unfilled = x.oso_qty || x.nccs_qty || x.unfilled_qty || x.remain_qty || x.miche_qty || "";
          const cells = [
            `${dt} ${tm}`.trim(),
            String(ordNo),
            String(side),
            fmtInt(px),
            fmtInt(qty),
            fmtInt(unfilled),
          ];
          cells.forEach((c, i) => {
            const td = document.createElement("td");
            td.textContent = c;
            if (i >= 3) td.className = "right mono";
            else td.className = "mono";
            tr.appendChild(td);
          });
          tbody.appendChild(tr);
          anyRow = true;
        });
      }
      if (!anyRow) {
        const tr = document.createElement("tr");
        const td = document.createElement("td");
        td.colSpan = 6;
        td.className = "mono";
        td.style.color = "rgba(159,176,208,0.95)";
        td.style.padding = "14px 10px";
        td.textContent = "체결 내역이 없습니다.";
        tr.appendChild(td);
        tbody.appendChild(tr);
      }
    }

    // runner 구조에서는 st.running이 없을 수 있으므로, message를 항상 표시
    const msg = String(st.message || "").trim();
    if (msg) {
      const te = st.token_error || null;
      const isTokenErr = te && (te.code || te.return_code || te.detail);
      const kind =
        isTokenErr ? "err" :
        (/실패|error|오류/i.test(msg) ? "err" :
         (/대기/i.test(msg) ? "warn" : ""));
      setBotStatus(kind, msg);
    }
  } catch {
    // ignore
  }
}

async function loadChart() {
  const q = ($("ticker").value || "").trim();
  if (!q) { setStatus("warn", "종목코드/종목명을 입력하세요."); return; }
  const exchange = getChartExchange();
  setStatus("", "조회 중...");
  try {
    // 종목명이라면 먼저 매칭 정보 표시
    if (!/^\d{6}$/.test(q)) {
      const resolved = await resolveTicker(q);
      setStatus("ok", `종목명 매칭: ${resolved.name || q} → ${resolved.ticker}`);
    }
    const data = await fetchOhlcv(q);
    const ohlcv = Array.isArray(data.ohlcv) ? data.ohlcv.slice() : [];
    const asc = ohlcv.slice().reverse();
    drawCandles($("candleCanvas"), asc);
    drawCloseLine($("closeCanvas"), asc);
    const nm = data.name ? `${data.name} ` : "";
    setStatus("ok", `완료: ${nm}${data.ticker} · ${data.from} ~ ${data.to} · ${data.days}일 (API: ${data.meta?.used_api || "?"})`);

    // quote
    try {
      const p = new URLSearchParams();
      p.set("ticker", q);
      p.set("exchange", exchange);
      const qr = await fetch(`/api/quote?${p.toString()}`);
      const qd = await qr.json();
      if (qr.ok && qd.ok) {
        $("bestBid").textContent = qd.best_bid ? fmtInt(qd.best_bid) : "-";
        $("bestAsk").textContent = qd.best_ask ? fmtInt(qd.best_ask) : "-";
      }
    } catch {
      // ignore
    }
  } catch (e) {
    setStatus("err", `실패: ${e.message || String(e)}`);
  }
}

function initDefaults() {
  $("serverBase").textContent = location.origin;
  $("modeBadge").textContent = window.__BUYSELL_CONFIG__?.dryRun ? "DRY_RUN" : "LIVE";
  $("ticker").value = "005930";
  if ($("exchange")) $("exchange").value = "KRX";
  if ($("planExchange")) $("planExchange").value = "AUTO";
}

window.addEventListener("DOMContentLoaded", () => {
  initDefaults();
  // ✅ 헷갈림 방지:
  // - 상단 exchange: 차트/호가 조회용
  // - planExchange: 주문(자동매매)용
  // 사용자가 상단 거래소만 바꾸고 주문 거래소도 바뀐 줄 착각하는 경우가 많아 안내 문구를 띄웁니다.
  let _lastChartEx = getChartExchange().toUpperCase();
  const exSel = $("exchange");
  if (exSel) {
    exSel.addEventListener("change", () => {
      const newEx = getChartExchange().toUpperCase();
      const peSel = $("planExchange");
      const pe = (peSel && peSel.value) ? String(peSel.value).toUpperCase() : "AUTO";
      // planExchange가 기존 chart와 같았던 경우에만 "따라오게" 해서 의도치 않은 변경을 최소화
      if (peSel && (pe === _lastChartEx)) {
        peSel.value = newEx;
      }
      _lastChartEx = newEx;
      setBotStatus("warn", `차트/호가 거래소를 ${newEx}로 변경했습니다. 주문 거래소는 아래 '거래소(주문)'에서 확인 후 '전략 저장'을 눌러야 반영됩니다.`);
    });
  }
  $("btnLoad").addEventListener("click", loadChart);
  $("ticker").addEventListener("keydown", (e) => { if (e.key === "Enter") loadChart(); });
  $("btnSavePlan").addEventListener("click", async () => {
    try { await savePlan(); } catch (e) { setBotStatus("err", `실패: ${e.message || String(e)}`); }
  });
  $("btnStart").addEventListener("click", async () => {
    try { await botStart(); } catch (e) { setBotStatus("err", `실패: ${e.message || String(e)}`); }
  });
  $("btnStop").addEventListener("click", async () => {
    try { await botStop(); } catch (e) { setBotStatus("err", `실패: ${e.message || String(e)}`); }
  });
  const btnLogs = $("btnLogs");
  if (btnLogs) {
    btnLogs.addEventListener("click", async () => {
      const box = $("logBox");
      if (!box) return;
      // toggle
      if (box.style.display === "block") {
        box.style.display = "none";
        return;
      }
      box.style.display = "block";
      box.textContent = "로그 불러오는 중...\n";
      try {
        const data = await fetchLogs(200);
        const events = Array.isArray(data.events) ? data.events : [];
        if (!events.length) {
          box.textContent = "로그가 아직 없습니다.\n";
          return;
        }
        // 사람이 읽기 쉬운 요약 포맷
        const out = [];
        for (const ev of events) {
          const dt = ev.dt ? `[${ev.dt}]` : "";
          const msg = ev.msg || "";
          const parts = [];
          if (ev.exchange) parts.push(`ex=${ev.exchange}`);
          if (ev.stk_cd) parts.push(`stk=${ev.stk_cd}`);
          if (ev.ord_no) parts.push(`ord=${ev.ord_no}`);
          if (ev.px) parts.push(`px=${ev.px}`);
          if (ev.reason) parts.push(`reason=${ev.reason}`);
          const meta = parts.length ? ` (${parts.join(", ")})` : "";
          out.push(`${dt} ${msg}${meta}`.trim());
        }
        box.textContent = out.join("\n") + "\n";
        box.scrollTop = box.scrollHeight;
      } catch (e) {
        box.textContent = `로그 조회 실패: ${e.message || String(e)}\n`;
      }
    });
  }

  // 포지션 토글: (1) 신버전 btnPos가 있으면 사용, (2) 구버전 pill(포지션 span의 부모)에도 클릭을 붙임
  function ensurePosBox() {
    let box = $("posBox");
    if (box) return box;
    // 구버전 템플릿에도 동작하도록 동적으로 생성
    const host = $("runnerHelp") || $("botStatus");
    if (!host) return null;
    box = document.createElement("div");
    box.id = "posBox";
    box.className = "kv mono";
    box.style.display = "none";
    box.style.marginTop = "10px";
    box.style.whiteSpace = "pre";
    box.style.overflow = "auto";
    host.insertAdjacentElement("afterend", box);
    return box;
  }

  function attachPosToggle(el) {
    if (!el) return;
    if (el.__posToggleBound) return;
    el.__posToggleBound = true;
    el.style.cursor = "pointer";
    el.addEventListener("click", () => {
      const box = ensurePosBox();
      if (!box) return;
      box.style.display = (box.style.display === "block") ? "none" : "block";
    });
  }

  attachPosToggle($("btnPos"));
  // 구버전 폴백: posQty의 가장 가까운 pill에 클릭 부여
  const posQty = $("posQty");
  if (posQty) {
    const pill = posQty.closest(".pill");
    attachPosToggle(pill);
  }

  // 처음 로드 시 차트 자동조회
  loadChart();

  // 봇 상태 폴링
  setInterval(pollStatus, 1000);
});

