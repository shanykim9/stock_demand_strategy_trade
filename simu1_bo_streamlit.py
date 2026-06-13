"""
신고가 돌파 매매 시뮬레이션 — simul1_bo_stream.py
═══════════════════════════════════════════════════
- 트리거: ★신고가(설정기간) + 거래량 폭증 + 양봉 + MA상승추세
- 2단계 분할 매수 → 손절 / 2단계 익절 시뮬레이션
- 단일 종목 + 다종목 파일 업로드 지원
- 키움 REST API (ka10081) 일봉 OHLCV 기반

실행: streamlit run simul1_bo_stream.py
"""

from __future__ import annotations

import os, math
from datetime import datetime, timedelta
from typing import Optional

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

import demand as core

# ═══════════════════════════════════════════════════════
#  색상 팔레트
# ═══════════════════════════════════════════════════════
COLORS = {
    "bull": "#E53935", "bear": "#1E88E5",
    "bull_vol": "#EF9A9A", "bear_vol": "#90CAF9",
    "trigger": "#FFD600", "trigger_vol": "#FFD600",
    "buy1": "#00E676", "buy2": "#76FF03",
    "sell_profit": "#2196F3", "sell_trail": "#00BCD4",
    "sell_loss": "#F44336", "sell_hold": "#9E9E9E",
    "ma20": "#FFA726", "bg": "#131722", "grid": "#1E222D", "text": "#D1D4DC",
}
_MA_COLORS = {60: "#AB47BC", 120: "#26A69A", 180: "#42A5F5"}
_MA_DASH = {60: "dash", 120: "dashdot", 180: "longdash"}

PERIOD_OPTIONS = ["6개월", "1년", "1년 6개월", "2년", "2년 6개월", "3년", "4년", "5년", "전체"]
PERIOD_MAP = {
    "6개월": 180, "1년": 365, "1년 6개월": 548, "2년": 730,
    "2년 6개월": 913, "3년": 1095, "4년": 1460, "5년": 1825, "전체": 0,
}
NH_PERIOD_OPTIONS = ["6개월", "1년", "1년 6개월", "2년", "2년 6개월", "3년", "4년", "5년"]
NH_PERIOD_MAP = {
    "6개월": 180, "1년": 365, "1년 6개월": 548, "2년": 730,
    "2년 6개월": 913, "3년": 1095, "4년": 1460, "5년": 1825,
}

# ═══════════════════════════════════════════════════════
#  유틸
# ═══════════════════════════════════════════════════════

def _to_int(v, default=0) -> int:
    if v is None: return default
    if isinstance(v, int): return v
    s = str(v).strip().replace(",", "")
    if not s: return default
    try: return int(float(s))
    except: return default

def _parse_dt_any(v) -> str | None:
    if v is None: return None
    s = str(v).strip()
    if len(s) >= 8 and s[:8].isdigit(): return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    if len(s) >= 10 and s[4] == "-" and s[7] == "-": return s[:10]
    return None

def _first_non_empty(row: dict, keys: list[str]):
    for k in keys:
        if k in row and str(row.get(k)).strip() != "": return row.get(k)
    return None

def _resolve_ticker_and_name(query: str) -> tuple[str, str]:
    q = (query or "").strip()
    if not q: raise RuntimeError("종목명을 입력해 주세요.")
    if q.isdigit() and len(q) == 6:
        ticker = q
    else:
        ticker, err = core.resolve_ticker(q)
        if err or not ticker: raise RuntimeError(err or f"'{q}' 종목명을 확인해 주세요.")
    name = ticker
    try: name = (core._krx_cache.get("name_by_code") or {}).get(ticker, ticker)
    except: pass
    return ticker, name

def _parse_ticker_file(content: str) -> list[str]:
    tickers = []
    for part in content.replace("\n", ",").replace("\r", ",").split(","):
        t = part.strip()
        if t: tickers.append(t)
    return tickers

# ═══════════════════════════════════════════════════════
#  데이터 조회 — OHLCV
# ═══════════════════════════════════════════════════════

