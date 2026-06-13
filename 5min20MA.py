"""
ma20_break_sim.py — 5분봉 MA20 돌파 매매 시뮬레이터
═══════════════════════════════════════════════════════════════
실행: streamlit run ma20_break_sim.py

[매수] 종가 > MA20 AND 전봉 종가 <= MA20 (골든크로스)
[매도] 종가 < MA20 (이탈)
[금액] 1회 매수 100만원 고정, 1포지션만 유지
[데이터] 키움 REST API ka10080 (주식분봉차트조회요청) 5분봉
"""
from __future__ import annotations
import os, math
from datetime import datetime, timedelta, date as date_
from pathlib import Path
import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
try:
    from dotenv import load_dotenv; load_dotenv()
except: pass
import demand as core

# ── 상수 ──────────────────────────────────────────────────────
COLORS = {
    "bull": "#E53935", "bear": "#1E88E5",
    "bv":   "#EF9A9A", "bev":  "#90CAF9",
    "ma20": "#FFD600",
    "bg":   "#131722", "grid": "#1E222D", "txt": "#D1D4DC",
    "buy":  "#00E676", "sell": "#2196F3", "sl":  "#F44336",
}
INVEST = 1_000_000   # 1회 매수금액 100만원
TIC_SCOPE = "5"      # 5분봉
CACHE_DIR = Path(".cache/ma20_break"); CACHE_DIR.mkdir(parents=True, exist_ok=True)

_CSS = """<style>
[data-testid="stMetric"]{background:linear-gradient(135deg,#1a1f2e,#151926);
  border:1px solid #2a2f42;border-radius:10px;padding:14px 18px}
[data-testid="stMetric"] label{color:#8b8fa3!important;font-size:.78rem!important}
[data-testid="stMetric"] [data-testid="stMetricValue"]{color:#e8eaed!important;
  font-size:1.15rem!important;font-weight:600!important}
section[data-testid="stSidebar"]{background:#0f1117}
</style>"""

# ── 유틸 ──────────────────────────────────────────────────────
def _int(v, d=0):
    if v is None: return d
    if isinstance(v, int): return v
    s = str(v).strip().replace(",","").replace("+","").replace("-","",1).strip()
    if not s: return d
    try: return int(float(s))
    except: return d

def _f(row, keys):
    for k in keys:
        if k in row and str(row.get(k,"")).strip() != "": return row.get(k)
    return None

def _resolve(q):
    q = (q or "").strip()
    if not q: raise RuntimeError("종목명 입력 필요")
    if q.isdigit() and len(q) == 6: tk = q
    else:
        tk, err = core.resolve_ticker(q)
        if err or not tk: raise RuntimeError(err or f"'{q}' 확인 필요")
    nm = tk
    try: nm = (core._krx_cache.get("name_by_code") or {}).get(tk, tk)
    except: pass
    return tk, nm

def _today(): return datetime.now(core.TZ).strftime("%Y-%m-%d")

# ── 분봉 데이터 로딩 ─────────────────────────────────────────
def _pdt_min(v):
    """체결시간 파싱: '20240101090500' → '2024-01-01 09:05:00'"""
    if v is None: return None
    s = str(v).strip()
    if len(s) >= 14 and s[:14].isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]} {s[8:10]}:{s[10:12]}:{s[12:14]}"
    if len(s) >= 12 and s[:12].isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]} {s[8:10]}:{s[10:12]}:00"
    return None

def _cpath_min(tk): return CACHE_DIR/f"{tk}_5m.parquet", CACHE_DIR/f"{tk}_5m.csv"

def _load_c(tk):
    pp, pc = _cpath_min(tk)
    for p, rd in [(pp, lambda p: pd.read_parquet(p)),
                  (pc, lambda p: pd.read_csv(p))]:
        try:
            if p.exists():
                df = rd(p); df["dt"] = df["dt"].astype(str)
                if not df.empty: return df.to_dict("records")
        except: pass
    return []

def _save_c(tk, bars):
    if not bars: return
    pp, pc = _cpath_min(tk); df = pd.DataFrame(bars)
    try: df.to_parquet(pp, index=False); return
    except: pass
    try: df.to_csv(pc, index=False, encoding="utf-8-sig")
    except: pass

