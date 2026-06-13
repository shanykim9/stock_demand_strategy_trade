"""
거래량 돌파 패턴 탐지 + 매매 시뮬레이션 — pattern_scan.py
══════════════════════════════════════════════════════════════
[사전필터] 시총≥1000억, 종가>MA120×1.02
[1] 트리거: 양봉 + 시총연동 거래량 폭증
[2] 기준하한가: 연속양봉 역추적(최대5일)
[3] 기준상한가: 양봉→최고가 추적, 음봉→확정
[4] 상한가 갱신 루프
[5] 20거래일 시간제한, 해지 우선
[6] 기준중간가 = (상한+하한)/2
[7] 시그널: 고가 > 하한+(상한-하한)×N/8
[8] 매매: 익절(1차/2차), 손절(다음날시가/장중즉시)

실행: streamlit run pattern_scan.py
"""
from __future__ import annotations
import os, math, time
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
#  상수 / 색상
# ═══════════════════════════════════════════════════════
COLORS = {
    "bull": "#E53935", "bear": "#1E88E5",
    "bull_vol": "#EF9A9A", "bear_vol": "#90CAF9",
    "trigger": "#FFD600", "trigger_vol": "#FFD600",
    "upper": "#FF5252", "lower": "#448AFF", "mid": "#AB47BC",
    "signal": "#00E676", "cancel": "#F44336",
    "buy": "#00E676", "sell_p1": "#2196F3", "sell_p2": "#00BCD4",
    "sell_loss": "#F44336", "hold": "#9E9E9E",
    "ma120": "#26A69A", "bg": "#131722", "grid": "#1E222D", "text": "#D1D4DC",
}
PERIOD_OPTIONS = ["6개월","1년","1년6개월","2년","3년","5년","전체"]
PERIOD_MAP = {"6개월":180,"1년":365,"1년6개월":548,"2년":730,"3년":1095,"5년":1825,"전체":0}

# ═══════════════════════════════════════════════════════
#  유틸
# ═══════════════════════════════════════════════════════
def _to_int(v, default=0) -> int:
    if v is None: return default
    if isinstance(v, int): return v
    s = str(v).strip().replace(",","").replace("+","").replace("-","",1) if isinstance(v,str) else str(v)
    s = s.strip().replace(",","")
    if not s: return default
    try: return int(float(s))
    except: return default

def _parse_dt(v) -> str|None:
    if v is None: return None
    s = str(v).strip()
    if len(s)>=8 and s[:8].isdigit(): return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    if len(s)>=10 and s[4]=="-" and s[7]=="-": return s[:10]
    return None

def _first(row, keys):
    for k in keys:
        if k in row and str(row.get(k)).strip()!="": return row.get(k)
    return None

def _resolve(q):
    q=(q or "").strip()
    if not q: raise RuntimeError("종목명 입력 필요")
    if q.isdigit() and len(q)==6: ticker=q
    else:
        ticker,err=core.resolve_ticker(q)
        if err or not ticker: raise RuntimeError(err or f"'{q}' 확인 필요")
    name=ticker
    try: name=(core._krx_cache.get("name_by_code") or {}).get(ticker,ticker)
    except: pass
    return ticker,name

def _parse_file(content):
    out=[]
    for p in content.replace("\n",",").replace("\r",",").split(","):
        t=p.strip()
        if t: out.append(t)
    return out

# ═══════════════════════════════════════════════════════
#  시총 조회 (ka10001)
# ═══════════════════════════════════════════════════════
def _get_market_cap(token: str, ticker: str) -> float:
    """시가총액을 억원 단위로 반환. 실패 시 0."""
    try:
        body = {"stk_cd": ticker}
        res = core.call_tr_all_pages(
            token=token, api_id="ka10001", body=body,
            endpoint="/api/dostk/stkinfo", max_pages=1,
        )
        rows = res.get("rows") or []
        if not rows:
            # single data
            data = res.get("data") or res
            cap_raw = _first(data, ["mktc","market_cap","시가총액","mkt_cap","tot_mktc"])
            if cap_raw:
                return abs(_to_int(cap_raw)) # 억원 단위
        else:
            r = rows[0]
            cap_raw = _first(r, ["mktc","market_cap","시가총액","mkt_cap","tot_mktc"])
            if cap_raw:
                return abs(_to_int(cap_raw))
    except Exception:
        pass
    return 0.0

def _calc_volume_pct(market_cap_억: float) -> float:
    """시총 연동 거래량 증가율(%) 반환. 1000억→400%, 3조→100%, 3조초과→100%"""
    if market_cap_억 <= 1000: return 400.0
    if market_cap_억 >= 30000: return 100.0
    return 400.0 - (market_cap_억 - 1000.0) * 300.0 / 29000.0

def _volume_multiplier(market_cap_억: float) -> float:
    """거래량 배수 (전일 대비). 예: 400% → 5.0배"""
    return 1.0 + _calc_volume_pct(market_cap_억) / 100.0