def _fetch_daily_ohlcv(token: str, ticker: str, max_pages: int = 40) -> list[dict]:
    end_dt = datetime.now(core.TZ).strftime("%Y%m%d")
    stex_tp = (os.getenv("KIWOOM_DMST_STEX_TP") or "KRX").strip().upper()
    upd_stkpc_tp = (os.getenv("KIWOOM_OHLCV_UPD_STKPC_TP") or "1").strip()
    common = {"stk_cd": ticker, "stex_tp": stex_tp, "dmst_stex_tp": stex_tp}
    bodies = [
        {**common, "base_dt": end_dt, "upd_stkpc_tp": upd_stkpc_tp},
        {**common, "base_dt": end_dt},
        {**common, "dt": end_dt, "upd_stkpc_tp": upd_stkpc_tp},
        {**common, "dt": end_dt},
    ]
    last_err = None
    for body in bodies:
        try:
            res = core.call_tr_all_pages(
                token=token, api_id="ka10081", body=body,
                endpoint="/api/dostk/chart", max_pages=max_pages,
            )
            rows = res.get("rows") or []
            if not rows: continue
            dedup: dict[str, dict] = {}
            for r in rows:
                dt = _parse_dt_any(_first_non_empty(r, ["dt","date","bas_dt","base_dt","trde_dt","trd_dt"]))
                if not dt: continue
                op = _to_int(_first_non_empty(r, ["open_pric","open","stck_oprc","opn_prc"]), 0)
                hp = _to_int(_first_non_empty(r, ["high_pric","high","stck_hgpr","hgh_prc"]), 0)
                lp = _to_int(_first_non_empty(r, ["low_pric","low","stck_lwpr","low_prc"]), 0)
                cp = _to_int(_first_non_empty(r, ["close_pric","close","stck_clpr","cur_prc","cur_pric"]), 0)
                vol = _to_int(_first_non_empty(r, ["trde_qty","volume","acml_vol","acc_trde_qty"]), 0)
                if cp <= 0: continue
                if op <= 0: op = cp
                if hp <= 0: hp = max(op, cp)
                if lp <= 0: lp = min(op, cp)
                dedup[dt] = {"dt": dt, "open": op, "high": hp, "low": lp, "close": cp, "volume": max(0, vol)}
            out = sorted(dedup.values(), key=lambda x: x["dt"])
            if out: return out
        except Exception as e:
            last_err = e; continue
    raise RuntimeError(f"일봉 데이터 조회 실패: {last_err}")

# ═══════════════════════════════════════════════════════
#  기술지표
# ═══════════════════════════════════════════════════════

def _rolling_ma(values: list[int], window: int) -> list[Optional[float]]:
    out: list[Optional[float]] = [None] * len(values)
    if window <= 0: return out
    run_sum = 0.0
    for i, v in enumerate(values):
        run_sum += float(v)
        if i >= window: run_sum -= float(values[i - window])
        if i >= window - 1: out[i] = run_sum / float(window)
    return out

def _linear_regression_slope(y: list[float]) -> float:
    n = len(y)
    if n < 2: return 0.0
    x_mean = (n - 1) / 2.0
    y_mean = sum(y) / n
    num = sum((i - x_mean) * (yi - y_mean) for i, yi in enumerate(y))
    den = sum((i - x_mean) ** 2 for i in range(n))
    return num / den if den != 0 else 0.0

def _check_ma_uptrend(ma_values: list, i: int, trend_window: int) -> bool:
    if trend_window < 2 or i - trend_window + 1 < 0: return False
    window = ma_values[i - trend_window + 1: i + 1]
    if not all(v is not None for v in window): return False
    return _linear_regression_slope([float(v) for v in window]) > 0

# ═══════════════════════════════════════════════════════
#  트리거 탐지: 신고가 + 거래량 폭증 + 양봉 + MA상승추세
# ═══════════════════════════════════════════════════════

def _find_triggers(
    days: list[dict],
    ma_configs: dict[int, int],
    volume_multiplier: float,
    newhigh_lookback_days: int,
) -> tuple[list[dict], list[dict]]:
    """
    Returns: (trigger_rows, enriched)
    """
    closes = [int(x["close"]) for x in days]
    opens = [int(x["open"]) for x in days]
    vols = [int(x["volume"]) for x in days]
    dates = [x["dt"] for x in days]
    ma20 = _rolling_ma(closes, 20)

    ma_lines: dict[int, list[Optional[float]]] = {}
    for period in ma_configs:
        ma_lines[period] = _rolling_ma(closes, period)

    trigger_rows: list[dict] = []
    enriched: list[dict] = []

    # ── 신고가 판정용 슬라이딩 윈도우 최댓값 (O(n) 최적화) ──
    from collections import deque
    # lookback 기간을 거래일 수로 환산 (달력일 → 거래일 약 70%)
    # 정확한 날짜 비교 대신 인덱스 기반으로 처리
    # 날짜→인덱스 매핑을 사전 구축
    date_indices: dict[str, int] = {d["dt"]: idx for idx, d in enumerate(days)}

    for i in range(len(days)):
        d = days[i]
        row = {
            "dt": d["dt"], "open": d["open"], "high": d["high"],
            "low": d["low"], "close": d["close"], "volume": d["volume"],
            "ma20": ma20[i], "signal": "",
        }
        for period, ma_vals in ma_lines.items():
            row[f"ma{period}"] = ma_vals[i]

        if i >= 1:
            cond_bull = closes[i] >= opens[i]

            prev_vol = vols[i - 1]
            curr_vol = vols[i]
            cond_volume = prev_vol > 0 and curr_vol >= prev_vol * volume_multiplier

            cond_all_ma_up = all(
                _check_ma_uptrend(ma_lines[p], i, tw) for p, tw in ma_configs.items()
            )

            # ★ 신고가: 설정 기간 내 최고 종가 돌파 (종가 기준)
            cond_newhigh = False
            prev_high = 0
            if newhigh_lookback_days > 0:
                lb_start = (
                    datetime.strptime(d["dt"], "%Y-%m-%d") - timedelta(days=newhigh_lookback_days)
                ).strftime("%Y-%m-%d")
                # lookback 시작 인덱스 찾기 (이진 탐색 대신 선형 → 캐시로 최적화)
                start_j = 0
                for j in range(i - 1, -1, -1):
                    if dates[j] < lb_start:
                        start_j = j + 1
                        break
                # 구간 내 최대값
                if start_j < i:
                    prev_high = max(closes[start_j:i])
                if prev_high > 0 and closes[i] > prev_high:
                    cond_newhigh = True

            if cond_newhigh and cond_volume and cond_bull and cond_all_ma_up:
                row["signal"] = "trigger"
                trigger_rows.append({
                    "dt": d["dt"],
                    "open": opens[i], "high": d["high"],
                    "low": d["low"], "close": closes[i],
                    "volume": curr_vol,
                    "prev_high": prev_high,
                    "vol_ratio": curr_vol / float(prev_vol),
                })

        enriched.append(row)

    return trigger_rows, enriched