def _fetch_raw_min(token, ticker, base_dt=None, mp=40):
    """ka10080 분봉 조회 (tic_scope=5)"""
    stex = (os.getenv("KIWOOM_DMST_STEX_TP") or "KRX").strip().upper()
    upd  = (os.getenv("KIWOOM_OHLCV_UPD_STKPC_TP") or "1").strip()
    edt  = base_dt or datetime.now(core.TZ).strftime("%Y%m%d")
    # upd_stkpc_tp 는 필수 파라미터
    cm   = {"stk_cd": ticker, "tic_scope": TIC_SCOPE,
            "upd_stkpc_tp": upd,
            "stex_tp": stex, "dmst_stex_tp": stex}
    bodies = [
        {**cm, "base_dt": edt},
        {**cm, "dt": edt},
        {**cm},
    ]
    last_err = ""
    for body in bodies:
        try:
            res = core.call_tr_all_pages(
                token=token, api_id="ka10080", body=body,
                endpoint="/api/dostk/chart", max_pages=mp)
            # 응답 키 확인 — rows 외 다른 키도 시도
            rows = (res.get("rows") or res.get("output") or
                    res.get("data") or res.get("chart") or [])
            if not rows:
                # 응답 구조를 에러 메시지에 포함해 디버그
                last_err = f"응답키={list(res.keys())}, body={body}"
                continue
            dd = {}
            # 첫 번째 행의 키 목록으로 필드명 자동 감지
            sample = rows[0] if rows else {}
            all_keys = list(sample.keys())
            # 체결시간 필드 자동 탐색 — cntr_tm 우선, 없으면 14자리 숫자 키 탐색
            dt_key = None
            for preferred in ["cntr_tm","dt","date","stck_cntg_hour","cntr_time"]:
                if preferred in all_keys:
                    dt_key = preferred; break
            if dt_key is None:
                for k in all_keys:
                    v = str(sample.get(k,"")).strip().lstrip("+-")
                    if len(v) >= 12 and v[:12].isdigit():
                        dt_key = k; break
            for r in rows:
                raw_dt = str(r.get(dt_key,"")).strip() if dt_key else None
                if not raw_dt:
                    raw_dt = str(_f(r, ["cntr_tm","dt","date","stck_cntg_hour","체결시간",
                                        "trd_dt","trde_dt","cntr_time","cntg_hour",
                                        "bas_dt","base_dt","stck_bsop_date"]) or "")
                dt = _pdt_min(raw_dt)
                if not dt: continue
                o = _int(_f(r, ["open_pric","open","stck_oprc","opn_prc"]), 0)
                h = _int(_f(r, ["high_pric","high","stck_hgpr","hgh_prc"]), 0)
                l = _int(_f(r, ["low_pric","low","stck_lwpr","low_prc"]), 0)
                c = _int(_f(r, ["cur_prc","close_pric","close","stck_clpr","cur_pric"]), 0)
                v = _int(_f(r, ["trde_qty","volume","acml_vol","acc_trde_qty","acc_vol"]), 0)
                if c <= 0: continue
                if o <= 0: o = c
                if h <= 0: h = max(o, c)
                if l <= 0: l = min(o, c)
                dd[dt] = {"dt": dt, "open": o, "high": h, "low": l,
                          "close": c, "volume": max(0, v)}
            out = sorted(dd.values(), key=lambda x: x["dt"])
            if out: return out
            last_err = f"dt파싱실패 — dt_key={dt_key}, 샘플키={all_keys[:10]}"
        except Exception as e:
            last_err = str(e)
            continue
    raise RuntimeError(f"5분봉 조회 실패 — {last_err}")

def _fetch_min(token, tk, mp=40):
    """캐시 + 증분 업데이트"""
    today = _today()
    cached = _load_c(tk)
    if cached:
        last_dt = cached[-1]["dt"][:10]  # 날짜 부분
        if last_dt >= today:
            return cached
        # 증분: 최근 5페이지만 새로 받아 병합
        try:
            fresh = _fetch_raw_min(token, tk, mp=min(mp, 5))
            m = {d["dt"]: d for d in cached}
            for d in fresh: m[d["dt"]] = d
            out = sorted(m.values(), key=lambda x: x["dt"])
            _save_c(tk, out); return out
        except:
            return cached
    fresh = _fetch_raw_min(token, tk, mp=mp)
    _save_c(tk, fresh); return fresh