# ═══════════════════════════════════════════════════════
#  OHLCV 조회
# ═══════════════════════════════════════════════════════
def _fetch_ohlcv(token, ticker, max_pages=40):
    end_dt = datetime.now(core.TZ).strftime("%Y%m%d")
    stex = (os.getenv("KIWOOM_DMST_STEX_TP") or "KRX").strip().upper()
    upd = (os.getenv("KIWOOM_OHLCV_UPD_STKPC_TP") or "1").strip()
    common = {"stk_cd":ticker,"stex_tp":stex,"dmst_stex_tp":stex}
    bodies = [
        {**common,"base_dt":end_dt,"upd_stkpc_tp":upd},
        {**common,"base_dt":end_dt},
        {**common,"dt":end_dt,"upd_stkpc_tp":upd},
        {**common,"dt":end_dt},
    ]
    last_err=None
    for body in bodies:
        try:
            res=core.call_tr_all_pages(token=token,api_id="ka10081",body=body,
                                       endpoint="/api/dostk/chart",max_pages=max_pages)
            rows=res.get("rows") or []
            if not rows: continue
            dedup={}
            for r in rows:
                dt=_parse_dt(_first(r,["dt","date","bas_dt","base_dt","trde_dt","trd_dt"]))
                if not dt: continue
                op=_to_int(_first(r,["open_pric","open","stck_oprc","opn_prc"]),0)
                hp=_to_int(_first(r,["high_pric","high","stck_hgpr","hgh_prc"]),0)
                lp=_to_int(_first(r,["low_pric","low","stck_lwpr","low_prc"]),0)
                cp=_to_int(_first(r,["close_pric","close","stck_clpr","cur_prc","cur_pric"]),0)
                vol=_to_int(_first(r,["trde_qty","volume","acml_vol","acc_trde_qty"]),0)
                if cp<=0: continue
                if op<=0: op=cp
                if hp<=0: hp=max(op,cp)
                if lp<=0: lp=min(op,cp)
                dedup[dt]={"dt":dt,"open":op,"high":hp,"low":lp,"close":cp,"volume":max(0,vol)}
            out=sorted(dedup.values(),key=lambda x:x["dt"])
            if out: return out
        except Exception as e:
            last_err=e; continue
    raise RuntimeError(f"일봉 조회 실패: {last_err}")

# ═══════════════════════════════════════════════════════
#  MA 계산
# ═══════════════════════════════════════════════════════
def _ma(values, w):
    out=[None]*len(values)
    if w<=0: return out
    s=0.0
    for i,v in enumerate(values):
        s+=float(v)
        if i>=w: s-=float(values[i-w])
        if i>=w-1: out[i]=s/float(w)
    return out