# ═══════════════════════════════════════════════════════
#  시뮬레이션 엔진
# ═══════════════════════════════════════════════════════

def _run_simulation(
    days: list[dict],
    trigger_rows: list[dict],
    amount_per_tranche: int = 1_000_000,
    take_profit_pct: float = 20.0,
    trailing_stop_pct: float = 10.0,
) -> tuple[list[dict], list[dict]]:
    """
    상태머신: IDLE → WATCHING → HOLDING → TRAILING → IDLE
    """
    trigger_map = {r["dt"]: r for r in trigger_rows}
    if not trigger_map:
        return [], []

    trades: list[dict] = []
    trade_log: list[dict] = []

    state = "IDLE"
    buy_prices = [0.0, 0.0]
    trigger_low = 0.0
    trigger_close_val = 0
    trigger_dt = ""
    pending = [True, True]
    holdings: list[dict] = []

    trail_qty = 0
    trail_cost = 0.0
    trail_peak = 0.0
    first_sell_price = 0.0

    def _reset():
        nonlocal state, holdings, pending, trail_qty, trail_cost, trail_peak, first_sell_price
        state = "IDLE"; holdings = []; pending = [True, True]
        trail_qty = 0; trail_cost = 0.0; trail_peak = 0.0; first_sell_price = 0.0

    for i, d in enumerate(days):
        dt = d["dt"]
        o, h, l, c = d["open"], d["high"], d["low"], d["close"]

        # ══ IDLE ══
        if state == "IDLE":
            if dt in trigger_map:
                td = trigger_map[dt]
                t_o, t_c, t_l = td["open"], td["close"], td["low"]
                buy_prices[0] = (t_o + t_c) / 2.0
                buy_prices[1] = t_o + (t_c - t_o) / 4.0
                trigger_low = float(t_l)
                trigger_close_val = t_c
                trigger_dt = dt
                pending = [True, True]
                holdings = []
                state = "WATCHING"
            continue

        # ══ WATCHING ══
        if state == "WATCHING":
            for idx in range(2):
                if pending[idx] and l <= buy_prices[idx]:
                    bp = buy_prices[idx]
                    qty = int(amount_per_tranche / bp) if bp > 0 else 0
                    if qty > 0:
                        holdings.append({"tranche": idx+1, "price": bp, "qty": qty, "amount": bp*qty, "dt": dt})
                        trade_log.append({"trigger_dt": trigger_dt, "action": "매수",
                                          "tranche": idx+1, "dt": dt, "price": bp, "qty": qty, "amount": bp*qty})
                    pending[idx] = False

            total_qty = sum(x["qty"] for x in holdings)

            if total_qty == 0:
                if c > trigger_close_val:
                    _reset()
                elif dt in trigger_map and dt != trigger_dt:
                    td = trigger_map[dt]
                    t_o, t_c, t_l = td["open"], td["close"], td["low"]
                    buy_prices[0] = (t_o + t_c) / 2.0
                    buy_prices[1] = t_o + (t_c - t_o) / 4.0
                    trigger_low = float(t_l)
                    trigger_close_val = t_c
                    trigger_dt = dt
                    pending = [True, True]; holdings = []
                continue

            state = "HOLDING"
            # fall through

        # ══ HOLDING ══
        if state == "HOLDING":
            for idx in range(2):
                if pending[idx] and l <= buy_prices[idx]:
                    bp = buy_prices[idx]
                    qty = int(amount_per_tranche / bp) if bp > 0 else 0
                    if qty > 0:
                        holdings.append({"tranche": idx+1, "price": bp, "qty": qty, "amount": bp*qty, "dt": dt})
                        trade_log.append({"trigger_dt": trigger_dt, "action": "매수",
                                          "tranche": idx+1, "dt": dt, "price": bp, "qty": qty, "amount": bp*qty})
                    pending[idx] = False

            total_qty = sum(x["qty"] for x in holdings)
            total_cost = sum(x["price"] * x["qty"] for x in holdings)
            avg_price = total_cost / total_qty if total_qty else 0
            target_price = avg_price * (1.0 + take_profit_pct / 100.0)

            # 손절
            if l <= trigger_low:
                sell_price = trigger_low
                sell_amount = sell_price * total_qty
                pnl = sell_amount - total_cost
                trades.append({
                    "trigger_dt": trigger_dt, "buys": list(holdings),
                    "sell_dt": dt, "sell_price": sell_price,
                    "sell_type": "손절", "total_qty": total_qty,
                    "total_invested": total_cost, "total_returned": sell_amount,
                    "pnl": pnl, "roi_pct": (pnl/total_cost*100) if total_cost else 0,
                    "avg_price": avg_price, "trigger_low": trigger_low,
                    "trail_sell_dt": None, "trail_sell_price": None, "trail_pnl": None, "trail_qty": 0,
                })
                trade_log.append({"trigger_dt": trigger_dt, "action": "손절", "tranche": 0,
                                  "dt": dt, "price": sell_price, "qty": total_qty, "amount": sell_amount})
                _reset(); continue

            # 익절 1단계
            if h >= target_price:
                sell_qty = total_qty // 2
                remain_qty = total_qty - sell_qty
                sell_amount_1 = target_price * sell_qty

                trade_log.append({"trigger_dt": trigger_dt, "action": "익절(50%)", "tranche": 0,
                                  "dt": dt, "price": target_price, "qty": sell_qty, "amount": sell_amount_1})

                if remain_qty <= 0:
                    pnl = sell_amount_1 - total_cost
                    trades.append({
                        "trigger_dt": trigger_dt, "buys": list(holdings),
                        "sell_dt": dt, "sell_price": target_price,
                        "sell_type": "익절", "total_qty": total_qty,
                        "total_invested": total_cost, "total_returned": sell_amount_1,
                        "pnl": pnl, "roi_pct": (pnl/total_cost*100) if total_cost else 0,
                        "avg_price": avg_price, "trigger_low": trigger_low,
                        "trail_sell_dt": None, "trail_sell_price": None, "trail_pnl": None, "trail_qty": 0,
                    })
                    _reset(); continue

                trail_qty = remain_qty
                trail_cost = avg_price * remain_qty
                trail_peak = h
                first_sell_price = target_price
                state = "TRAILING"
                continue

        # ══ TRAILING ══
        if state == "TRAILING":
            if h > trail_peak:
                trail_peak = h

            trail_stop_price = trail_peak * (1.0 - trailing_stop_pct / 100.0)

            if l <= trail_stop_price:
                sell_price_2 = trail_stop_price
                sell_amount_2 = sell_price_2 * trail_qty
                trail_pnl = sell_amount_2 - trail_cost

                total_qty_all = sum(x["qty"] for x in holdings)
                total_cost_all = sum(x["price"] * x["qty"] for x in holdings)
                sell_qty_1 = total_qty_all - trail_qty
                sell_amount_1 = first_sell_price * sell_qty_1
                total_returned = sell_amount_1 + sell_amount_2
                total_pnl = total_returned - total_cost_all

                trades.append({
                    "trigger_dt": trigger_dt, "buys": list(holdings),
                    "sell_dt": dt, "sell_price": first_sell_price,
                    "sell_type": "익절+추적", "total_qty": total_qty_all,
                    "total_invested": total_cost_all, "total_returned": total_returned,
                    "pnl": total_pnl, "roi_pct": (total_pnl/total_cost_all*100) if total_cost_all else 0,
                    "avg_price": total_cost_all/total_qty_all if total_qty_all else 0,
                    "trigger_low": trigger_low,
                    "trail_sell_dt": dt, "trail_sell_price": sell_price_2,
                    "trail_pnl": trail_pnl, "trail_qty": trail_qty,
                })
                trade_log.append({"trigger_dt": trigger_dt, "action": "추적매도", "tranche": 0,
                                  "dt": dt, "price": sell_price_2, "qty": trail_qty, "amount": sell_amount_2})
                _reset(); continue

    # 미결제
    if holdings and state in ("HOLDING", "WATCHING"):
        last = days[-1]
        tq = sum(x["qty"] for x in holdings)
        tc = sum(x["price"]*x["qty"] for x in holdings)
        ap = tc/tq if tq else 0
        sa = last["close"]*tq
        pnl = sa - tc
        trades.append({
            "trigger_dt": trigger_dt, "buys": list(holdings),
            "sell_dt": last["dt"], "sell_price": last["close"],
            "sell_type": "보유중", "total_qty": tq,
            "total_invested": tc, "total_returned": sa,
            "pnl": pnl, "roi_pct": (pnl/tc*100) if tc else 0,
            "avg_price": ap, "trigger_low": trigger_low,
            "trail_sell_dt": None, "trail_sell_price": None, "trail_pnl": None, "trail_qty": 0,
        })
    elif state == "TRAILING":
        last = days[-1]
        tq_all = sum(x["qty"] for x in holdings)
        tc_all = sum(x["price"]*x["qty"] for x in holdings)
        sq1 = tq_all - trail_qty
        sa1 = first_sell_price * sq1
        sa2 = last["close"] * trail_qty
        tr = sa1 + sa2; tp = tr - tc_all
        trades.append({
            "trigger_dt": trigger_dt, "buys": list(holdings),
            "sell_dt": last["dt"], "sell_price": first_sell_price,
            "sell_type": "익절+보유중", "total_qty": tq_all,
            "total_invested": tc_all, "total_returned": tr,
            "pnl": tp, "roi_pct": (tp/tc_all*100) if tc_all else 0,
            "avg_price": tc_all/tq_all if tq_all else 0, "trigger_low": trigger_low,
            "trail_sell_dt": last["dt"], "trail_sell_price": last["close"],
            "trail_pnl": last["close"]*trail_qty - trail_cost, "trail_qty": trail_qty,
        })

    return trades, trade_log