# ── MA 계산 ───────────────────────────────────────────────────
def _ma(vals, w):
    out = [None] * len(vals); s = 0.0
    for i, v in enumerate(vals):
        s += float(v)
        if i >= w: s -= float(vals[i - w])
        if i >= w - 1: out[i] = s / float(w)
    return out

# ── 기간 필터 ─────────────────────────────────────────────────
def _filter_bars(bars, d_from, d_to):
    """dt 기준 기간 필터 (분봉 dt는 datetime 문자열)"""
    result = bars
    if d_from:
        result = [b for b in result if b["dt"][:10] >= d_from]
    if d_to:
        result = [b for b in result if b["dt"][:10] <= d_to]
    return result

# ── 백테스트 ─────────────────────────────────────────────────
def _backtest(bars):
    """
    매수: 종가 > MA20 AND 전봉 종가 <= MA20
    매도: 종가 < MA20
    1포지션, 100만원 고정
    """
    n = len(bars)
    if n < 22: return [], []

    closes = [b["close"] for b in bars]
    mv = _ma(closes, 20)

    trades = []   # 완결된 매매
    tlog  = []    # 액션 로그

    in_pos       = False
    buy_dt       = ""; buy_p = 0; buy_qty = 0; buy_cost = 0.0
    pending_buy  = False   # 다음봉 시가에 매수 대기
    pending_sell = False   # 다음봉 시가에 매도 대기

    for i in range(1, n):
        b = bars[i]; c = b["close"]; o = b["open"]; dt = b["dt"]
        ma = mv[i]; ma_prev = mv[i-1]
        c_prev = closes[i-1]

        if ma is None or ma_prev is None: continue

        # ── 대기 중인 매수 실행 (이전봉 돌파 확인 → 이번봉 시가 매수) ──
        if pending_buy and not in_pos:
            buy_p   = o  # 이번봉 시가
            buy_qty = int(INVEST / buy_p) if buy_p > 0 else 0
            if buy_qty > 0:
                buy_cost = buy_p * buy_qty
                buy_dt = dt; in_pos = True
                tlog.append({"action":"매수","dt":dt,"price":buy_p,"qty":buy_qty})
            pending_buy = False

        # ── 대기 중인 매도 실행 (이전봉 이탈 확인 → 이번봉 시가 매도) ──
        if pending_sell and in_pos:
            sell_p = o  # 이번봉 시가
            sa = sell_p * buy_qty
            pnl = sa - buy_cost
            roi = (sell_p / buy_p - 1) * 100 if buy_p else 0
            trades.append({
                "buy_dt": buy_dt, "buy_price": buy_p,
                "sell_dt": dt, "sell_price": sell_p,
                "qty": buy_qty, "buy_cost": buy_cost,
                "sell_amount": sa, "pnl": pnl, "roi_pct": roi,
            })
            tlog.append({"action":"매도","dt":dt,"price":sell_p,"qty":buy_qty})
            in_pos = False; buy_dt=""; buy_p=0; buy_qty=0; buy_cost=0.0
            pending_sell = False

        # ── 이번봉 종가 기준 다음봉 신호 판단 ──
        if not in_pos and not pending_buy:
            # 매수 신호: 전봉 종가 <= MA20 이었다가 현재봉 종가 > MA20 돌파
            if c > ma and c_prev <= (mv[i-1] or 0):
                pending_buy = True  # 다음봉 시가에 매수
        elif in_pos and not pending_sell:
            # 매도 신호: 현재봉 종가 < MA20 이탈
            if c < ma:
                pending_sell = True  # 다음봉 시가에 매도

    # 미결 포지션 (데이터 끝까지 보유중)
    if in_pos and buy_qty > 0:
        last = bars[-1]; sell_p = last["close"]; sa = sell_p * buy_qty
        pnl = sa - buy_cost; roi = (sell_p / buy_p - 1) * 100 if buy_p else 0
        trades.append({
            "buy_dt": buy_dt, "buy_price": buy_p,
            "sell_dt": last["dt"], "sell_price": sell_p,
            "qty": buy_qty, "buy_cost": buy_cost,
            "sell_amount": sa, "pnl": pnl, "roi_pct": roi,
            "open": True,
        })

    return trades, tlog