# ═══════════════════════════════════════════════════════
#  패턴 탐지 엔진
# ═══════════════════════════════════════════════════════
def _detect_pattern(
    days: list[dict],
    vol_multiplier: float,
    signal_ratio: float = 7/8,
    max_watch_days: int = 20,
) -> list[dict]:
    """
    패턴 탐지 상태머신:
    IDLE → TRIGGERED(하한가설정) → TRACKING_HIGH → UPPER_CONFIRMED(시그널감시)
    → SIGNAL / CANCEL → IDLE

    Returns: list of pattern dicts with trigger/signal info
    """
    closes=[d["close"] for d in days]
    opens=[d["open"] for d in days]
    highs=[d["high"] for d in days]
    lows=[d["low"] for d in days]
    vols=[d["volume"] for d in days]
    ma120=_ma(closes,120)

    patterns=[]
    state="IDLE"
    base_lower=0; base_upper=0; peak_high=0
    trigger_idx=0; trigger_day_count=0
    upper_confirmed=False

    def _is_bull(i):
        return closes[i]>=opens[i]  # 도지형도 양봉

    def _is_bear(i):
        return closes[i]<opens[i]

    for i in range(1, len(days)):
        d=days[i]

        if state=="IDLE":
            # 사전필터: MA120 위 + 2% 여유
            if ma120[i] is None: continue
            if closes[i] <= ma120[i]*1.02: continue

            # 트리거: 양봉 + 거래량 폭증
            if not _is_bull(i): continue
            prev_vol=vols[i-1]
            if prev_vol<=0: continue
            if vols[i] < prev_vol * vol_multiplier: continue

            # ── 트리거 발생! ──
            # 기준하한가: 연속 양봉 역추적 (최대 5일)
            base_lower = opens[i]  # 기본값: 트리거봉 시가
            if i>=1 and _is_bull(i-1):
                # 전일 양봉 → 역추적
                trace_start = i-1
                for back in range(2, 6):  # 최대 5일 역추적
                    if i-back < 0: break
                    if _is_bull(i-back):
                        trace_start = i-back
                    else:
                        break
                base_lower = opens[trace_start]
            # else: 전일 음봉 → 트리거봉 시가 (이미 설정됨)

            peak_high = highs[i]  # 트리거봉 고가부터 추적 시작
            trigger_idx = i
            trigger_day_count = 0
            upper_confirmed = False
            base_upper = 0
            state = "TRACKING_HIGH"
            continue

        # ── 공통: 20거래일 체크 ──
        trigger_day_count += 1
        if trigger_day_count > max_watch_days:
            state = "IDLE"; continue

        if state == "TRACKING_HIGH":
            # 양봉/도지 → 고가 추적
            if _is_bull(i):
                if highs[i] > peak_high:
                    peak_high = highs[i]
                continue
            # 음봉 → 상한가 확정
            base_upper = peak_high
            upper_confirmed = True
            mid = (base_upper + base_lower) / 2.0
            signal_line = base_lower + (base_upper - base_lower) * signal_ratio

            # 즉시 해지 체크: 음봉 종가 < 중간가
            if closes[i] < mid:
                state = "IDLE"; continue

            # 즉시 시그널 체크 (이 음봉의 고가가 시그널선 초과?)
            # → 해지 우선이므로 종가 ≥ 중간가 확인 후 체크
            if highs[i] > signal_line:
                patterns.append({
                    "trigger_idx": trigger_idx,
                    "trigger_dt": days[trigger_idx]["dt"],
                    "signal_idx": i,
                    "signal_dt": d["dt"],
                    "base_lower": base_lower,
                    "base_upper": base_upper,
                    "mid": mid,
                    "signal_line": signal_line,
                    "trigger_open": opens[trigger_idx],
                    "trigger_close": closes[trigger_idx],
                    "trigger_high": highs[trigger_idx],
                    "trigger_low": lows[trigger_idx],
                    "vol_ratio": vols[trigger_idx]/float(vols[trigger_idx-1]) if vols[trigger_idx-1]>0 else 0,
                })
                state = "IDLE"; continue

            state = "SIGNAL_WATCH"
            continue

        if state == "SIGNAL_WATCH":
            mid = (base_upper + base_lower) / 2.0
            signal_line = base_lower + (base_upper - base_lower) * signal_ratio

            # 해지 우선: 종가 < 중간가
            if closes[i] < mid:
                state = "IDLE"; continue

            # 재돌파: 양봉이 상한가 초과 → 갱신 루프
            if _is_bull(i) and highs[i] > base_upper:
                peak_high = highs[i]
                state = "TRACKING_HIGH"
                continue

            # 시그널: 고가 > 시그널선
            if highs[i] > signal_line:
                patterns.append({
                    "trigger_idx": trigger_idx,
                    "trigger_dt": days[trigger_idx]["dt"],
                    "signal_idx": i,
                    "signal_dt": d["dt"],
                    "base_lower": base_lower,
                    "base_upper": base_upper,
                    "mid": mid,
                    "signal_line": signal_line,
                    "trigger_open": opens[trigger_idx],
                    "trigger_close": closes[trigger_idx],
                    "trigger_high": highs[trigger_idx],
                    "trigger_low": lows[trigger_idx],
                    "vol_ratio": vols[trigger_idx]/float(vols[trigger_idx-1]) if vols[trigger_idx-1]>0 else 0,
                })
                state = "IDLE"; continue

    return patterns