# ═══════════════════════════════════════════════════════
#  차트
# ═══════════════════════════════════════════════════════

def build_sim_chart(enriched, trigger_rows, trades, trade_log, name,
                    period_days=0, ma_configs=None):
    if not ma_configs: ma_configs = {60: 5}
    df = pd.DataFrame(enriched); df["dt"] = pd.to_datetime(df["dt"])
    if period_days > 0:
        df = df[df["dt"] >= datetime.now()-timedelta(days=period_days)].copy().reset_index(drop=True)
    else:
        df = df.copy().reset_index(drop=True)
    if df.empty:
        fig = go.Figure(); fig.add_annotation(text="데이터 없음", showarrow=False); return fig

    df["is_bull"] = df["close"] >= df["open"]
    trig_set = set(r["dt"] for r in trigger_rows)
    ds = df["dt"].dt.strftime("%Y-%m-%d")
    df["is_trig"] = ds.isin(trig_set)

    vc = [COLORS["trigger_vol"] if row["is_trig"]
          else (COLORS["bull_vol"] if row["is_bull"] else COLORS["bear_vol"])
          for _, row in df.iterrows()]

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                        vertical_spacing=0.02, row_heights=[0.75, 0.25])
    fig.add_trace(go.Candlestick(
        x=df["dt"], open=df["open"], high=df["high"], low=df["low"], close=df["close"],
        increasing=dict(line=dict(color=COLORS["bull"]), fillcolor=COLORS["bull"]),
        decreasing=dict(line=dict(color=COLORS["bear"]), fillcolor=COLORS["bear"]),
        name="일봉", hoverinfo="x+y"), row=1, col=1)
    fig.add_trace(go.Scatter(x=df["dt"], y=df["ma20"], name="MA20",
        line=dict(color=COLORS["ma20"], width=1.2, dash="dot"), hoverinfo="skip"), row=1, col=1)
    for p in sorted(ma_configs.keys()):
        cn = f"ma{p}"
        if cn not in df.columns: continue
        fig.add_trace(go.Scatter(x=df["dt"], y=df[cn], name=f"MA{p}",
            line=dict(color=_MA_COLORS.get(p,"#AB47BC"), width=1.2, dash=_MA_DASH.get(p,"dash")),
            hoverinfo="skip"), row=1, col=1)

    dt = df[df["is_trig"]]
    if not dt.empty:
        fig.add_trace(go.Scatter(x=dt["dt"], y=dt["high"]*1.04, mode="markers", name="★ 트리거",
            marker=dict(symbol="star", size=14, color=COLORS["trigger"], line=dict(width=1, color="#F9A825")),
            hovertemplate="%{x|%Y-%m-%d}<br><b>★ 신고가 트리거</b><extra></extra>"), row=1, col=1)

    for tr_n, color, label in [(1, COLORS["buy1"], "1차"), (2, COLORS["buy2"], "2차")]:
        entries = [e for e in trade_log if e["action"]=="매수" and e["tranche"]==tr_n]
        if entries:
            fig.add_trace(go.Scatter(
                x=pd.to_datetime([e["dt"] for e in entries]),
                y=[e["price"]*0.97 for e in entries], mode="markers", name=f"▲ {label}",
                marker=dict(symbol="triangle-up", size=12, color=color, line=dict(width=1, color="#004D40")),
                customdata=[e["price"] for e in entries],
                hovertemplate=f"%{{x|%Y-%m-%d}}<br><b>▲ {label}</b><br>%{{customdata:,.0f}}원<extra></extra>"),
                row=1, col=1)

    for st_, co, sy, lb in [
        ("손절", COLORS["sell_loss"], "triangle-down", "▼ 손절"),
        ("익절", COLORS["sell_profit"], "triangle-down", "▼ 익절"),
        ("익절+추적", COLORS["sell_profit"], "triangle-down", "▼ 1차익절"),
        ("보유중", COLORS["sell_hold"], "diamond", "◇ 보유중"),
        ("익절+보유중", COLORS["sell_hold"], "diamond", "◇ 익절+보유중")]:
        es = [t for t in trades if t["sell_type"]==st_]
        if es:
            fig.add_trace(go.Scatter(
                x=pd.to_datetime([e["sell_dt"] for e in es]),
                y=[e["sell_price"]*1.03 for e in es], mode="markers", name=lb,
                marker=dict(symbol=sy, size=13, color=co, line=dict(width=1.5, color="#FFF")),
                customdata=[e["sell_price"] for e in es],
                hovertemplate=f"%{{x|%Y-%m-%d}}<br><b>{lb}</b><br>%{{customdata:,.0f}}원<extra></extra>"),
                row=1, col=1)

    tl_e = [e for e in trade_log if e["action"]=="추적매도"]
    if tl_e:
        fig.add_trace(go.Scatter(
            x=pd.to_datetime([e["dt"] for e in tl_e]),
            y=[e["price"]*1.03 for e in tl_e], mode="markers", name="▼ 추적매도",
            marker=dict(symbol="triangle-down", size=13, color=COLORS["sell_trail"],
                        line=dict(width=1.5, color="#FFF")),
            customdata=[e["price"] for e in tl_e],
            hovertemplate="%{x|%Y-%m-%d}<br><b>▼ 추적매도</b><br>%{customdata:,.0f}원<extra></extra>"),
            row=1, col=1)

    fig.add_trace(go.Bar(x=df["dt"], y=df["volume"], name="거래량",
        marker_color=vc, marker_line_width=0,
        hovertemplate="%{x|%Y-%m-%d}<br>%{y:,.0f}<extra></extra>"), row=2, col=1)

    _m = "+".join(f"MA{p}" for p in sorted(ma_configs.keys()))
    fig.update_layout(height=750, margin=dict(l=0,r=0,t=80,b=0),
        paper_bgcolor=COLORS["bg"], plot_bgcolor=COLORS["bg"],
        font=dict(color=COLORS["text"], size=12),
        title=dict(text=f"  {name} — 매매 시뮬 ({_m})", font=dict(size=16, color="#FFF"),
                   x=0, xanchor="left", y=0.98, yanchor="top"),
        legend=dict(orientation="h", yanchor="top", y=1.0, xanchor="left", x=0,
                    bgcolor="rgba(0,0,0,0)", font=dict(size=11, color=COLORS["text"])),
        modebar=dict(orientation="h", bgcolor="rgba(0,0,0,0)"),
        hovermode="x unified", xaxis_rangeslider_visible=False)
    fig.update_yaxes(row=1,col=1,tickformat=",",gridcolor=COLORS["grid"],zeroline=False,side="right")
    fig.update_yaxes(row=2,col=1,tickformat=".2s",gridcolor=COLORS["grid"],zeroline=False,side="right")
    at = set(df["dt"].dt.normalize())
    cal = pd.date_range(df["dt"].min().normalize(), df["dt"].max().normalize(), freq="D")
    nt = [d for d in cal if d not in at]
    for rn in (1,2):
        fig.update_xaxes(row=rn,col=1,gridcolor=COLORS["grid"],zeroline=False,showgrid=False,
                         rangebreaks=[dict(values=[d.strftime("%Y-%m-%d") for d in nt])])
    fig.update_xaxes(row=2,col=1,tickformat="%y/%m/%d")
    return fig


