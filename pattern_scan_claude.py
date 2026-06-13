"""
거래량 돌파 패턴 탐지 + 매매 시뮬레이션 — pattern_scan.py
══════════════════════════════════════════════════════════════
[사전필터] 시총≥1000억, 종가>MA120×1.02
[1] 트리거: 양봉 + 시총연동 거래량 폭증
[2] 기준하한가: 연속상승 역추적(최대5일)
    - 기본: 연속 양봉(도지 포함)
    - 옵션: "종가 상승(전일 종가 대비)"도 연속으로 인정
[3] 기준상한가: 양봉→최고가 추적, 음봉→확정
    + 옵션: 음봉 당일 고가도 상한 후보로 반영 가능
[4] 재돌파/상한갱신 처리(절충안)
    - close > base_upper  → 강세 재돌파 → TRACKING_HIGH 복귀
    - high  > base_upper AND close <= base_upper
        → 리셋하지 않고 base_upper만 high로 갱신(상한 확대)
        → 조정조건을 다시 거치게 ADJUSTMENT_WAIT로 이동
[5] 해지 우선: 종가 < MID면 즉시 해지
[6] MID 기준선: 하한 + (상한-하한)×(2/8~5/8 선택)
[7] 조정 조건(필수): 종가가 MID 초과 AND 조정상단 미만 (1일만 만족해도 OK)
[8] 시그널: 조정 이후에만, high >= 시그널 비율(6/8 또는 7/8)
[9] 매매: 수량 기반 시뮬 (익절1/익절2, 손절 next_open/intraday)
    + 옵션: "익절 트레일링 스탑" (매수 직후부터 트레일링으로 전량 청산)
      - 트레일링 청산 로그에 peak_high, trail_stop 기록(디버깅용)
      - 트레일링 ON이면 tp1/tp2 UI 비활성 처리

실행: streamlit run pattern_scan.py
"""
from __future__ import annotations
import os, math
from datetime import datetime, timedelta
from pathlib import Path

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
    "adj": "#FFB300",
}
PERIOD_OPTIONS = ["6개월","1년","1년6개월","2년","3년","5년","전체"]
PERIOD_MAP = {"6개월":180,"1년":365,"1년6개월":548,"2년":730,"3년":1095,"5년":1825,"전체":0}

# 캐시 설정
CACHE_DIR = Path(".cache") / "pattern_scan"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# 장 마감 추정 + 버퍼
MARKET_CLOSE_HOUR = 15
MARKET_CLOSE_MIN = 30
MARKET_CLOSE_BUFFER_MIN = 20  # 15:50까지는 "장중/정리중"으로 간주

DEFAULT_ADJ_MAX_DAYS = 10

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

def _is_market_intraday_now() -> bool:
    now = datetime.now(core.TZ)
    close_t = now.replace(hour=MARKET_CLOSE_HOUR, minute=MARKET_CLOSE_MIN, second=0, microsecond=0)
    close_t = close_t + timedelta(minutes=MARKET_CLOSE_BUFFER_MIN)
    return now < close_t

def _today_kr() -> str:
    return datetime.now(core.TZ).strftime("%Y-%m-%d")

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
            data = res.get("data") or res
            cap_raw = _first(data, ["mktc","market_cap","시가총액","mkt_cap","tot_mktc"])
            if cap_raw:
                return abs(_to_int(cap_raw))
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
#  OHLCV 캐시 (속도 개선 + 장중 안정화)
# ═══════════════════════════════════════════════════════
def _merge_days(old: list[dict], new: list[dict]) -> list[dict]:
    dedup = {}
    for d in (old or []):
        if d.get("dt"):
            dedup[d["dt"]] = d
    for d in (new or []):
        if d.get("dt"):
            dedup[d["dt"]] = d
    return sorted(dedup.values(), key=lambda x: x["dt"])

def _cache_paths(ticker: str):
    p_parquet = CACHE_DIR / f"ohlcv_{ticker}.parquet"
    p_csv = CACHE_DIR / f"ohlcv_{ticker}.csv"
    return p_parquet, p_csv