# ═══════════════════════════════════════════════════════
#  매매 시뮬레이션
# ═══════════════════════════════════════════════════════
def _simulate(
    days: list[dict],
    patterns: list[dict],
    tp1_pct: float = 20.0,
    tp2_pct: float = 30.0,
    sl_pct: float = 7.0,
    sl_mode: str = "next_open",  # "next_open" or "intraday"
) -> tuple[list[dict], list[dict]]:
    """
    매매 시뮬레이션
    매수: 시그널 다음날 시가
    익절: +tp1% → 50%, +tp2% → 전량
    손절: -sl% → next_open(다음날 시가) or intraday(즉시)
    """
    if not patterns: return [], []

    day_map = {d["dt"]: (idx, d) for idx, d in enumerate(days)}
    trades = []
    trade_log = []

    for pat in patterns:
        sig_idx = pat["signal_idx"]
        buy_idx = sig_idx + 1  # 시그널 다음날

        if buy_idx >= len(days): continue

        buy_price = days[buy_idx]["open"]
        if buy_price <= 0: continue

        buy_dt = days[buy_idx]["dt"]
        tp1_price = buy_price * (1 + tp1_pct / 100.0)
        tp2_price = buy_price * (1 + tp2_pct / 100.0)
        sl_price = buy_price * (1 - sl_pct / 100.0)

        trade_log.append({
            "trigger_dt": pat["trigger_dt"], "signal_dt": pat["signal_dt"],
            "action": "매수", "dt": buy_dt, "price": buy_price, "qty_pct": 100,
        })

        # 매매 추적
        holding_pct = 100  # 보유 비율
        tp1_done = False
        sl_triggered = False
        sell_dt = None; sell_price = 0; sell_type = "보유중"
        total_return_pct = 0.0

        for j in range(buy_idx + 1, len(days)):
            dj = days[j]
            h, l, c, o = dj["high"], dj["low"], dj["close"], dj["open"]

            # 손절 모드: 장중 즉시
            if sl_mode == "intraday" and holding_pct > 0:
                if l <= sl_price:
                    sell_type = "손절(즉시)"
                    sell_price = sl_price
                    sell_dt = dj["dt"]
                    total_return_pct += (sell_price / buy_price - 1) * holding_pct
                    trade_log.append({
                        "trigger_dt": pat["trigger_dt"], "signal_dt": pat["signal_dt"],
                        "action": "손절", "dt": sell_dt, "price": sell_price, "qty_pct": holding_pct,
                    })
                    holding_pct = 0; break

            # 손절 모드: 다음날 시가 (전일 종가 기준 확인)
            if sl_mode == "next_open" and holding_pct > 0:
                # 전일 종가 확인 (j-1)
                prev_close = days[j-1]["close"] if j > buy_idx else buy_price
                if prev_close <= sl_price:
                    sell_type = "손절(시가)"
                    sell_price = o  # 다음날 시가
                    sell_dt = dj["dt"]
                    total_return_pct += (sell_price / buy_price - 1) * holding_pct
                    trade_log.append({
                        "trigger_dt": pat["trigger_dt"], "signal_dt": pat["signal_dt"],
                        "action": "손절", "dt": sell_dt, "price": sell_price, "qty_pct": holding_pct,
                    })
                    holding_pct = 0; break

            # 익절 2차: +tp2% 전량 (1차보다 먼저 체크하면 안 됨 — 같은 날 1차+2차 가능)
            if not tp1_done and holding_pct > 0 and h >= tp1_price:
                # 1차 익절
                sell_qty = holding_pct // 2
                tp1_done = True
                total_return_pct += (tp1_price / buy_price - 1) * sell_qty
                holding_pct -= sell_qty
                trade_log.append({
                    "trigger_dt": pat["trigger_dt"], "signal_dt": pat["signal_dt"],
                    "action": f"익절1({tp1_pct:.0f}%)", "dt": dj["dt"],
                    "price": tp1_price, "qty_pct": sell_qty,
                })
                # 같은 날 2차도 가능
                if holding_pct > 0 and h >= tp2_price:
                    total_return_pct += (tp2_price / buy_price - 1) * holding_pct
                    trade_log.append({
                        "trigger_dt": pat["trigger_dt"], "signal_dt": pat["signal_dt"],
                        "action": f"익절2({tp2_pct:.0f}%)", "dt": dj["dt"],
                        "price": tp2_price, "qty_pct": holding_pct,
                    })
                    sell_type = "익절(전량)"; sell_price = tp2_price; sell_dt = dj["dt"]
                    holding_pct = 0; break
                continue

            if tp1_done and holding_pct > 0 and h >= tp2_price:
                # 2차 익절
                total_return_pct += (tp2_price / buy_price - 1) * holding_pct
                trade_log.append({
                    "trigger_dt": pat["trigger_dt"], "signal_dt": pat["signal_dt"],
                    "action": f"익절2({tp2_pct:.0f}%)", "dt": dj["dt"],
                    "price": tp2_price, "qty_pct": holding_pct,
                })
                sell_type = "익절(전량)"; sell_price = tp2_price; sell_dt = dj["dt"]
                holding_pct = 0; break

        # 미결제
        if holding_pct > 0:
            last = days[-1]
            sell_dt = last["dt"]; sell_price = last["close"]
            sell_type = "보유중" if not tp1_done else "익절1+보유중"
            total_return_pct += (sell_price / buy_price - 1) * holding_pct

        trades.append({
            "trigger_dt": pat["trigger_dt"],
            "signal_dt": pat["signal_dt"],
            "buy_dt": buy_dt, "buy_price": buy_price,
            "sell_dt": sell_dt, "sell_price": sell_price,
            "sell_type": sell_type,
            "roi_pct": total_return_pct / 100.0 * 100,  # normalize
            "base_lower": pat["base_lower"],
            "base_upper": pat["base_upper"],
            "signal_line": pat["signal_line"],
            "tp1_done": tp1_done,
        })

    return trades, trade_log


# ═══════════════════════════════════════════════════════
#  enriched 데이터 (차트용)
# ═══════════════════════════════════════════════════════
def _enrich(days):
    closes=[d["close"] for d in days]
    ma120=_ma(closes,120)
    out=[]
    for i,d in enumerate(days):
        out.append({**d, "ma120":ma120[i]})
    return out