# ═══════════════════════════════════════════════════════
#  결과 테이블
# ═══════════════════════════════════════════════════════

def _build_summary_df(trades):
    rows = []
    for i, t in enumerate(trades):
        bd = " / ".join(f"{b['tranche']}차 {b['price']:,.0f}×{b['qty']}" for b in t["buys"])
        trail = ""
        if t.get("trail_sell_dt") and t.get("trail_qty",0) > 0:
            trail = f"추적 {t['trail_sell_dt']} @{t['trail_sell_price']:,.0f} {t['trail_qty']}주"
        rows.append({"No":i+1, "트리거일":t["trigger_dt"], "매수":bd,
            "평균가":f"{t['avg_price']:,.0f}", "손절선":f"{t['trigger_low']:,.0f}",
            "매도일":t["sell_dt"], "유형":t["sell_type"],
            "투자":f"{t['total_invested']:,.0f}", "회수":f"{t['total_returned']:,.0f}",
            "손익":f"{t['pnl']:+,.0f}", "수익률":f"{t['roi_pct']:+.1f}%", "비고":trail})
    return pd.DataFrame(rows)

def _calc_summary(trades):
    if not trades:
        return {"total":0,"wins":0,"losses":0,"holds":0,"win_rate":0,"total_pnl":0,"avg_roi":0}
    w = [t for t in trades if t["sell_type"] in ("익절","익절+추적")]
    lo = [t for t in trades if t["sell_type"]=="손절"]
    ho = [t for t in trades if t["sell_type"] in ("보유중","익절+보유중")]
    cl = [t for t in trades if t["sell_type"] not in ("보유중","익절+보유중")]
    return {"total":len(trades), "wins":len(w), "losses":len(lo), "holds":len(ho),
        "win_rate":(len(w)/len(cl)*100) if cl else 0,
        "total_pnl":sum(t["pnl"] for t in trades),
        "avg_roi":(sum(t["roi_pct"] for t in cl)/len(cl)) if cl else 0}