def _load_cached_ohlcv(ticker: str) -> list[dict]:
    p_parquet, p_csv = _cache_paths(ticker)
    try:
        if p_parquet.exists():
            df = pd.read_parquet(p_parquet)
            if df.empty: return []
            df["dt"] = df["dt"].astype(str)
            return df.to_dict("records")
    except Exception:
        pass
    try:
        if p_csv.exists():
            df = pd.read_csv(p_csv)
            if df.empty: return []
            df["dt"] = df["dt"].astype(str)
            return df.to_dict("records")
    except Exception:
        pass
    return []

def _save_cached_ohlcv(ticker: str, days: list[dict]) -> None:
    if not days:
        return
    p_parquet, p_csv = _cache_paths(ticker)
    df = pd.DataFrame(days)
    if df.empty:
        return
    try:
        df.to_parquet(p_parquet, index=False)
        return
    except Exception:
        pass
    try:
        df.to_csv(p_csv, index=False, encoding="utf-8-sig")
    except Exception:
        pass

def _fetch_ohlcv_cached(token, ticker, max_pages=40, intraday_refresh_pages=3):
    """
    캐시 전략:
    - 장중/정리중: 오늘 일봉 불완전 가능 → 최근 N페이지만 다시 호출해 merge
    - 장 마감 이후: 캐시 마지막 날짜가 오늘이면 API 재호출 생략
    """
    today = _today_kr()
    intraday = _is_market_intraday_now()

    cached = _load_cached_ohlcv(ticker)
    if cached:
        last_dt = cached[-1]["dt"]

        if intraday:
            fresh = _fetch_ohlcv(token, ticker, max_pages=min(max_pages, intraday_refresh_pages))
            merged = _merge_days(cached, fresh)
            _save_cached_ohlcv(ticker, merged)
            return merged

        if last_dt >= today:
            return cached

        try:
            delta_days = (datetime.strptime(today, "%Y-%m-%d") - datetime.strptime(last_dt, "%Y-%m-%d")).days
        except Exception:
            delta_days = 30
        recent_pages = min(max_pages, max(2, math.ceil((delta_days + 10) / 80)))
        fresh = _fetch_ohlcv(token, ticker, max_pages=recent_pages)
        merged = _merge_days(cached, fresh)
        _save_cached_ohlcv(ticker, merged)
        return merged

    fresh = _fetch_ohlcv(token, ticker, max_pages=max_pages)
    _save_cached_ohlcv(ticker, fresh)
    return fresh

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
#  패턴 탐지 엔진 (조정 필수 + 시그널 선택)
# ═══════════════════════════════════════════════════════
def _detect_pattern(
    days: list[dict],
    vol_multiplier: float,
    mid_ratio: float = 4/8,
    adj_upper_ratio: float = 6/8,
    signal_ratio: float = 7/8,
    max_watch_days: int = 20,
    adj_max_days: int = DEFAULT_ADJ_MAX_DAYS,
    include_bear_high_in_upper: bool = False,
    include_close_up_in_lower_trace: bool = False,
) -> list[dict]:
    """
    상태머신:
    IDLE → TRACKING_HIGH → ADJUSTMENT_WAIT → BREAKOUT_WATCH → SIGNAL/IDLE

    핵심 규칙:
    - 해지: close < mid
    - 조정: (close > mid) AND (close < adj_upper) 하루만 만족해도 OK
    - 시그널: 조정 이후에만 high >= signal_ratio
    - 상한 갱신(절충):
        * close > base_upper 이면 TRACKING_HIGH로 복귀(강세 재돌파)
        * high > base_upper AND close <= base_upper 이면
            - base_upper를 high로 갱신(상한 확대)
            - TRACKING_HIGH로는 가지 않고
            - 조정조건을 다시 거치게 ADJUSTMENT_WAIT로 이동
    """
    closes=[d["close"] for d in days]
    opens=[d["open"] for d in days]
    highs=[d["high"] for d in days]
    lows=[d["low"] for d in days]
    vols=[d["volume"] for d in days]
    ma120=_ma(closes,120)

    patterns=[]
    state="IDLE"

    base_lower=0
    base_upper=0
    peak_high=0

    trigger_idx=0
    trigger_day_count=0
    adj_day_count=0

    def _is_bull(i):
        return closes[i]>=opens[i]

    def _is_upclose(i):
        if i-1 < 0:
            return False
        return closes[i] > closes[i-1]  # 엄격 >

    def _is_uptrend_day(i):
        if _is_bull(i):
            return True
        if include_close_up_in_lower_trace and i >= 1 and _is_upclose(i):
            return True
        return False

    def _calc_lines():
        rng = (base_upper - base_lower)
        mid = base_lower + rng * mid_ratio
        adj_upper = base_lower + rng * adj_upper_ratio
        sig_line = base_lower + rng * signal_ratio
        return mid, adj_upper, sig_line

    def _soft_update_upper(i):
        nonlocal base_upper, adj_day_count, state
        if highs[i] > base_upper and closes[i] <= base_upper:
            base_upper = highs[i]
            adj_day_count = 0
            state = "ADJUSTMENT_WAIT"
            return True
        return False

    for i in range(1, len(days)):
        d=days[i]

        if state=="IDLE":
            if ma120[i] is None:
                continue
            if closes[i] <= ma120[i]*1.02:
                continue

            if not _is_bull(i):
                continue

            prev_vol=vols[i-1]
            if prev_vol<=0:
                continue
            if vols[i] < prev_vol * vol_multiplier:
                continue

            # 트리거 발생
            base_lower = opens[i]

            # 역추적(전일이 상승일이면 시작)
            if i >= 1 and _is_uptrend_day(i-1):
                trace_start = i-1
                for back in range(2, 6):  # 최대 5일
                    if i-back < 0: break
                    if _is_uptrend_day(i-back):
                        trace_start = i-back
                    else:
                        break
                base_lower = opens[trace_start]

            peak_high = highs[i]
            base_upper = 0

            trigger_idx = i
            trigger_day_count = 0
            adj_day_count = 0

            state = "TRACKING_HIGH"
            continue

        # 공통 감시일 카운트
        trigger_day_count += 1
        if trigger_day_count > max_watch_days:
            state = "IDLE"
            continue

        if state == "TRACKING_HIGH":
            if _is_bull(i):
                if highs[i] > peak_high:
                    peak_high = highs[i]
                continue

            # 음봉 → 상한 확정
            base_upper = max(peak_high, highs[i]) if include_bear_high_in_upper else peak_high

            mid, adj_upper, sig_line = _calc_lines()

            if closes[i] < mid:
                state = "IDLE"
                continue

            adj_day_count = 0
            state = "ADJUSTMENT_WAIT"
            continue

        if state == "ADJUSTMENT_WAIT":
            adj_day_count += 1
            mid, adj_upper, sig_line = _calc_lines()

            if closes[i] < mid:
                state = "IDLE"
                continue

            if closes[i] > base_upper:
                peak_high = highs[i]
                state = "TRACKING_HIGH"
                continue

            if _soft_update_upper(i):
                continue

            if adj_day_count > adj_max_days:
                state = "IDLE"
                continue

            # 조정조건: mid < close < adj_upper (하루만 만족하면 OK)
            if (closes[i] > mid) and (closes[i] < adj_upper):
                state = "BREAKOUT_WATCH"
                continue

            continue

        if state == "BREAKOUT_WATCH":
            mid, adj_upper, sig_line = _calc_lines()

            if closes[i] < mid:
                state = "IDLE"
                continue

            if closes[i] > base_upper:
                peak_high = highs[i]
                state = "TRACKING_HIGH"
                continue

            if _soft_update_upper(i):
                continue

            # 시그널: high >= sig_line
            if highs[i] >= sig_line:
                patterns.append({
                    "trigger_idx": trigger_idx,
                    "trigger_dt": days[trigger_idx]["dt"],
                    "signal_idx": i,
                    "signal_dt": d["dt"],
                    "base_lower": base_lower,
                    "base_upper": base_upper,
                    "mid": mid,
                    "adj_upper": adj_upper,
                    "signal_line": sig_line,
                    "trigger_open": opens[trigger_idx],
                    "trigger_close": closes[trigger_idx],
                    "trigger_high": highs[trigger_idx],
                    "trigger_low": lows[trigger_idx],
                    "vol_ratio": vols[trigger_idx]/float(vols[trigger_idx-1]) if vols[trigger_idx-1]>0 else 0,
                })
                state = "IDLE"
                continue

            continue

    return patterns