# ═══════════════════════════════════════════════════════
#  차트
# ═══════════════════════════════════════════════════════
def _build_chart(enriched, patterns, trades, trade_log, name, period_days=0):
    df=pd.DataFrame(enriched); df["dt"]=pd.to_datetime(df["dt"])
    if period_days>0:
        df=df[df["dt"]>=datetime.now()-timedelta(days=period_days)].copy().reset_index(drop=True)
    if df.empty:
        fig=go.Figure(); fig.add_annotation(text="데이터 없음",showarrow=False); return fig

    df["is_bull"]=df["close"]>=df["open"]
    trig_set={p["trigger_dt"] for p in patterns}
    sig_set={p["signal_dt"] for p in patterns}
    ds=df["dt"].dt.strftime("%Y-%m-%d")
    df["is_trig"]=ds.isin(trig_set)

    vc=[COLORS["trigger_vol"] if row["is_trig"]
        else (COLORS["bull_vol"] if row["is_bull"] else COLORS["bear_vol"])
        for _,row in df.iterrows()]

    fig=make_subplots(rows=2,cols=1,shared_xaxes=True,vertical_spacing=0.02,row_heights=[0.75,0.25])

    fig.add_trace(go.Candlestick(
        x=df["dt"],open=df["open"],high=df["high"],low=df["low"],close=df["close"],
        increasing=dict(line=dict(color=COLORS["bull"]),fillcolor=COLORS["bull"]),
        decreasing=dict(line=dict(color=COLORS["bear"]),fillcolor=COLORS["bear"]),
        name="일봉"),row=1,col=1)

    if "ma120" in df.columns:
        fig.add_trace(go.Scatter(x=df["dt"],y=df["ma120"],name="MA120",
            line=dict(color=COLORS["ma120"],width=1.2,dash="dash"),hoverinfo="skip"),row=1,col=1)

    # 트리거 마커
    dt_=df[df["is_trig"]]
    if not dt_.empty:
        fig.add_trace(go.Scatter(x=dt_["dt"],y=dt_["high"]*1.04,mode="markers",name="★ 트리거",
            marker=dict(symbol="star",size=14,color=COLORS["trigger"],line=dict(width=1,color="#F9A825")),
            hovertemplate="%{x|%Y-%m-%d}<br><b>★ 트리거</b><extra></extra>"),row=1,col=1)

    # 시그널 마커
    sig_entries=[p for p in patterns if p["signal_dt"] in set(ds)]
    if sig_entries:
        sig_dates=pd.to_datetime([p["signal_dt"] for p in sig_entries])
        sig_prices=[p["signal_line"] for p in sig_entries]
        fig.add_trace(go.Scatter(x=sig_dates,y=[p*1.02 for p in sig_prices],
            mode="markers",name="◆ 시그널",
            marker=dict(symbol="diamond",size=13,color=COLORS["signal"],line=dict(width=1.5,color="#FFF")),
            hovertemplate="%{x|%Y-%m-%d}<br><b>◆ 시그널</b><extra></extra>"),row=1,col=1)

    # 매수/매도 마커
    buys=[e for e in trade_log if e["action"]=="매수"]
    if buys:
        fig.add_trace(go.Scatter(
            x=pd.to_datetime([e["dt"] for e in buys]),
            y=[e["price"]*0.97 for e in buys],mode="markers",name="▲ 매수",
            marker=dict(symbol="triangle-up",size=12,color=COLORS["buy"],line=dict(width=1,color="#004D40")),
            customdata=[e["price"] for e in buys],
            hovertemplate="%{x|%Y-%m-%d}<br><b>▲ 매수</b><br>%{customdata:,.0f}원<extra></extra>"),row=1,col=1)

    for act_key,color,label in [
        ("익절",COLORS["sell_p1"],"▼ 익절"),("손절",COLORS["sell_loss"],"▼ 손절")]:
        entries=[e for e in trade_log if act_key in e["action"] and e["action"]!="매수"]
        if entries:
            fig.add_trace(go.Scatter(
                x=pd.to_datetime([e["dt"] for e in entries]),
                y=[e["price"]*1.03 for e in entries],mode="markers",name=label,
                marker=dict(symbol="triangle-down",size=12,color=color,line=dict(width=1.5,color="#FFF")),
                customdata=[e["price"] for e in entries],
                hovertemplate=f"%{{x|%Y-%m-%d}}<br><b>{label}</b><br>%{{customdata:,.0f}}원<extra></extra>"),row=1,col=1)

    # 기준가 라인 (패턴별 수평선)
    for pat in patterns:
        dt_range=df[(df["dt"]>=pd.Timestamp(pat["trigger_dt"]))&(df["dt"]<=pd.Timestamp(pat["signal_dt"])+timedelta(days=5))]
        if dt_range.empty: continue
        x0,x1=dt_range["dt"].iloc[0],dt_range["dt"].iloc[-1]
        for val,color,dash,lbl in [
            (pat["base_upper"],COLORS["upper"],"dot","상한"),
            (pat["base_lower"],COLORS["lower"],"dot","하한"),
            (pat["mid"],COLORS["mid"],"dashdot","중간"),
            (pat["signal_line"],COLORS["signal"],"dash","시그널"),]:
            fig.add_shape(type="line",x0=x0,x1=x1,y0=val,y1=val,
                line=dict(color=color,width=1,dash=dash),row=1,col=1)

    fig.add_trace(go.Bar(x=df["dt"],y=df["volume"],name="거래량",
        marker_color=vc,marker_line_width=0,
        hovertemplate="%{x|%Y-%m-%d}<br>%{y:,.0f}<extra></extra>"),row=2,col=1)

    fig.update_layout(height=750,margin=dict(l=0,r=0,t=80,b=0),
        paper_bgcolor=COLORS["bg"],plot_bgcolor=COLORS["bg"],
        font=dict(color=COLORS["text"],size=12),
        title=dict(text=f"  {name} — 패턴 탐지 + 시뮬레이션",font=dict(size=16,color="#FFF"),
                   x=0,xanchor="left",y=0.98,yanchor="top"),
        legend=dict(orientation="h",yanchor="top",y=1.0,xanchor="left",x=0,
                    bgcolor="rgba(0,0,0,0)",font=dict(size=11,color=COLORS["text"])),
        hovermode="x unified",xaxis_rangeslider_visible=False)
    fig.update_yaxes(row=1,col=1,tickformat=",",gridcolor=COLORS["grid"],zeroline=False,side="right")
    fig.update_yaxes(row=2,col=1,tickformat=".2s",gridcolor=COLORS["grid"],zeroline=False,side="right")
    at=set(df["dt"].dt.normalize())
    cal=pd.date_range(df["dt"].min().normalize(),df["dt"].max().normalize(),freq="D")
    nt=[d for d in cal if d not in at]
    for rn in (1,2):
        fig.update_xaxes(row=rn,col=1,gridcolor=COLORS["grid"],zeroline=False,showgrid=False,
                         rangebreaks=[dict(values=[d.strftime("%Y-%m-%d") for d in nt])])
    fig.update_xaxes(row=2,col=1,tickformat="%y/%m/%d")
    return fig