# ═══════════════════════════════════════════════════════
#  Streamlit UI
# ═══════════════════════════════════════════════════════

_CSS = """
<style>
[data-testid="stMetric"] {
    background: linear-gradient(135deg, #1a1f2e 0%, #151926 100%);
    border: 1px solid #2a2f42; border-radius: 10px; padding: 14px 18px;
}
[data-testid="stMetric"] label { color: #8b8fa3 !important; font-size: 0.78rem !important; }
[data-testid="stMetric"] [data-testid="stMetricValue"] {
    color: #e8eaed !important; font-size: 1.15rem !important; font-weight: 600 !important;
}
section[data-testid="stSidebar"] { background: #0f1117; }
[data-testid="stDataFrame"] { border: 1px solid #2a2f42; border-radius: 8px; }
</style>
"""

def _render_one(name, ticker, enriched, trig, trades, tlog, pd_, mac):
    s = _calc_summary(trades)
    c1,c2,c3,c4,c5,c6 = st.columns(6)
    c1.metric("트레이드", f"{s['total']}건"); c2.metric("익절", f"{s['wins']}건")
    c3.metric("손절", f"{s['losses']}건"); c4.metric("승률", f"{s['win_rate']:.0f}%")
    c5.metric("총손익", f"{s['total_pnl']:+,.0f}원"); c6.metric("평균수익률", f"{s['avg_roi']:+.1f}%")
    if s["holds"]>0: st.info(f"📌 보유중 {s['holds']}건")
    fig = build_sim_chart(enriched, trig, trades, tlog, name, pd_, mac)
    st.plotly_chart(fig, use_container_width=True, key=f"sim_{ticker}",
        config={"displayModeBar":True,"modeBarButtonsToRemove":["lasso2d","select2d","autoScale2d","toggleSpikelines"],
                "displaylogo":False,"scrollZoom":True})
    if trades:
        st.markdown("#### 📋 트레이드 상세")
        df_ = _build_summary_df(trades)
        st.dataframe(df_, use_container_width=True, hide_index=True, key=f"tbl_{ticker}")
        st.download_button("📥 CSV", df_.to_csv(index=False,encoding="utf-8-sig").encode("utf-8-sig"),
            file_name=f"{name}_sim.csv", mime="text/csv", key=f"csv_{ticker}")
    else: st.info("트레이드 없음")