# ── 통계 ──────────────────────────────────────────────────────
def _stats(trades):
    if not trades:
        return {"n":0,"w":0,"l":0,"wr":0,"ar":0,"tp":0,"tl":0,"pnl":0}
    cl = [t for t in trades if not t.get("open")]
    w  = [t for t in cl if t["pnl"] > 0]
    lo = [t for t in cl if t["pnl"] <= 0]
    pr = sum(t["pnl"] for t in cl if t["pnl"] > 0)
    ls = sum(t["pnl"] for t in cl if t["pnl"] <= 0)
    return {
        "n": len(trades), "w": len(w), "l": len(lo),
        "wr": (len(w)/len(cl)*100) if cl else 0,
        "ar": (sum(t["roi_pct"] for t in cl)/len(cl)) if cl else 0,
        "tp": pr, "tl": ls, "pnl": pr+ls,
    }

def _trade_df(trades):
    rows = []
    for i, t in enumerate(trades):
        status = "보유중" if t.get("open") else "매도완료"
        rows.append({
            "No": i+1,
            "매수일시": t["buy_dt"],
            "매수가": f"{t['buy_price']:,.0f}",
            "수량": t["qty"],
            "매수금액": f"{t['buy_cost']:,.0f}",
            "매도일시": t["sell_dt"],
            "매도가": f"{t['sell_price']:,.0f}",
            "매도금액": f"{t['sell_amount']:,.0f}",
            "손익금": f"{t['pnl']:+,.0f}",
            "수익률": f"{t['roi_pct']:+.2f}%",
            "상태": status,
        })
    return pd.DataFrame(rows)

# ── 차트 ──────────────────────────────────────────────────────
def _chart(bars, trades, tlog, nm, d_from=None, d_to=None):
    closes = [b["close"] for b in bars]
    mv = _ma(closes, 20)
    df = pd.DataFrame([{**b, "ma20": mv[i]} for i, b in enumerate(bars)])
    df["dt"] = pd.to_datetime(df["dt"])

    if df.empty:
        fig = go.Figure(); fig.add_annotation(text="데이터 없음", showarrow=False)
        return fig

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                        vertical_spacing=0.02, row_heights=[0.75, 0.25])

    # 캔들
    fig.add_trace(go.Candlestick(
        x=df["dt"], open=df["open"], high=df["high"],
        low=df["low"], close=df["close"],
        increasing=dict(line=dict(color=COLORS["bull"]), fillcolor=COLORS["bull"]),
        decreasing=dict(line=dict(color=COLORS["bear"]), fillcolor=COLORS["bear"]),
        name="5분봉"), row=1, col=1)

    # MA20
    fig.add_trace(go.Scatter(
        x=df["dt"], y=df["ma20"], name="MA20",
        line=dict(color=COLORS["ma20"], width=1.5),
        hoverinfo="skip"), row=1, col=1)

    # 거래량
    bull_mask = df["close"] >= df["open"]
    vc = [COLORS["bv"] if b else COLORS["bev"] for b in bull_mask]
    fig.add_trace(go.Bar(
        x=df["dt"], y=df["volume"], name="거래량",
        marker_color=vc, opacity=0.55), row=2, col=1)

    # 매수/매도 마커
    dt_set = set(df["dt"])
    for e in tlog:
        edt = pd.to_datetime(e["dt"]); act = e["action"]; price = e["price"]
        if act == "매수":
            fig.add_trace(go.Scatter(
                x=[edt], y=[price * 0.97], mode="markers", showlegend=False,
                marker=dict(symbol="triangle-up", size=12, color=COLORS["buy"]),
                hovertemplate=f"%{{x}}<br>매수<br>{price:,.0f}<extra></extra>"),
                row=1, col=1)
        elif act == "매도":
            fig.add_trace(go.Scatter(
                x=[edt], y=[price * 1.03], mode="markers", showlegend=False,
                marker=dict(symbol="triangle-down", size=12, color=COLORS["sell"]),
                hovertemplate=f"%{{x}}<br>매도<br>{price:,.0f}<extra></extra>"),
                row=1, col=1)

    period_str = ""
    if d_from: period_str += f" ({d_from}"
    if d_to:   period_str += f" ~ {d_to})"
    elif d_from: period_str += " ~ 현재)"

    fig.update_layout(
        title=f"📊 {nm} — 5분봉 MA20 돌파매매{period_str}",
        template="plotly_dark", height=660,
        paper_bgcolor=COLORS["bg"], plot_bgcolor=COLORS["bg"],
        font=dict(color=COLORS["txt"]),
        xaxis_rangeslider_visible=False, showlegend=True,
        legend=dict(orientation="h", y=1.06, x=0, font=dict(size=9)),
        margin=dict(l=50, r=20, t=80, b=10))

    for rn in (1, 2):
        fig.update_yaxes(row=rn, col=1, gridcolor=COLORS["grid"], zeroline=False)
        fig.update_xaxes(row=rn, col=1, gridcolor=COLORS["grid"],
                         zeroline=False, showgrid=False)
    fig.update_xaxes(row=2, col=1, tickformat="%m/%d %H:%M")
    return fig