# ═══════════════════════════════════════════════════════
#  결과 테이블 / 요약
# ═══════════════════════════════════════════════════════
def _summary_df(trades):
    rows=[]
    for i,t in enumerate(trades):
        rows.append({
            "No":i+1, "트리거":t["trigger_dt"], "시그널":t["signal_dt"],
            "매수일":t["buy_dt"], "매수가":f"{t['buy_price']:,.0f}",
            "하한":f"{t['base_lower']:,.0f}", "상한":f"{t['base_upper']:,.0f}",
            "매도일":t["sell_dt"], "유형":t["sell_type"],
            "수익률":f"{t['roi_pct']:+.1f}%",
        })
    return pd.DataFrame(rows)

def _calc_stats(trades):
    if not trades: return {"total":0,"wins":0,"losses":0,"holds":0,"win_rate":0,"avg_roi":0}
    w=[t for t in trades if "익절" in t["sell_type"]]
    lo=[t for t in trades if "손절" in t["sell_type"]]
    ho=[t for t in trades if "보유" in t["sell_type"]]
    cl=[t for t in trades if "보유" not in t["sell_type"]]
    return {
        "total":len(trades),"wins":len(w),"losses":len(lo),"holds":len(ho),
        "win_rate":(len(w)/len(cl)*100) if cl else 0,
        "avg_roi":(sum(t["roi_pct"] for t in cl)/len(cl)) if cl else 0,
    }


# ═══════════════════════════════════════════════════════
#  Streamlit UI
# ═══════════════════════════════════════════════════════
_CSS="""<style>
[data-testid="stMetric"]{background:linear-gradient(135deg,#1a1f2e,#151926);
border:1px solid #2a2f42;border-radius:10px;padding:14px 18px}
[data-testid="stMetric"] label{color:#8b8fa3!important;font-size:.78rem!important}
[data-testid="stMetric"] [data-testid="stMetricValue"]{color:#e8eaed!important;font-size:1.15rem!important;font-weight:600!important}
section[data-testid="stSidebar"]{background:#0f1117}
</style>"""

def _render_one(name,ticker,enriched,patterns,trades,tlog,pd_):
    s=_calc_stats(trades)
    c1,c2,c3,c4,c5=st.columns(5)
    c1.metric("트레이드",f"{s['total']}건"); c2.metric("익절",f"{s['wins']}건")
    c3.metric("손절",f"{s['losses']}건"); c4.metric("승률",f"{s['win_rate']:.0f}%")
    c5.metric("평균수익률",f"{s['avg_roi']:+.1f}%")
    if s["holds"]>0: st.info(f"📌 보유중 {s['holds']}건")
    fig=_build_chart(enriched,patterns,trades,tlog,name,pd_)
    st.plotly_chart(fig,use_container_width=True,key=f"ch_{ticker}",
        config={"displayModeBar":True,"displaylogo":False,"scrollZoom":True,
                "modeBarButtonsToRemove":["lasso2d","select2d","autoScale2d","toggleSpikelines"]})
    if trades:
        st.markdown("#### 📋 트레이드 상세")
        st.dataframe(_summary_df(trades),use_container_width=True,hide_index=True,key=f"tb_{ticker}")
        csv=_summary_df(trades).to_csv(index=False,encoding="utf-8-sig").encode("utf-8-sig")
        st.download_button("📥 CSV",csv,f"{name}_pattern.csv","text/csv",key=f"csv_{ticker}")
    elif patterns:
        st.success(f"패턴 {len(patterns)}개 탐지 (매매 시그널 발생)")
    else:
        st.info("패턴 미탐지")