def _render_multi(results, pd_, mac):
    ov = []
    for it in results:
        if it.get("error"): continue
        s = _calc_summary(it["trades"])
        ov.append({"종목":f"{it['name']}({it['ticker']})", "트레이드":s["total"],
            "익절":s["wins"], "손절":s["losses"], "보유중":s["holds"],
            "승률":f"{s['win_rate']:.0f}%", "총손익":f"{s['total_pnl']:+,.0f}",
            "평균수익률":f"{s['avg_roi']:+.1f}%"})
    if ov:
        st.markdown("#### 📊 종합"); st.dataframe(pd.DataFrame(ov), use_container_width=True, hide_index=True)
        tp=sum(_calc_summary(i["trades"])["total_pnl"] for i in results if not i.get("error"))
        tt=sum(_calc_summary(i["trades"])["total"] for i in results if not i.get("error"))
        st.metric("합산 손익", f"{tp:+,.0f}원", delta=f"{tt}건")
    st.markdown("#### 📈 종목별")
    for it in results:
        if it.get("error"):
            with st.expander(f"❌ {it['query']}"): st.error(it["error"]); continue
        s=_calc_summary(it["trades"]); ic="🟢" if s["total_pnl"]>=0 else "🔴"
        with st.expander(f"{ic} {it['name']}({it['ticker']}) — {s['total']}건 | {s['total_pnl']:+,.0f}"):
            _render_one(it["name"],it["ticker"],it["enriched"],it["trigger_rows"],
                        it["trades"],it["trade_log"],pd_,mac)