# ── 결과 표시 ─────────────────────────────────────────────────
def _show(nm, tk, bars, trades, tlog, d_from, d_to, ksuf=""):
    s = _stats(trades)
    c1,c2,c3,c4,c5,c6 = st.columns(6)
    c1.metric("매매횟수", f"{s['n']}건")
    c2.metric("수익", f"{s['w']}건")
    c3.metric("손실", f"{s['l']}건")
    c4.metric("승률", f"{s['wr']:.0f}%")
    c5.metric("평균수익률", f"{s['ar']:+.2f}%")
    c6.metric("분석봉수", f"{len(bars):,}개")
    c7,c8,c9 = st.columns(3)
    c7.metric("수익금", f"{s['tp']:+,.0f}원")
    c8.metric("손실금", f"{s['tl']:,.0f}원")
    ic = "🟢" if s["pnl"] >= 0 else "🔴"
    c9.metric(f"{ic} 순손익", f"{s['pnl']:+,.0f}원")

    if any(t.get("open") for t in trades):
        st.info("📌 보유중 포지션 있음")

    fig = _chart(bars, trades, tlog, nm, d_from, d_to)
    st.plotly_chart(fig, use_container_width=True, key=f"ma20c{ksuf}",
        config={"displayModeBar":True,"displaylogo":False,"scrollZoom":True,
                "modeBarButtonsToRemove":["lasso2d","select2d","autoScale2d"]})

    if trades:
        st.markdown("#### 📋 트레이드 상세")
        st.dataframe(_trade_df(trades), use_container_width=True,
                     hide_index=True, key=f"ma20t{ksuf}")
        csv = _trade_df(trades).to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
        st.download_button("📥 CSV", csv, f"{nm}_ma20_5m.csv",
                           "text/csv", key=f"ma20d{ksuf}")
    else:
        st.info("매매 미발생 (데이터 부족 또는 조건 미충족)")