def _render_multi(results,pd_):
    ov=[]
    for it in results:
        if it.get("error"): continue
        s=_calc_stats(it["trades"])
        if s["total"]==0 and not it.get("patterns"): continue  # 패턴 없는 종목 숨김
        ov.append({"종목":f"{it['name']}({it['ticker']})",
            "패턴":len(it.get("patterns",[])), "트레이드":s["total"],
            "익절":s["wins"],"손절":s["losses"],"승률":f"{s['win_rate']:.0f}%",
            "평균수익률":f"{s['avg_roi']:+.1f}%"})
    if ov:
        st.markdown("#### 📊 종합 결과")
        st.dataframe(pd.DataFrame(ov),use_container_width=True,hide_index=True)
    found=[it for it in results if not it.get("error") and (it.get("patterns") or it.get("trades"))]
    if not found:
        st.warning("조건을 만족하는 패턴이 없습니다.")
        return
    st.markdown(f"#### 📈 패턴 탐지 종목 ({len(found)}개)")
    for it in found:
        s=_calc_stats(it["trades"])
        ic="🟢" if s["avg_roi"]>=0 else "🔴"
        with st.expander(f"{ic} {it['name']}({it['ticker']}) — 패턴 {len(it['patterns'])}개"):
            _render_one(it["name"],it["ticker"],it["enriched"],it["patterns"],
                        it["trades"],it["trade_log"],pd_)
    errors=[r for r in results if r.get("error")]
    if errors:
        with st.expander(f"⚠️ 오류 {len(errors)}건"):
            for e in errors: st.warning(f"{e['query']}: {e['error']}")