def main():
    st.set_page_config(page_title="신고가 매매 시뮬", page_icon="💰", layout="wide",
                       initial_sidebar_state="expanded")
    st.markdown(_CSS, unsafe_allow_html=True)

    with st.sidebar:
        st.markdown("## ⚙️ 시뮬레이션 설정")
        input_mode = st.radio("종목 입력", ["직접 입력","파일 업로드"], horizontal=True)
        if input_mode=="직접 입력":
            ti = st.text_input("종목명/코드", placeholder="예: 삼성전자"); uf=None
        else:
            ti=None; uf=st.file_uploader("종목 목록(.txt/.md)", type=["txt","md"])

        st.markdown("---")
        st.markdown("##### 📐 차트 표시 기간")
        cp = st.selectbox("차트 기간", PERIOD_OPTIONS, index=len(PERIOD_OPTIONS)-1)

        st.markdown("##### ★ 신고가 비교 기간")
        nhl = st.selectbox("신고가 비교기간", NH_PERIOD_OPTIONS, index=1)
        nh_lb = NH_PERIOD_MAP[nhl]

        st.markdown("##### 추세 판단 MA")
        st.caption("1개 이상 (복수=AND)")
        mac: dict[int,int] = {}
        ca,cb,cc = st.columns(3)
        if ca.checkbox("MA60",True): mac[60]=st.slider("MA60 일수",2,30,5,key="tw60")
        if cb.checkbox("MA120",False): mac[120]=st.slider("MA120 일수",2,30,5,key="tw120")
        if cc.checkbox("MA180",False): mac[180]=st.slider("MA180 일수",2,30,5,key="tw180")

        st.markdown("---"); st.markdown("##### 💰 매매 설정")
        vm = st.slider("거래량 폭증 배수", 2.0, 20.0, 6.0, 0.5)
        amt = st.number_input("차수별 투자금(원)", 100_000, 100_000_000, 1_000_000, 100_000)
        tp = st.slider("1차 익절(%)", 5.0, 100.0, 20.0, 1.0, help="평균매수가 대비 → 50% 매도")
        ts = st.slider("추적 하락(%)", 1.0, 30.0, 10.0, 1.0, help="고점 대비 → 나머지 매도")
        mp = st.number_input("API 페이지", 5, 100, 40, 5)
        run = st.button("🔍  실행", use_container_width=True, type="primary")

        st.markdown("---")
        st.markdown("""<div style="font-size:0.73rem;color:#888;line-height:1.8">
        <b style="color:#FFD600">★</b> 신고가+거래량+양봉+MA추세<br>
        <b style="color:#00E676">▲1차</b> (시가+종가)/2 <b style="color:#76FF03">▲2차</b> 시가+(종가-시가)/4<br>
        <b style="color:#F44336">▼손절</b> 저가≤트리거저가<br>
        <b style="color:#2196F3">▼익절</b> 평균+목표%→50%<br>
        <b style="color:#00BCD4">▼추적</b> 고점-하락%→나머지</div>""", unsafe_allow_html=True)

    st.markdown("<h2 style='margin-bottom:0'>💰 신고가 돌파 매매 시뮬레이션</h2>"
        "<p style='color:#888;margin-top:4px'>2단계 분할매수 → 손절 / 2단계 익절</p>",
        unsafe_allow_html=True)

    # ── session_state 초기화 ──
    if "sim_results" not in st.session_state:
        st.session_state.sim_results = None
    if "sim_mode" not in st.session_state:
        st.session_state.sim_mode = None
    if "sim_params" not in st.session_state:
        st.session_state.sim_params = None

    # ── 실행 버튼 클릭 시 분석 수행 후 session_state에 저장 ──
    if run:
        if not mac:
            st.error("MA 최소 1개 선택"); return
        cd = PERIOD_MAP.get(cp, 0)

        if input_mode == "직접 입력":
            if not ti or not ti.strip():
                st.error("종목명 입력"); return
            try:
                with st.spinner("확인..."):
                    ticker, name = _resolve_ticker_and_name(ti.strip())
                with st.spinner(f"{name} 조회..."):
                    token = core.get_token(core.APP_KEY, core.APP_SECRET)
                    days = _fetch_daily_ohlcv(token, ticker, mp)
                with st.spinner("분석..."):
                    trig, enriched = _find_triggers(days, mac, vm, nh_lb)
                    trades, tlog = _run_simulation(days, trig, amt, tp, ts)
                st.session_state.sim_results = {
                    "name": name, "ticker": ticker, "enriched": enriched,
                    "trigger_rows": trig, "trades": trades, "trade_log": tlog,
                }
                st.session_state.sim_mode = "single"
                st.session_state.sim_params = {"cd": cd, "mac": mac}
            except Exception as e:
                st.error(f"오류: {e}"); st.exception(e); return
        else:
            if not uf:
                st.warning("파일 업로드"); return
            qs = _parse_ticker_file(uf.read().decode("utf-8"))
            if not qs:
                st.error("종목 없음"); return
            st.info(f"📂 {len(qs)}개: {', '.join(qs)}")
            token = core.get_token(core.APP_KEY, core.APP_SECRET)
            res = []; pg = st.progress(0)
            for i, q in enumerate(qs):
                pg.progress((i + 1) / len(qs), f"({i+1}/{len(qs)}) {q}")
                try:
                    tk, nm = _resolve_ticker_and_name(q)
                    days = _fetch_daily_ohlcv(token, tk, mp)
                    trig, en = _find_triggers(days, mac, vm, nh_lb)
                    tr, tl = _run_simulation(days, trig, amt, tp, ts)
                    res.append({"query": q, "ticker": tk, "name": nm, "enriched": en,
                                "trigger_rows": trig, "trades": tr, "trade_log": tl, "error": None})
                except Exception as e:
                    res.append({"query": q, "ticker": "", "name": q, "enriched": [],
                                "trigger_rows": [], "trades": [], "trade_log": [], "error": str(e)})
            pg.empty()
            st.session_state.sim_results = res
            st.session_state.sim_mode = "multi"
            st.session_state.sim_params = {"cd": cd, "mac": mac}

    # ── 결과 표시 (session_state에서) ──
    if st.session_state.sim_results is not None and st.session_state.sim_params is not None:
        cd = st.session_state.sim_params["cd"]
        mac_display = st.session_state.sim_params["mac"]

        if st.session_state.sim_mode == "single":
            r = st.session_state.sim_results
            st.markdown(f"### {r['name']} ({r['ticker']})")
            _render_one(r["name"], r["ticker"], r["enriched"], r["trigger_rows"],
                        r["trades"], r["trade_log"], cd, mac_display)
        elif st.session_state.sim_mode == "multi":
            res = st.session_state.sim_results
            errs = [r for r in res if r.get("error")]
            if errs:
                with st.expander(f"⚠️ 오류 {len(errs)}건"):
                    for e in errs:
                        st.warning(f"{e['query']}: {e['error']}")
            _render_multi(res, cd, mac_display)
    else:
        st.info("👈 설정 후 **실행**을 누르세요.")
        with st.expander("💡 가이드", expanded=True):
            st.markdown(f"""
**트리거** ★신고가({nhl}) + 거래량 폭증(전일×{vm:.0f}배) + 양봉 + MA상승추세

**매수** 트리거 다음일부터 저가 도달 시 체결 (각 {amt:,}원)  
· 1차: (시가+종가)/2 · 2차: 시가+(종가-시가)/4

**손절** 저가 ≤ 트리거봉 저가 → 트리거 저가에 전량 매도  
**익절** 고가 ≥ 평균가×{1+tp/100:.2f} → 50% 매도 → 이후 고점 대비 {ts:.0f}% 하락 시 나머지 매도  
**무효화** 매수 전 종가 > 트리거종가 → 취소
""")

if __name__=="__main__":
    main()