# ═══════════════════════════════════════════════════════
#  매매 시뮬레이션 (수량 기반 + 익절 트레일링 스탑)
# ═══════════════════════════════════════════════════════
def _simulate(
    days: list[dict],
    patterns: list[dict],
    tp1_pct: float = 20.0,
    tp2_pct: float = 30.0,
    sl_pct: float = 7.0,
    sl_mode: str = "next_open",
    use_profit_trailing_stop: bool = False,
    trail_pct: float = 8.0,
) -> tuple[list[dict], list[dict]]:
    if not patterns:
        return [], []

    trades = []
    trade_log = []

    for pat in patterns:
        sig_idx = pat["signal_idx"]
        buy_idx = sig_idx + 1
        if buy_idx >= len(days):
            continue

        buy_price = days[buy_idx]["open"]
        if buy_price <= 0:
            continue
        buy_dt = days[buy_idx]["dt"]

        tp1_price = buy_price * (1 + tp1_pct / 100.0)
        tp2_price = buy_price * (1 + tp2_pct / 100.0)

        cash = 1.0
        shares = cash / buy_price
        init_shares = shares
        cash = 0.0

        sl_price = buy_price * (1 - sl_pct / 100.0)

        trade_log.append({
            "trigger_dt": pat["trigger_dt"], "signal_dt": pat["signal_dt"],
            "action": "매수", "dt": buy_dt, "price": buy_price, "qty_pct": 100,
        })

        tp1_done = False
        sell_dt = None
        sell_price = 0.0
        sell_type = "보유중"

        peak_high = days[buy_idx]["high"]

        def _qty_pct(sold_shares: float) -> float:
            if init_shares <= 0:
                return 0.0
            return max(0.0, min(100.0, sold_shares / init_shares * 100.0))

        for j in range(buy_idx + 1, len(days)):
            dj = days[j]
            h, l, c, o = dj["high"], dj["low"], dj["close"], dj["open"]

            if h > peak_high:
                peak_high = h

            if use_profit_trailing_stop and shares > 0:
                trail_stop = peak_high * (1 - trail_pct / 100.0)
                if l <= trail_stop:
                    exec_price = o if (o > 0 and o < trail_stop) else trail_stop
                    sold = shares
                    cash += sold * exec_price

                    trade_log.append({
                        "trigger_dt": pat["trigger_dt"], "signal_dt": pat["signal_dt"],
                        "action": f"익절TS({trail_pct:.1f}%)",
                        "dt": dj["dt"], "price": exec_price,
                        "qty_pct": _qty_pct(sold),
                        "peak_high": peak_high,
                        "trail_stop": trail_stop,
                    })

                    sell_type = "익절TS(전량)"
                    sell_dt = dj["dt"]
                    sell_price = exec_price
                    shares = 0.0
                    break

            if shares > 0 and (not use_profit_trailing_stop) and sl_mode == "intraday":
                if l <= sl_price:
                    exec_price = o if (o > 0 and o < sl_price) else sl_price
                    sold = shares
                    cash += sold * exec_price
                    trade_log.append({
                        "trigger_dt": pat["trigger_dt"], "signal_dt": pat["signal_dt"],
                        "action": "손절", "dt": dj["dt"], "price": exec_price,
                        "qty_pct": _qty_pct(sold),
                    })
                    sell_type = "손절(즉시)"
                    sell_dt = dj["dt"]
                    sell_price = exec_price
                    shares = 0.0
                    break

            if shares > 0 and (not use_profit_trailing_stop) and sl_mode == "next_open":
                prev_close = days[j - 1]["close"] if j - 1 >= buy_idx else buy_price
                if prev_close <= sl_price:
                    exec_price = o
                    sold = shares
                    cash += sold * exec_price
                    trade_log.append({
                        "trigger_dt": pat["trigger_dt"], "signal_dt": pat["signal_dt"],
                        "action": "손절", "dt": dj["dt"], "price": exec_price,
                        "qty_pct": _qty_pct(sold),
                    })
                    sell_type = "손절(시가)"
                    sell_dt = dj["dt"]
                    sell_price = exec_price
                    shares = 0.0
                    break

            if shares > 0 and (not tp1_done) and h >= tp1_price:
                sold = shares * 0.5
                cash += sold * tp1_price
                shares -= sold
                tp1_done = True
                trade_log.append({
                    "trigger_dt": pat["trigger_dt"], "signal_dt": pat["signal_dt"],
                    "action": f"익절1({tp1_pct:.0f}%)", "dt": dj["dt"],
                    "price": tp1_price, "qty_pct": _qty_pct(sold),
                })
                if shares > 0 and h >= tp2_price:
                    sold2 = shares
                    cash += sold2 * tp2_price
                    shares = 0.0
                    trade_log.append({
                        "trigger_dt": pat["trigger_dt"], "signal_dt": pat["signal_dt"],
                        "action": f"익절2({tp2_pct:.0f}%)", "dt": dj["dt"],
                        "price": tp2_price, "qty_pct": _qty_pct(sold2),
                    })
                    sell_type = "익절(전량)"
                    sell_dt = dj["dt"]
                    sell_price = tp2_price
                    break
                continue

            if shares > 0 and tp1_done and h >= tp2_price:
                sold2 = shares
                cash += sold2 * tp2_price
                shares = 0.0
                trade_log.append({
                    "trigger_dt": pat["trigger_dt"], "signal_dt": pat["signal_dt"],
                    "action": f"익절2({tp2_pct:.0f}%)", "dt": dj["dt"],
                    "price": tp2_price, "qty_pct": _qty_pct(sold2),
                })
                sell_type = "익절(전량)"
                sell_dt = dj["dt"]
                sell_price = tp2_price
                break

        if sell_dt is None:
            last = days[-1]
            final_value = cash + shares * last["close"]
            sell_dt = last["dt"]
            sell_price = last["close"]
            sell_type = "TS보유중" if use_profit_trailing_stop else ("보유중" if not tp1_done else "익절1+보유중")
        else:
            final_value = cash

        roi_pct = (final_value / 1.0 - 1.0) * 100.0

        trades.append({
            "trigger_dt": pat["trigger_dt"],
            "signal_dt": pat["signal_dt"],
            "buy_dt": buy_dt, "buy_price": buy_price,
            "sell_dt": sell_dt, "sell_price": sell_price,
            "sell_type": sell_type,
            "roi_pct": roi_pct,
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

    dt_=df[df["is_trig"]]
    if not dt_.empty:
        fig.add_trace(go.Scatter(x=dt_["dt"],y=dt_["high"]*1.04,mode="markers",name="★ 트리거",
            marker=dict(symbol="star",size=14,color=COLORS["trigger"],line=dict(width=1,color="#F9A825")),
            hovertemplate="%{x|%Y-%m-%d}<br><b>★ 트리거</b><extra></extra>"),row=1,col=1)

    sig_entries=[p for p in patterns if p["signal_dt"] in set(ds)]
    if sig_entries:
        sig_dates=pd.to_datetime([p["signal_dt"] for p in sig_entries])
        sig_prices=[p["signal_line"] for p in sig_entries]
        fig.add_trace(go.Scatter(x=sig_dates,y=[p*1.02 for p in sig_prices],
            mode="markers",name="◆ 시그널",
            marker=dict(symbol="diamond",size=13,color=COLORS["signal"],line=dict(width=1.5,color="#FFF")),
            hovertemplate="%{x|%Y-%m-%d}<br><b>◆ 시그널</b><extra></extra>"),row=1,col=1)

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

    for pat in patterns:
        dt_range=df[(df["dt"]>=pd.Timestamp(pat["trigger_dt"]))&(df["dt"]<=pd.Timestamp(pat["signal_dt"])+timedelta(days=5))]
        if dt_range.empty: continue
        x0,x1=dt_range["dt"].iloc[0],dt_range["dt"].iloc[-1]
        for val,color,dash,lbl in [
            (pat["base_upper"],COLORS["upper"],"dot","상한"),
            (pat["base_lower"],COLORS["lower"],"dot","하한"),
            (pat["mid"],COLORS["mid"],"dashdot","MID"),
            (pat.get("adj_upper", None),COLORS["adj"],"dash","조정상단"),
            (pat["signal_line"],COLORS["signal"],"dash","시그널"),]:
            if val is None:
                continue
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
        ts_logs = [e for e in tlog if "익절TS" in (e.get("action") or "")]
        if ts_logs:
            with st.expander("🧪 트레일링 디버그 로그(peak_high / trail_stop)"):
                st.dataframe(pd.DataFrame(ts_logs), use_container_width=True, hide_index=True)
    elif patterns:
        st.success(f"패턴 {len(patterns)}개 탐지 (매매 시그널 발생)")
    else:
        st.info("패턴 미탐지")

def _render_multi(results,pd_):
    ov=[]
    for it in results:
        if it.get("error"): continue
        s=_calc_stats(it["trades"])
        if s["total"]==0 and not it.get("patterns"): continue
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
        min_cap=st.number_input("최소 시총(억원)",100,100000,1000,100, help="1000 = 1,000억원")

        # ✅ (변경) 시그널 비율 선택: 7/8 or 6/8
        signal_ratio_label = st.selectbox("시그널 비율(선택)", ["7/8", "6/8"], index=0)
        signal_ratio = {"7/8":7/8, "6/8":6/8}[signal_ratio_label]

        mid_ratio_label=st.selectbox("MID(해지 기준) 비율",["2/8","3/8","4/8","5/8"],index=2)
        mid_ratio={"2/8":2/8,"3/8":3/8,"4/8":4/8,"5/8":5/8}[mid_ratio_label]

        # ✅ (변경) 시그널 7/8일 때만 조정상단 5/8,6/8 선택
        # ✅ 시그널 6/8이면 조정상단 자동 5/8 고정
        if signal_ratio_label == "7/8":
            adj_upper_label = st.selectbox("조정 상단 비율(선택)", ["6/8","5/8"], index=0)
            adj_upper_ratio = {"5/8":5/8, "6/8":6/8}[adj_upper_label]
        else:
            # signal = 6/8 -> adj_upper = 5/8 고정
            adj_upper_label = "5/8(자동)"
            adj_upper_ratio = 5/8
            st.selectbox("조정 상단 비율(자동)", ["5/8"], index=0, disabled=True)
            st.info("ℹ️ 시그널을 6/8로 선택하면 조정상단은 자동으로 5/8로 고정됩니다.")

        max_watch=st.slider("최대 감시일(거래일)",5,40,20)
        adj_max_days = st.slider("조정 대기 최대일(거래일)", 1, 30, DEFAULT_ADJ_MAX_DAYS)

        include_bear_high_in_upper = st.checkbox("상한가 확정 시 음봉 당일 고가도 후보로 포함", False)

        include_close_up_in_lower_trace = st.checkbox(
            "하한 역추적에 '종가상승(전일 종가 대비)'도 연속으로 인정",
            False,
            help="음봉이어도 종가가 전일보다 높으면 '상승일'로 보고 하한 역추적 구간에 포함합니다."
        )

        vol_override=st.checkbox("거래량 배수 직접 입력",False)
        if vol_override:
            vol_mult_manual=st.slider("거래량 배수",1.5,10.0,3.0,0.5)
        else:
            vol_mult_manual=None

        st.markdown("---")
        st.markdown("##### 💰 매매 설정")

        use_profit_trailing_stop = st.checkbox(
            "익절 트레일링 스탑",
            False,
            help="매수 직후부터 최고가 기준으로 트레일링 스탑을 적용해, 터치 시 전량 매도합니다."
        )
        if use_profit_trailing_stop:
            trail_pct = st.slider("트레일링 폭(%)", 2.0, 30.0, 8.0, 0.5)
            st.info("ℹ️ 익절 트레일링 ON 상태에서는 tp1/tp2 부분익절을 비활성 처리했습니다. (전량 트레일링 청산 중심)")
        else:
            trail_pct = 8.0

        tp1=st.slider("1차 익절(%)",5.0,50.0,20.0,1.0,help="→ 50% 매도", disabled=use_profit_trailing_stop)
        tp2=st.slider("2차 익절(%)",10.0,100.0,30.0,1.0,help="→ 전량 매도", disabled=use_profit_trailing_stop)

        sl_mode=st.radio("손절 방식",["다음날 시가","장중 즉시"],horizontal=True, disabled=use_profit_trailing_stop)
        sl_pct=st.slider("손절(%)",1.0,20.0,7.0,0.5, disabled=use_profit_trailing_stop)

        intraday_refresh_pages = st.slider(
            "장중 캐시 갱신(최근 페이지)", 1, 10, 3, 1,
            help="장중에는 오늘 일봉이 불완전할 수 있어 최근 일부를 재조회해 캐시를 갱신합니다."
        )
        max_pages=st.number_input("API 페이지",5,100,40,5)

        run=st.button("🔍 스캔 실행",use_container_width=True,type="primary")

        st.markdown("---")
        st.markdown(f"""<div style="font-size:.73rem;color:#888;line-height:1.8">
        <b style="color:#FFD600">★</b> 트리거: 양봉+거래량폭증+MA120위<br>
        <b style="color:#FF5252">━</b> 상한
        <b style="color:#448AFF">━</b> 하한
        <b style="color:#AB47BC">━</b> MID(해지)<br>
        <b style="color:#FFB300">━</b> 조정상단({adj_upper_label})<br>
        <b style="color:#00E676">◆</b> 시그널({signal_ratio_label}): 조정 후 high ≥ 시그널선<br>
        </div>""",unsafe_allow_html=True)

    st.markdown(
        "<h2 style='margin-bottom:0'>🔍 거래량 돌파 패턴 스캔</h2>"
        "<p style='color:#888;margin-top:4px'>트리거→상한확정→조정(필수)→시그널→매매 시뮬</p>",
        unsafe_allow_html=True
    )

    if "scan_results" not in st.session_state: st.session_state.scan_results=None
    if "scan_mode" not in st.session_state: st.session_state.scan_mode=None
    if "scan_pd" not in st.session_state: st.session_state.scan_pd=0

    if run:
        if (not use_profit_trailing_stop) and (tp2<=tp1):
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
                if _is_market_intraday_now():
                    st.info("🕒 현재 장중/정리중으로 판단되어 캐시는 최근 데이터 일부를 재조회하여 갱신합니다.")

                with st.spinner(f"{name} 데이터 조회(캐시)..."):
                    days=_fetch_ohlcv_cached(token,ticker,max_pages, intraday_refresh_pages=intraday_refresh_pages)

                with st.spinner("패턴 분석..."):
                    patterns=_detect_pattern(
                        days, vm,
                        mid_ratio=mid_ratio,
                        adj_upper_ratio=adj_upper_ratio,
                        signal_ratio=signal_ratio,
                        max_watch_days=max_watch,
                        adj_max_days=adj_max_days,
                        include_bear_high_in_upper=include_bear_high_in_upper,
                        include_close_up_in_lower_trace=include_close_up_in_lower_trace,
                    )
                    trades,tlog=_simulate(
                        days, patterns,
                        tp1_pct=tp1, tp2_pct=tp2,
                        sl_pct=sl_pct, sl_mode=sl_m,
                        use_profit_trailing_stop=use_profit_trailing_stop,
                        trail_pct=trail_pct,
                    )
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

            if _is_market_intraday_now():
                st.info("🕒 현재 장중/정리중으로 판단되어 캐시는 최근 데이터 일부를 재조회하여 갱신합니다.")

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

                    days=_fetch_ohlcv_cached(token,tk,max_pages, intraday_refresh_pages=intraday_refresh_pages)

                    pats=_detect_pattern(
                        days, vm,
                        mid_ratio=mid_ratio,
                        adj_upper_ratio=adj_upper_ratio,
                        signal_ratio=signal_ratio,
                        max_watch_days=max_watch,
                        adj_max_days=adj_max_days,
                        include_bear_high_in_upper=include_bear_high_in_upper,
                        include_close_up_in_lower_trace=include_close_up_in_lower_trace,
                    )
                    trs,tl=_simulate(
                        days, pats,
                        tp1_pct=tp1, tp2_pct=tp2,
                        sl_pct=sl_pct, sl_mode=sl_m,
                        use_profit_trailing_stop=use_profit_trailing_stop,
                        trail_pct=trail_pct,
                    )
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
**[트리거]** 양봉 + 거래량 폭증 + MA120×1.02 상회  
**[상한 확정]** 양봉 고가 추적 → 첫 음봉에서 base_upper 확정(옵션 시 음봉고가 포함)  
**[해지]** 종가 < MID이면 즉시 해지  
**[조정(필수)]** 종가가 MID 초과 & 조정상단 미만을 1일이라도 만족  
**[시그널]** 조정 이후에만, 고가가 시그널 비율(6/8 또는 7/8) 라인 이상이면 시그널  
**[상한 갱신(절충)]**  
- close > base_upper → TRACKING_HIGH(강세 재돌파)  
- high > base_upper & close <= base_upper → 상한만 갱신 + 조정 다시  
""")


if __name__=="__main__":
    main()