# ── 메인 ──────────────────────────────────────────────────────
def main():
    st.set_page_config(page_title="MA20 돌파 시뮬레이터",
                       page_icon="📈", layout="wide",
                       initial_sidebar_state="expanded")
    st.markdown(_CSS, unsafe_allow_html=True)

    with st.sidebar:
        st.markdown("## ⚙️ MA20 돌파 시뮬레이터")
        st.markdown("##### 5분봉 기준")

        ti = st.text_input("종목명/코드", placeholder="삼성전자")

        st.markdown("---")
        st.markdown("##### 📅 분석 기간")
        period_mode = st.radio("기간 설정", ["기간 선택","날짜 직접 입력"],
                               horizontal=True)
        d_from = d_to = None
        if period_mode == "기간 선택":
            period_opt = st.selectbox("기간",
                ["1주","2주","1개월","2개월","3개월","전체"], index=2)
            days_map = {"1주":7,"2주":14,"1개월":30,"2개월":60,"3개월":90,"전체":0}
            dd = days_map[period_opt]
            if dd > 0:
                d_from = (datetime.now()-timedelta(days=dd)).strftime("%Y-%m-%d")
        else:
            col_f, col_t = st.columns(2)
            cf = col_f.date_input("시작일", value=date_.today()-timedelta(days=30),
                                   min_value=date_(2020,1,1), max_value=date_.today())
            ct = col_t.date_input("종료일", value=date_.today(),
                                   min_value=date_(2020,1,1), max_value=date_.today())
            d_from = str(cf); d_to = str(ct)

        max_pages = st.slider("데이터 페이지수", 5, 100, 40, 5,
                              help="페이지당 약 900봉 (5분봉 1일 ≈ 78봉)")

        st.markdown("---")
        st.markdown(f"""<div style="font-size:.72rem;color:#888;line-height:1.9">
        <b style="color:{COLORS['ma20']}">━</b> MA20<br>
        <b style="color:{COLORS['buy']}">▲</b> 매수 (종가 > MA20, 전봉 ≤ MA20)<br>
        <b style="color:{COLORS['sell']}">▼</b> 매도 (종가 < MA20)<br><br>
        <b>투자금</b>: 1회 {INVEST:,}원<br>
        <b>포지션</b>: 최대 1개 유지
        </div>""", unsafe_allow_html=True)

        run = st.button("🔍 분석 실행", use_container_width=True, type="primary")

    # session state
    for k in ["ma20r","ma20_from","ma20_to"]:
        if k not in st.session_state: st.session_state[k] = None

    if run:
        if not ti: st.warning("종목명 입력 필요"); return
        try:
            token = core.get_token(core.APP_KEY, core.APP_SECRET)
            tk, nm = _resolve(ti)
            with st.spinner(f"5분봉 데이터 로딩 중... ({nm})"):
                all_bars = _fetch_min(token, tk, mp=max_pages)

            if not all_bars:
                st.error("데이터 없음 — API 응답을 확인하세요"); return

            bars = _filter_bars(all_bars, d_from, d_to)
            if len(bars) < 22:
                st.warning(f"데이터 부족: {len(bars)}봉 (MA20 계산 최소 22봉 필요)"); return

            st.markdown(f"### {nm} ({tk})")
            st.caption(f"전체 {len(all_bars):,}봉 중 기간 필터 후 {len(bars):,}봉 분석")

            trades, tlog = _backtest(bars)
            _show(nm, tk, bars, trades, tlog, d_from, d_to, ksuf=tk)

            st.session_state.ma20r = {
                "nm":nm,"tk":tk,"bars":bars,"trades":trades,"tlog":tlog}
            st.session_state.ma20_from = d_from
            st.session_state.ma20_to   = d_to

        except Exception as e:
            st.error(f"오류: {e}"); st.exception(e)

    else:
        if st.session_state.ma20r:
            r = st.session_state.ma20r
            st.markdown(f"### {r['nm']} ({r['tk']})")
            _show(r["nm"], r["tk"], r["bars"], r["trades"], r["tlog"],
                  st.session_state.ma20_from, st.session_state.ma20_to,
                  ksuf=r["tk"]+"_r")
        else:
            st.info("👈 종목 입력 후 **분석 실행**을 누르세요.")
            with st.expander("💡 매매 로직", expanded=True):
                st.markdown(f"""
**[데이터]** 키움 REST API `ka10080` — 5분봉

**[매수 조건]** 골든크로스 확인 후 다음봉 시가 매수
- 현재봉 종가 > MA20 AND 전봉 종가 ≤ MA20 → 돌파 확인
- **다음 5분봉 시가**에 매수 체결

**[매도 조건]** 이탈 확인 후 다음봉 시가 매도
- 현재봉 종가 < MA20 → 이탈 확인
- **다음 5분봉 시가**에 매도 체결

**[금액]** 1회 매수 {INVEST:,}원 고정 / 1포지션만 유지

**[주의]** 키움 분봉 데이터는 최대 약 1년치 제공
                """)

if __name__ == "__main__": main()