def main():
    st.set_page_config(page_title="패턴 스캔",page_icon="🔍",layout="wide",initial_sidebar_state="expanded")
    st.markdown(_CSS,unsafe_allow_html=True)

    with st.sidebar:
        st.markdown("## ⚙️ 패턴 스캔 설정")
        input_mode=st.radio("입력 방식",["직접 입력","파일 업로드"],horizontal=True)
        if input_mode=="직접 입력":
            ti=st.text_input("종목명/코드",placeholder="예: 삼성전자"); uf=None
        else:
            ti=None; uf=st.file_uploader("종목 목록(.txt/.md)",type=["txt","md"])

        st.markdown("---")
        st.markdown("##### 📐 차트 기간")
        cp=st.selectbox("차트 표시",PERIOD_OPTIONS,index=len(PERIOD_OPTIONS)-1)

        st.markdown("##### 🔍 패턴 조건")
        min_cap=st.number_input("최소 시총(억원)",100,100000,1000,100,
                                help="1000 = 1,000억원")
        sig_ratio_label=st.selectbox("시그널 비율",["5/8","6/8","7/8"],index=2)
        sig_ratio={"5/8":5/8,"6/8":6/8,"7/8":7/8}[sig_ratio_label]
        max_watch=st.slider("최대 감시일(거래일)",5,40,20)
        vol_override=st.checkbox("거래량 배수 직접 입력",False)
        if vol_override:
            vol_mult_manual=st.slider("거래량 배수",1.5,10.0,3.0,0.5)
        else:
            vol_mult_manual=None

        st.markdown("---")
        st.markdown("##### 💰 매매 설정")
        tp1=st.slider("1차 익절(%)",5.0,50.0,20.0,1.0,help="→ 50% 매도")
        tp2=st.slider("2차 익절(%)",10.0,100.0,30.0,1.0,help="→ 전량 매도")
        sl_mode=st.radio("손절 방식",["다음날 시가","장중 즉시"],horizontal=True)
        sl_pct=st.slider("손절(%)",1.0,20.0,7.0,0.5)
        max_pages=st.number_input("API 페이지",5,100,40,5)

        run=st.button("🔍 스캔 실행",use_container_width=True,type="primary")

        st.markdown("---")
        st.markdown("""<div style="font-size:.73rem;color:#888;line-height:1.8">
        <b style="color:#FFD600">★</b> 트리거: 양봉+거래량폭증+MA120위<br>
        <b style="color:#FF5252">━</b> 상한가
        <b style="color:#448AFF">━</b> 하한가
        <b style="color:#AB47BC">━</b> 중간가<br>
        <b style="color:#00E676">◆</b> 시그널: 고가>하한+(상한-하한)×N/8<br>
        <b style="color:#00E676">▲</b> 매수: 시그널 다음날 시가<br>
        <b style="color:#2196F3">▼</b> 익절 <b style="color:#F44336">▼</b> 손절
        </div>""",unsafe_allow_html=True)

    st.markdown("<h2 style='margin-bottom:0'>🔍 거래량 돌파 패턴 스캔</h2>"
        "<p style='color:#888;margin-top:4px'>트리거→기준가→시그널→매매 시뮬레이션</p>",
        unsafe_allow_html=True)

    # session_state
    if "scan_results" not in st.session_state: st.session_state.scan_results=None
    if "scan_mode" not in st.session_state: st.session_state.scan_mode=None
    if "scan_pd" not in st.session_state: st.session_state.scan_pd=0

    if run:
        if tp2<=tp1:
            st.error("2차 익절률은 1차보다 커야 합니다."); return

        cd=PERIOD_MAP.get(cp,0)
        sl_m="intraday" if sl_mode=="장중 즉시" else "next_open"

        if input_mode=="직접 입력":
            if not ti or not ti.strip(): st.error("종목명 입력"); return
            try:
                with st.spinner("종목 확인..."):
                    ticker,name=_resolve(ti.strip())
                with st.spinner("시총 확인..."):
                    token=core.get_token(core.APP_KEY,core.APP_SECRET)
                    cap=_get_market_cap(token,ticker)
                    if cap>0 and cap<min_cap:
                        st.error(f"{name} 시총 {cap:,.0f}억원 < 기준 {min_cap:,.0f}억원")
                        return
                vm=vol_mult_manual if vol_override and vol_mult_manual else _volume_multiplier(cap if cap>0 else 1000)
                if cap>0:
                    st.info(f"📊 {name} 시총: {cap:,.0f}억원 → 거래량 기준: {_calc_volume_pct(cap):.0f}% ({vm:.1f}배)")
                with st.spinner(f"{name} 데이터 조회..."):
                    days=_fetch_ohlcv(token,ticker,max_pages)
                with st.spinner("패턴 분석..."):
                    patterns=_detect_pattern(days,vm,sig_ratio,max_watch)
                    trades,tlog=_simulate(days,patterns,tp1,tp2,sl_pct,sl_m)
                    enriched=_enrich(days)
                st.session_state.scan_results={
                    "name":name,"ticker":ticker,"enriched":enriched,
                    "patterns":patterns,"trades":trades,"trade_log":tlog}
                st.session_state.scan_mode="single"
                st.session_state.scan_pd=cd
            except Exception as e:
                st.error(f"오류: {e}"); st.exception(e); return
        else:
            if not uf: st.warning("파일 업로드 필요"); return
            qs=_parse_file(uf.read().decode("utf-8"))
            if not qs: st.error("종목 없음"); return
            st.info(f"📂 {len(qs)}개 종목: {', '.join(qs[:20])}{'...' if len(qs)>20 else ''}")
            token=core.get_token(core.APP_KEY,core.APP_SECRET)
            results=[]; pg=st.progress(0)
            for idx,q in enumerate(qs):
                pg.progress((idx+1)/len(qs),f"({idx+1}/{len(qs)}) {q}")
                try:
                    tk,nm=_resolve(q)
                    cap=_get_market_cap(token,tk)
                    if cap>0 and cap<min_cap:
                        results.append({"query":q,"ticker":tk,"name":nm,"enriched":[],
                            "patterns":[],"trades":[],"trade_log":[],"error":f"시총 {cap:,.0f}억 < {min_cap:,.0f}억"})
                        continue
                    vm=vol_mult_manual if vol_override and vol_mult_manual else _volume_multiplier(cap if cap>0 else 1000)
                    days=_fetch_ohlcv(token,tk,max_pages)
                    pats=_detect_pattern(days,vm,sig_ratio,max_watch)
                    trs,tl=_simulate(days,pats,tp1,tp2,sl_pct,sl_m)
                    en=_enrich(days)
                    results.append({"query":q,"ticker":tk,"name":nm,"enriched":en,
                        "patterns":pats,"trades":trs,"trade_log":tl,"error":None})
                except Exception as e:
                    results.append({"query":q,"ticker":"","name":q,"enriched":[],
                        "patterns":[],"trades":[],"trade_log":[],"error":str(e)})
            pg.empty()
            st.session_state.scan_results=results
            st.session_state.scan_mode="multi"
            st.session_state.scan_pd=cd

    # 결과 표시
    if st.session_state.scan_results is not None:
        pd_=st.session_state.scan_pd
        if st.session_state.scan_mode=="single":
            r=st.session_state.scan_results
            st.markdown(f"### {r['name']} ({r['ticker']})")
            _render_one(r["name"],r["ticker"],r["enriched"],r["patterns"],
                        r["trades"],r["trade_log"],pd_)
        elif st.session_state.scan_mode=="multi":
            _render_multi(st.session_state.scan_results,pd_)
    else:
        st.info("👈 설정 후 **스캔 실행**을 누르세요.")
        with st.expander("💡 패턴 가이드",expanded=True):
            st.markdown("""
**[사전필터]** 시총 ≥ 설정값, 종가 > MA120 × 1.02

**[트리거]** 양봉 + 거래량 ≥ 전일 × 시총연동배수(100~400%)

**[기준가 설정]**
- 하한가: 연속 양봉 역추적(최대5일) 첫 시가 / 전일 음봉이면 트리거 시가
- 상한가: 양봉 고가 추적 → 음봉 출현 시 확정

**[시그널]** 고가 > 하한 + (상한-하한) × N/8 (종가 ≥ 중간가 유지)

**[매매]** 시그널 다음날 시가 매수 → 1차/2차 익절, 손절
""")


if __name__=="__main__":
    main()