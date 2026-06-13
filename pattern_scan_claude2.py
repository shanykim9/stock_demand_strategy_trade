"""
과거차트 시그널 기반 매매 시뮬레이션 — pattern_scan2.py
═══════════════════════════════════════════════════════════
[사전필터] 시총 ≥ 1000억, 종가 > 선택MA × 1.02
[1] 트리거: 양봉 + 시총연동 거래량 폭증 + MA120/240 선택
[2] 트리거 유효성: 설정기간 내 최고 종가여야 유효
[3] 기준하한가: 연속양봉 역추적(최대5일)
[4] 기준상한가: 양봉→최고가 추적, 음봉→확정
[5] 과거차트 시그널 횟수: 설정기간 내 (종가≥트리거종가×임계% AND 거래량폭증) 카운트
[6] 시그널: 과거차트 시그널 횟수 ≥ 3 → 발생
[7] 매수 경로A: 횟수 티어(3/5/7/9)별 분할매수 (4단계 or 8단계)
    매수 경로B: 분할매수 미체결 + 상한돌파 양봉 → 다음날 시가 매수 + 트레일링7%
[8] 매도: 평균매수가 기준 티어별 익절률, 즉시 손절
[9] 같은 날 매수+매도 동시 처리 금지 (매도 우선)

실행: streamlit run pattern_scan2.py
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
#  상수
# ═══════════════════════════════════════════════════════
COLORS = {
    "bull": "#E53935", "bear": "#1E88E5",
    "bull_vol": "#EF9A9A", "bear_vol": "#90CAF9",
    "trigger": "#FFD600", "trigger_vol": "#FFD600",
    "upper": "#FF5252", "lower": "#448AFF", "mid": "#AB47BC",
    "signal": "#00E676", "cancel": "#F44336",
    "buy1": "#00E676", "buy2": "#76FF03", "buy3": "#FFEB3B",
    "sell_tp": "#2196F3", "sell_sl": "#F44336", "sell_trail": "#00BCD4",
    "ma120": "#26A69A", "ma240": "#FF7043",
    "bg": "#131722", "grid": "#1E222D", "text": "#D1D4DC",
}
PERIOD_OPTIONS = ["6개월", "1년", "1년6개월", "2년", "3년", "5년", "전체"]
PERIOD_MAP = {"6개월": 180, "1년": 365, "1년6개월": 548, "2년": 730, "3년": 1095, "5년": 1825, "전체": 0}
LOOKBACK_OPTIONS = ["6개월", "1년", "1년6개월", "2년", "3년"]
LOOKBACK_MAP = {"6개월": 180, "1년": 365, "1년6개월": 548, "2년": 730, "3년": 1095}

CACHE_DIR = Path(".cache") / "pattern_scan2"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

MARKET_CLOSE_HOUR = 15
MARKET_CLOSE_MIN = 30
MARKET_CLOSE_BUFFER_MIN = 20

# ═══════════════════════════════════════════════════════
#  유틸 (기존 재사용)
# ═══════════════════════════════════════════════════════
def _to_int(v, default=0) -> int:
    if v is None: return default
    if isinstance(v, int): return v
    s = str(v).strip().replace(",", "").replace("+", "").replace("-", "", 1)
    s = s.strip().replace(",", "")
    if not s: return default
    try: return int(float(s))
    except: return default

def _parse_dt(v) -> str | None:
    if v is None: return None
    s = str(v).strip()
    if len(s) >= 8 and s[:8].isdigit(): return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    if len(s) >= 10 and s[4] == "-" and s[7] == "-": return s[:10]
    return None

def _first(row, keys):
    for k in keys:
        if k in row and str(row.get(k)).strip() != "": return row.get(k)
    return None

def _resolve(q):
    q = (q or "").strip()
    if not q: raise RuntimeError("종목명 입력 필요")
    if q.isdigit() and len(q) == 6: ticker = q
    else:
        ticker, err = core.resolve_ticker(q)
        if err or not ticker: raise RuntimeError(err or f"'{q}' 확인 필요")
    name = ticker
    try: name = (core._krx_cache.get("name_by_code") or {}).get(ticker, ticker)
    except: pass
    return ticker, name

def _parse_file(content):
    out = []
    for p in content.replace("\n", ",").replace("\r", ",").split(","):
        t = p.strip()
        if t: out.append(t)
    return out

def _is_market_intraday_now() -> bool:
    now = datetime.now(core.TZ)
    close_t = now.replace(hour=MARKET_CLOSE_HOUR, minute=MARKET_CLOSE_MIN, second=0)
    close_t = close_t + timedelta(minutes=MARKET_CLOSE_BUFFER_MIN)
    return now < close_t

def _today_kr() -> str:
    return datetime.now(core.TZ).strftime("%Y-%m-%d")

# ═══════════════════════════════════════════════════════
#  시총 / 거래량 (기존 재사용)
# ═══════════════════════════════════════════════════════
def _get_market_cap(token: str, ticker: str) -> float:
    try:
        body = {"stk_cd": ticker}
        res = core.call_tr_all_pages(token=token, api_id="ka10001", body=body,
                                     endpoint="/api/dostk/stkinfo", max_pages=1)
        rows = res.get("rows") or []
        data = rows[0] if rows else (res.get("data") or res)
        cap_raw = _first(data, ["mktc", "market_cap", "시가총액", "mkt_cap", "tot_mktc"])
        if cap_raw: return abs(_to_int(cap_raw))
    except: pass
    return 0.0

def _calc_volume_pct(cap_억: float) -> float:
    if cap_억 <= 1000: return 400.0
    if cap_억 >= 30000: return 100.0
    return 400.0 - (cap_억 - 1000.0) * 300.0 / 29000.0

def _volume_multiplier(cap_억: float) -> float:
    return 1.0 + _calc_volume_pct(cap_억) / 100.0

# ═══════════════════════════════════════════════════════
#  OHLCV 조회 + 캐시 (기존 재사용)
# ═══════════════════════════════════════════════════════
def _fetch_ohlcv(token, ticker, max_pages=40):
    end_dt = datetime.now(core.TZ).strftime("%Y%m%d")
    stex = (os.getenv("KIWOOM_DMST_STEX_TP") or "KRX").strip().upper()
    upd = (os.getenv("KIWOOM_OHLCV_UPD_STKPC_TP") or "1").strip()
    common = {"stk_cd": ticker, "stex_tp": stex, "dmst_stex_tp": stex}
    bodies = [
        {**common, "base_dt": end_dt, "upd_stkpc_tp": upd},
        {**common, "base_dt": end_dt},
        {**common, "dt": end_dt, "upd_stkpc_tp": upd},
        {**common, "dt": end_dt},
    ]
    last_err = None
    for body in bodies:
        try:
            res = core.call_tr_all_pages(token=token, api_id="ka10081", body=body,
                                         endpoint="/api/dostk/chart", max_pages=max_pages)
            rows = res.get("rows") or []
            if not rows: continue
            dedup = {}
            for r in rows:
                dt = _parse_dt(_first(r, ["dt", "date", "bas_dt", "base_dt", "trde_dt", "trd_dt"]))
                if not dt: continue
                op = _to_int(_first(r, ["open_pric", "open", "stck_oprc", "opn_prc"]), 0)
                hp = _to_int(_first(r, ["high_pric", "high", "stck_hgpr", "hgh_prc"]), 0)
                lp = _to_int(_first(r, ["low_pric", "low", "stck_lwpr", "low_prc"]), 0)
                cp = _to_int(_first(r, ["close_pric", "close", "stck_clpr", "cur_prc", "cur_pric"]), 0)
                vol = _to_int(_first(r, ["trde_qty", "volume", "acml_vol", "acc_trde_qty"]), 0)
                if cp <= 0: continue
                if op <= 0: op = cp
                if hp <= 0: hp = max(op, cp)
                if lp <= 0: lp = min(op, cp)
                dedup[dt] = {"dt": dt, "open": op, "high": hp, "low": lp, "close": cp, "volume": max(0, vol)}
            out = sorted(dedup.values(), key=lambda x: x["dt"])
            if out: return out
        except Exception as e:
            last_err = e; continue
    raise RuntimeError(f"일봉 조회 실패: {last_err}")

def _merge_days(old, new):
    dedup = {}
    for d in (old or []): dedup[d["dt"]] = d
    for d in (new or []): dedup[d["dt"]] = d
    return sorted(dedup.values(), key=lambda x: x["dt"])

def _cache_paths(ticker):
    return CACHE_DIR / f"ohlcv_{ticker}.parquet", CACHE_DIR / f"ohlcv_{ticker}.csv"

def _load_cached(ticker):
    pp, pc = _cache_paths(ticker)
    for p, reader in [(pp, lambda p: pd.read_parquet(p)), (pc, lambda p: pd.read_csv(p))]:
        try:
            if p.exists():
                df = reader(p)
                if not df.empty:
                    df["dt"] = df["dt"].astype(str)
                    return df.to_dict("records")
        except: pass
    return []

def _save_cached(ticker, days):
    if not days: return
    pp, pc = _cache_paths(ticker)
    df = pd.DataFrame(days)
    try: df.to_parquet(pp, index=False); return
    except: pass
    try: df.to_csv(pc, index=False, encoding="utf-8-sig")
    except: pass

def _fetch_ohlcv_cached(token, ticker, max_pages=40, intraday_pages=3):
    today = _today_kr()
    intraday = _is_market_intraday_now()
    cached = _load_cached(ticker)
    if cached:
        last_dt = cached[-1]["dt"]
        if intraday:
            fresh = _fetch_ohlcv(token, ticker, min(max_pages, intraday_pages))
            merged = _merge_days(cached, fresh)
            _save_cached(ticker, merged); return merged
        if last_dt >= today: return cached
        try: delta = (datetime.strptime(today, "%Y-%m-%d") - datetime.strptime(last_dt, "%Y-%m-%d")).days
        except: delta = 30
        pages = min(max_pages, max(2, math.ceil((delta + 10) / 80)))
        fresh = _fetch_ohlcv(token, ticker, pages)
        merged = _merge_days(cached, fresh)
        _save_cached(ticker, merged); return merged
    fresh = _fetch_ohlcv(token, ticker, max_pages)
    _save_cached(ticker, fresh); return fresh

# ═══════════════════════════════════════════════════════
#  MA 계산
# ═══════════════════════════════════════════════════════
def _ma(values, w):
    out = [None] * len(values)
    if w <= 0: return out
    s = 0.0
    for i, v in enumerate(values):
        s += float(v)
        if i >= w: s -= float(values[i - w])
        if i >= w - 1: out[i] = s / float(w)
    return out

# ═══════════════════════════════════════════════════════
#  트리거 탐지 + 과거차트 시그널 횟수 + 기준가 확정
# ═══════════════════════════════════════════════════════
def _is_bull(closes, opens, i):
    return closes[i] >= opens[i]

def _count_past_signals(days, trigger_idx, lookback_days, threshold_pct, vol_multiplier):
    """
    설정기간 내 (종가 ≥ 트리거종가 × threshold% AND 거래량 폭증) 횟수
    """
    trigger_close = days[trigger_idx]["close"]
    target_price = trigger_close * threshold_pct / 100.0
    lb_start = (datetime.strptime(days[trigger_idx]["dt"], "%Y-%m-%d")
                - timedelta(days=lookback_days)).strftime("%Y-%m-%d")

    count = 0
    for j in range(trigger_idx):  # 트리거 이전만
        if days[j]["dt"] < lb_start: continue
        if days[j]["close"] >= target_price:
            if j >= 1 and days[j - 1]["volume"] > 0:
                if days[j]["volume"] >= days[j - 1]["volume"] * vol_multiplier:
                    count += 1
    return count

def _is_highest_close(days, trigger_idx, lookback_days):
    """설정기간 내 트리거봉 종가가 최고 종가인지"""
    tc = days[trigger_idx]["close"]
    lb_start = (datetime.strptime(days[trigger_idx]["dt"], "%Y-%m-%d")
                - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    for j in range(trigger_idx):
        if days[j]["dt"] < lb_start: continue
        if days[j]["close"] >= tc:
            return False
    return True

def _detect_all(
    days: list[dict],
    vol_multiplier: float,
    ma_period: int,
    lookback_days: int,
    threshold_pct: float,
    min_signal_count: int = 3,
) -> list[dict]:
    """
    전체 탐지 파이프라인:
    1) 트리거: 양봉 + 거래량 + MA위
    2) 유효성: 설정기간 내 최고종가
    3) 기준하한가: 역추적
    4) 기준상한가: 양봉추적 → 음봉확정
    5) 과거차트 시그널 횟수
    6) 횟수 ≥ 3 → 시그널
    """
    closes = [d["close"] for d in days]
    opens = [d["open"] for d in days]
    highs = [d["high"] for d in days]
    lows = [d["low"] for d in days]
    vols = [d["volume"] for d in days]
    ma_vals = _ma(closes, ma_period)

    results = []

    # 상태머신
    state = "IDLE"
    trigger_idx = 0
    base_lower = 0
    peak_high = 0
    peak_close = 0  # peak 양봉의 종가 (경로B 돌파 기준)
    past_sig_count = 0

    for i in range(1, len(days)):
        if state == "IDLE":
            # MA 필터
            if ma_vals[i] is None: continue
            if closes[i] <= ma_vals[i] * 1.02: continue
            # 양봉
            if not _is_bull(closes, opens, i): continue
            # 거래량
            if vols[i - 1] <= 0: continue
            if vols[i] < vols[i - 1] * vol_multiplier: continue
            # 최고종가 검증
            if not _is_highest_close(days, i, lookback_days): continue

            # ── 트리거 발생 ──
            trigger_idx = i

            # 기준하한가: 연속 양봉 역추적 (최대5일)
            base_lower = opens[i]
            if i >= 1 and _is_bull(closes, opens, i - 1):
                trace = i - 1
                for back in range(2, 6):
                    if i - back < 0: break
                    if _is_bull(closes, opens, i - back):
                        trace = i - back
                    else: break
                base_lower = opens[trace]

            # 과거차트 시그널 횟수 (트리거 시점에 이미 계산 가능)
            past_sig_count = _count_past_signals(days, i, lookback_days, threshold_pct, vol_multiplier)

            # 횟수 부족 → 무시
            if past_sig_count < min_signal_count:
                continue

            peak_high = highs[i]
            peak_close = closes[i]  # 트리거봉 종가부터 시작
            state = "TRACKING_HIGH"
            continue

        if state == "TRACKING_HIGH":
            # 양봉/도지 → 고가 추적
            if _is_bull(closes, opens, i):
                if highs[i] > peak_high:
                    peak_high = highs[i]
                    peak_close = closes[i]  # 최고가 갱신 시 그 봉의 종가 기록
                continue
            # 음봉 → 상한가 확정!
            base_upper = peak_high

            # 유효성 체크: 상한 > 하한
            if base_upper <= base_lower:
                state = "IDLE"; continue

            results.append({
                "trigger_idx": trigger_idx,
                "trigger_dt": days[trigger_idx]["dt"],
                "signal_idx": i,  # 상한 확정일 = 시그널 발생일
                "signal_dt": days[i]["dt"],
                "base_lower": base_lower,
                "base_upper": base_upper,
                "peak_close": peak_close,  # peak 양봉 종가 (경로B 돌파 기준)
                "trigger_close": closes[trigger_idx],
                "trigger_open": opens[trigger_idx],
                "trigger_high": highs[trigger_idx],
                "trigger_low": lows[trigger_idx],
                "vol_ratio": vols[trigger_idx] / float(vols[trigger_idx - 1]) if vols[trigger_idx - 1] > 0 else 0,
                "past_sig_count": past_sig_count,
            })
            state = "IDLE"
            continue

    return results


# ═══════════════════════════════════════════════════════
#  티어 결정 + 매수/매도 레벨 계산
# ═══════════════════════════════════════════════════════
def _get_tier(count: int) -> int:
    """횟수 → 티어 (하한 기준 적용)"""
    if count >= 9: return 9
    if count >= 7: return 7
    if count >= 5: return 5
    if count >= 3: return 3
    return 0

TIER_TP_PCT = {3: 5.0, 5: 7.5, 7: 10.0, 9: 12.5}

def _calc_levels(tier: int, base_lower: float, base_upper: float, steps: int):
    """
    매수 가격, 손절가 계산.
    Returns: buy_levels [(price, ratio), ...], sl_price
      ratio: 금액 배수 (1=기본단위, 2=2배, 4=4배)
      price=None이면 '다음날 시가' 매수 (tier9 1차)
    """
    rng = base_upper - base_lower

    def _p(n): return base_lower + rng * n / steps

    if steps == 4:
        if tier == 3:
            buys = [(_p(2), 1), (_p(1), 2), (base_lower, 4)]
            sl = base_lower * 0.95
        elif tier == 5:
            buys = [(_p(3), 1), (_p(2), 2), (_p(1), 4)]
            sl = base_lower
        elif tier == 7:
            buys = [(base_upper, 1), (_p(3), 2), (_p(2), 4)]
            sl = _p(1)
        elif tier == 9:
            buys = [(None, 1), (base_upper, 2), (_p(3), 4)]  # None = 다음날 시가
            sl = _p(2)
        else:
            return [], 0
    elif steps == 8:
        if tier == 3:
            buys = [(_p(3), 1), (_p(2), 2), (_p(1), 4)]
            sl = base_lower * 0.95
        elif tier == 5:
            buys = [(_p(4), 1), (_p(3), 2), (_p(2), 4)]
            sl = base_lower
        elif tier == 7:
            buys = [(_p(5), 1), (_p(4), 2), (_p(3), 4)]
            sl = _p(1)
        elif tier == 9:
            buys = [(_p(7), 1), (_p(5), 2), (_p(4), 4)]
            sl = _p(2)
        else:
            return [], 0
    else:
        return [], 0

    return buys, sl


# ═══════════════════════════════════════════════════════
#  매매 시뮬레이션 (같은 날 매수+매도 금지 / 경로B 트레일링)
# ═══════════════════════════════════════════════════════
def _simulate(
    days: list[dict],
    detections: list[dict],
    steps: int,
    base_unit: int,
    trail_pct: float = 7.0,
) -> tuple[list[dict], list[dict]]:
    """
    매매 시뮬레이션.
    
    ■ 같은 날 규칙:
      - 보유 중이면 → 매도(손절/익절) 우선 체크, 매도 발생 시 그날 매수 안 함
      - 보유 0주면 → 매수만 체크
      
    ■ 경로A: 분할 지정가 매수 → 평균매수가 기준 익절/손절
    ■ 경로B: 분할매수 미체결 + 상한돌파 양봉 → 다음날 시가 매수 + 트레일링7%
    """
    if not detections: return [], []

    trades = []
    trade_log = []

    for det in detections:
        sig_idx = det["signal_idx"]
        tier = _get_tier(det["past_sig_count"])
        if tier == 0: continue

        buy_levels, sl_price = _calc_levels(tier, det["base_lower"], det["base_upper"], steps)
        if not buy_levels: continue

        tp_pct = TIER_TP_PCT.get(tier, 5.0)
        base_upper = det["base_upper"]
        breakout_close = det.get("peak_close", base_upper)  # peak 양봉 종가 (경로B 기준)

        # 매수 주문 준비
        orders = []
        for idx, (price, ratio) in enumerate(buy_levels):
            amt = base_unit * ratio
            orders.append({
                "target": price,  # None = 다음날 시가 (tier9 1차)
                "amount": amt, "ratio": ratio,
                "filled": False, "fill_price": 0, "fill_qty": 0, "fill_dt": "",
                "label": f"{idx+1}차",
            })

        total_cost = 0.0
        total_qty = 0
        sell_dt = None
        sell_price = 0.0
        sell_type = "보유중"
        roi_pct = 0.0
        sell_amount = 0.0

        # 경로B 상태
        path_b_active = False
        path_b_buy_next_open = False  # 다음날 시가 매수 예약
        path_b_bought = False
        path_b_buy_price = 0.0
        path_b_qty = 0
        path_b_cost = 0.0
        trail_peak = 0.0

        start_idx = sig_idx + 1
        if start_idx >= len(days): continue

        for j in range(start_idx, len(days)):
            dj = days[j]
            o, h, l, c = dj["open"], dj["high"], dj["low"], dj["close"]

            # ═══════════════════════════════════════
            #  경로B: 다음날 시가 매수 실행
            # ═══════════════════════════════════════
            if path_b_buy_next_open and not path_b_bought:
                # 전체 투자금 = base_unit × (1+2+4) = 7배
                total_invest = base_unit * 7
                path_b_buy_price = o
                path_b_qty = int(total_invest / o) if o > 0 else 0
                if path_b_qty > 0:
                    path_b_cost = path_b_buy_price * path_b_qty
                    path_b_bought = True
                    trail_peak = h
                    trade_log.append({
                        "trigger_dt": det["trigger_dt"], "signal_dt": det["signal_dt"],
                        "action": "매수(돌파)", "dt": dj["dt"],
                        "price": o, "qty": path_b_qty, "amount": path_b_cost,
                        "tier": tier, "past_count": det["past_sig_count"],
                    })
                    # 매수한 날은 매도 안 함 (같은 날 규칙)
                    continue
                else:
                    path_b_buy_next_open = False
                    path_b_active = False

            # ═══════════════════════════════════════
            #  경로B: 트레일링 스탑 매도
            # ═══════════════════════════════════════
            if path_b_bought:
                if h > trail_peak:
                    trail_peak = h
                trail_stop = trail_peak * (1 - trail_pct / 100.0)
                if l <= trail_stop:
                    exec_p = min(o, trail_stop) if o > 0 else trail_stop
                    sell_amount = exec_p * path_b_qty
                    sell_dt = dj["dt"]
                    sell_price = exec_p
                    sell_type = f"트레일링({trail_pct:.0f}%)"
                    roi_pct = (exec_p / path_b_buy_price - 1) * 100.0
                    total_cost = path_b_cost
                    total_qty = path_b_qty
                    trade_log.append({
                        "trigger_dt": det["trigger_dt"], "signal_dt": det["signal_dt"],
                        "action": f"트레일링({trail_pct:.0f}%)", "dt": dj["dt"],
                        "price": exec_p, "qty": path_b_qty, "amount": sell_amount,
                        "tier": tier, "past_count": det["past_sig_count"],
                        "trail_peak": trail_peak, "trail_stop": trail_stop,
                    })
                    break
                continue  # 경로B 보유중이면 다른 로직 스킵

            # ═══════════════════════════════════════
            #  경로A: 보유중이면 → 매도 우선 체크
            # ═══════════════════════════════════════
            sold_today = False

            if total_qty > 0:
                avg_price = total_cost / total_qty

                # 손절 체크 (즉시)
                if l <= sl_price:
                    exec_p = min(o, sl_price) if o > 0 else sl_price
                    sell_amount = exec_p * total_qty
                    sell_dt = dj["dt"]
                    sell_price = exec_p
                    sell_type = "손절"
                    roi_pct = (exec_p / avg_price - 1) * 100.0
                    trade_log.append({
                        "trigger_dt": det["trigger_dt"], "signal_dt": det["signal_dt"],
                        "action": "손절", "dt": dj["dt"],
                        "price": exec_p, "qty": total_qty, "amount": sell_amount,
                        "tier": tier, "past_count": det["past_sig_count"],
                    })
                    break

                # 익절 체크
                tp_price = avg_price * (1 + tp_pct / 100.0)
                if h >= tp_price:
                    sell_amount = tp_price * total_qty
                    sell_dt = dj["dt"]
                    sell_price = tp_price
                    sell_type = f"익절(+{tp_pct:.1f}%)"
                    roi_pct = tp_pct
                    trade_log.append({
                        "trigger_dt": det["trigger_dt"], "signal_dt": det["signal_dt"],
                        "action": f"익절(+{tp_pct:.1f}%)", "dt": dj["dt"],
                        "price": tp_price, "qty": total_qty, "amount": sell_amount,
                        "tier": tier, "past_count": det["past_sig_count"],
                    })
                    break

                # 매도 안 일어남 → 추가 매수 가능 (미체결 주문)
                sold_today = False
            else:
                sold_today = False

            # ═══════════════════════════════════════
            #  경로A: 매수 체결 체크 (매도 없었을 때만)
            # ═══════════════════════════════════════
            if not sold_today and not path_b_active:
                bought_today = False
                for od in orders:
                    if od["filled"]: continue

                    if od["target"] is None:
                        # tier9 1차: 시그널 다음날 시가 즉시 매수
                        if j == start_idx:
                            fill_p = o
                            qty = int(od["amount"] / fill_p) if fill_p > 0 else 0
                            if qty > 0:
                                od["filled"] = True
                                od["fill_price"] = fill_p
                                od["fill_qty"] = qty
                                od["fill_dt"] = dj["dt"]
                                total_cost += fill_p * qty
                                total_qty += qty
                                bought_today = True
                                trade_log.append({
                                    "trigger_dt": det["trigger_dt"], "signal_dt": det["signal_dt"],
                                    "action": f"매수{od['label']}", "dt": dj["dt"],
                                    "price": fill_p, "qty": qty, "amount": fill_p * qty,
                                    "tier": tier, "past_count": det["past_sig_count"],
                                })
                    else:
                        if l <= od["target"]:
                            fill_p = od["target"]
                            qty = int(od["amount"] / fill_p) if fill_p > 0 else 0
                            if qty > 0:
                                od["filled"] = True
                                od["fill_price"] = fill_p
                                od["fill_qty"] = qty
                                od["fill_dt"] = dj["dt"]
                                total_cost += fill_p * qty
                                total_qty += qty
                                bought_today = True
                                trade_log.append({
                                    "trigger_dt": det["trigger_dt"], "signal_dt": det["signal_dt"],
                                    "action": f"매수{od['label']}", "dt": dj["dt"],
                                    "price": fill_p, "qty": qty, "amount": fill_p * qty,
                                    "tier": tier, "past_count": det["past_sig_count"],
                                })

                # ═══════════════════════════════════════
                #  경로B 전환 체크: 매수 미체결 + 상한돌파 양봉
                # ═══════════════════════════════════════
                if total_qty == 0 and not bought_today:
                    # 아직 1주도 안 샀고, 이 봉이 상한돌파 양봉인지 확인
                    if c > breakout_close and c >= o:  # 종가 > peak양봉종가 AND 양봉
                        path_b_active = True
                        path_b_buy_next_open = True  # 다음날 시가에 매수 예약

        # ── 미청산 처리 ──
        if sell_dt is None:
            if path_b_bought and path_b_qty > 0:
                last = days[-1]
                total_cost = path_b_cost
                total_qty = path_b_qty
                sell_dt = last["dt"]
                sell_price = last["close"]
                sell_type = "트레일보유중"
                roi_pct = (last["close"] / path_b_buy_price - 1) * 100.0
                sell_amount = last["close"] * path_b_qty
            elif total_qty > 0:
                last = days[-1]
                avg_price = total_cost / total_qty
                sell_dt = last["dt"]
                sell_price = last["close"]
                sell_type = "보유중"
                roi_pct = (last["close"] / avg_price - 1) * 100.0
                sell_amount = last["close"] * total_qty
            else:
                # 매수 자체가 안 됨
                continue

        filled_orders = [od for od in orders if od["filled"]]
        avg_price_final = total_cost / total_qty if total_qty > 0 else 0
        pnl = sell_amount - total_cost if sell_dt else 0

        trades.append({
            "trigger_dt": det["trigger_dt"],
            "signal_dt": det["signal_dt"],
            "buy_dts": ([od["fill_dt"] for od in filled_orders]
                        if not path_b_bought else [dj["dt"] for dj in days
                            if any(e["dt"] == dj["dt"] and "매수" in e.get("action","")
                                   for e in trade_log if e["trigger_dt"] == det["trigger_dt"])]),
            "first_buy_dt": (filled_orders[0]["fill_dt"] if filled_orders
                             else next((e["dt"] for e in trade_log
                                        if e["trigger_dt"] == det["trigger_dt"] and "매수" in e.get("action","")), "")),
            "avg_price": avg_price_final,
            "total_cost": total_cost,
            "total_qty": total_qty,
            "sell_dt": sell_dt,
            "sell_price": sell_price,
            "sell_type": sell_type,
            "sell_amount": sell_amount,
            "pnl": pnl,
            "roi_pct": roi_pct,
            "tier": tier,
            "past_sig_count": det["past_sig_count"],
            "base_lower": det["base_lower"],
            "base_upper": det["base_upper"],
            "sl_price": sl_price,
            "tp_pct": TIER_TP_PCT.get(tier, 0),
            "filled_count": len(filled_orders) if not path_b_bought else 1,
            "order_count": len(orders),
            "path": "B(돌파)" if path_b_bought else "A(분할)",
        })

    return trades, trade_log


# ═══════════════════════════════════════════════════════
#  enriched 데이터 + 차트
# ═══════════════════════════════════════════════════════
def _enrich(days, ma_period):
    closes = [d["close"] for d in days]
    ma_v = _ma(closes, ma_period)
    return [{**d, f"ma{ma_period}": ma_v[i]} for i, d in enumerate(days)]

def _build_chart(enriched, detections, trades, trade_log, name, ma_period, period_days=0):
    df = pd.DataFrame(enriched)
    df["dt"] = pd.to_datetime(df["dt"])
    if period_days > 0:
        df = df[df["dt"] >= datetime.now() - timedelta(days=period_days)].copy().reset_index(drop=True)
    if df.empty:
        fig = go.Figure(); fig.add_annotation(text="데이터 없음", showarrow=False); return fig

    df["is_bull"] = df["close"] >= df["open"]
    trig_set = {d["trigger_dt"] for d in detections}
    sig_set = {d["signal_dt"] for d in detections}
    ds = df["dt"].dt.strftime("%Y-%m-%d")
    df["is_trig"] = ds.isin(trig_set)

    vc = [COLORS["trigger_vol"] if row["is_trig"]
          else (COLORS["bull_vol"] if row["is_bull"] else COLORS["bear_vol"])
          for _, row in df.iterrows()]

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.02, row_heights=[0.75, 0.25])

    fig.add_trace(go.Candlestick(
        x=df["dt"], open=df["open"], high=df["high"], low=df["low"], close=df["close"],
        increasing=dict(line=dict(color=COLORS["bull"]), fillcolor=COLORS["bull"]),
        decreasing=dict(line=dict(color=COLORS["bear"]), fillcolor=COLORS["bear"]),
        name="일봉"), row=1, col=1)

    ma_col = f"ma{ma_period}"
    if ma_col in df.columns:
        c = COLORS["ma120"] if ma_period == 120 else COLORS["ma240"]
        fig.add_trace(go.Scatter(x=df["dt"], y=df[ma_col], name=f"MA{ma_period}",
                                 line=dict(color=c, width=1.2, dash="dash"), hoverinfo="skip"), row=1, col=1)

    # 트리거 마커
    dt_ = df[df["is_trig"]]
    if not dt_.empty:
        fig.add_trace(go.Scatter(x=dt_["dt"], y=dt_["high"] * 1.04, mode="markers", name="★ 트리거",
            marker=dict(symbol="star", size=14, color=COLORS["trigger"], line=dict(width=1, color="#F9A825")),
            hovertemplate="%{x|%Y-%m-%d}<br><b>★ 트리거</b><extra></extra>"), row=1, col=1)

    # 매수 마커
    buys = [e for e in trade_log if "매수" in e.get("action", "")]
    if buys:
        fig.add_trace(go.Scatter(
            x=pd.to_datetime([e["dt"] for e in buys]),
            y=[e["price"] * 0.97 for e in buys], mode="markers", name="▲ 매수",
            marker=dict(symbol="triangle-up", size=12, color=COLORS["buy1"], line=dict(width=1, color="#004D40")),
            customdata=[[e["price"], e["qty"]] for e in buys],
            hovertemplate="%{x|%Y-%m-%d}<br><b>▲ 매수</b><br>%{customdata[0]:,.0f}원 × %{customdata[1]:,}주<extra></extra>"),
            row=1, col=1)

    # 익절/손절/트레일링 마커
    for key, color, label in [("익절", COLORS["sell_tp"], "▼ 익절"),
                               ("손절", COLORS["sell_sl"], "▼ 손절"),
                               ("트레일링", COLORS["sell_trail"], "▼ 트레일링")]:
        entries = [e for e in trade_log if key in e.get("action", "") and "매수" not in e.get("action", "")]
        if entries:
            fig.add_trace(go.Scatter(
                x=pd.to_datetime([e["dt"] for e in entries]),
                y=[e["price"] * 1.03 for e in entries], mode="markers", name=label,
                marker=dict(symbol="triangle-down", size=12, color=color, line=dict(width=1.5, color="#FFF")),
                customdata=[e["price"] for e in entries],
                hovertemplate=f"%{{x|%Y-%m-%d}}<br><b>{label}</b><br>%{{customdata:,.0f}}원<extra></extra>"),
                row=1, col=1)

    # 기준가 수평선
    for det in detections:
        dr = df[(df["dt"] >= pd.Timestamp(det["trigger_dt"])) & (df["dt"] <= pd.Timestamp(det["signal_dt"]) + timedelta(days=10))]
        if dr.empty: continue
        x0, x1 = dr["dt"].iloc[0], dr["dt"].iloc[-1]
        for val, clr, dash in [(det["base_upper"], COLORS["upper"], "dot"), (det["base_lower"], COLORS["lower"], "dot")]:
            fig.add_shape(type="line", x0=x0, x1=x1, y0=val, y1=val,
                          line=dict(color=clr, width=1, dash=dash), row=1, col=1)

    # 거래량
    fig.add_trace(go.Bar(x=df["dt"], y=df["volume"], name="거래량", marker_color=vc, marker_line_width=0,
                         hovertemplate="%{x|%Y-%m-%d}<br>%{y:,.0f}<extra></extra>"), row=2, col=1)

    fig.update_layout(height=750, margin=dict(l=0, r=0, t=80, b=0),
        paper_bgcolor=COLORS["bg"], plot_bgcolor=COLORS["bg"],
        font=dict(color=COLORS["text"], size=12),
        title=dict(text=f"  {name} — 과거차트 시그널 기반 매매", font=dict(size=16, color="#FFF"),
                   x=0, xanchor="left", y=0.98, yanchor="top"),
        legend=dict(orientation="h", yanchor="top", y=1.0, xanchor="left", x=0,
                    bgcolor="rgba(0,0,0,0)", font=dict(size=11, color=COLORS["text"])),
        hovermode="x unified", xaxis_rangeslider_visible=False)
    fig.update_yaxes(row=1, col=1, tickformat=",", gridcolor=COLORS["grid"], zeroline=False, side="right")
    fig.update_yaxes(row=2, col=1, tickformat=".2s", gridcolor=COLORS["grid"], zeroline=False, side="right")
    at = set(df["dt"].dt.normalize())
    cal = pd.date_range(df["dt"].min().normalize(), df["dt"].max().normalize(), freq="D")
    nt = [d for d in cal if d not in at]
    for rn in (1, 2):
        fig.update_xaxes(row=rn, col=1, gridcolor=COLORS["grid"], zeroline=False, showgrid=False,
                         rangebreaks=[dict(values=[d.strftime("%Y-%m-%d") for d in nt])])
    fig.update_xaxes(row=2, col=1, tickformat="%y/%m/%d")
    return fig


# ═══════════════════════════════════════════════════════
#  통계 / 요약
# ═══════════════════════════════════════════════════════
def _summary_df(trades):
    rows = []
    for i, t in enumerate(trades):
        pnl = t.get("pnl", 0)
        pnl_label = f"+{pnl:,.0f}" if pnl >= 0 else f"{pnl:,.0f}"
        rows.append({
            "No": i + 1,
            "트리거": t["trigger_dt"],
            "시그널": t["signal_dt"],
            "과거횟수": t["past_sig_count"],
            "티어": f"T{t['tier']}",
            "경로": t.get("path", "A"),
            "하한": f"{t['base_lower']:,.0f}",
            "상한": f"{t['base_upper']:,.0f}",
            "매수일": t.get("first_buy_dt", ""),
            "평균매수가": f"{t['avg_price']:,.0f}" if t["avg_price"] > 0 else "-",
            "투자금": f"{t['total_cost']:,.0f}",
            "체결": f"{t['filled_count']}/{t['order_count']}",
            "매도일": t["sell_dt"] or "",
            "매도유형": t["sell_type"],
            "매도금액": f"{t.get('sell_amount', 0):,.0f}",
            "손익금": pnl_label,
            "수익률": f"{t['roi_pct']:+.1f}%",
        })
    return pd.DataFrame(rows)

def _calc_stats(trades):
    if not trades: return {"total": 0, "wins": 0, "losses": 0, "holds": 0,
                           "trails": 0, "win_rate": 0, "avg_roi": 0,
                           "total_pnl": 0, "total_profit": 0, "total_loss": 0}
    w = [t for t in trades if "익절" in t["sell_type"]]
    lo = [t for t in trades if "손절" in t["sell_type"]]
    tr = [t for t in trades if "트레일링" in t["sell_type"]]
    ho = [t for t in trades if "보유" in t["sell_type"]]
    cl = [t for t in trades if "보유" not in t["sell_type"]]
    pnl_list = [t.get("pnl", 0) for t in trades if "보유" not in t["sell_type"]]
    total_profit = sum(p for p in pnl_list if p > 0)
    total_loss = sum(p for p in pnl_list if p < 0)
    wins_all = [t for t in cl if t.get("pnl", 0) > 0]
    return {
        "total": len(trades), "wins": len(w), "losses": len(lo), "holds": len(ho),
        "trails": len(tr),
        "win_rate": (len(wins_all) / len(cl) * 100) if cl else 0,
        "avg_roi": (sum(t["roi_pct"] for t in cl) / len(cl)) if cl else 0,
        "total_pnl": total_profit + total_loss,
        "total_profit": total_profit,
        "total_loss": total_loss,
    }


# ═══════════════════════════════════════════════════════
#  Streamlit UI
# ═══════════════════════════════════════════════════════
_CSS = """<style>
[data-testid="stMetric"]{background:linear-gradient(135deg,#1a1f2e,#151926);
border:1px solid #2a2f42;border-radius:10px;padding:14px 18px}
[data-testid="stMetric"] label{color:#8b8fa3!important;font-size:.78rem!important}
[data-testid="stMetric"] [data-testid="stMetricValue"]{color:#e8eaed!important;font-size:1.15rem!important;font-weight:600!important}
section[data-testid="stSidebar"]{background:#0f1117}
</style>"""

def _filter_by_period(detections, trades, trade_log, period_days):
    """차트표시기간에 해당하는 트레이드만 필터링"""
    if period_days <= 0:
        return detections, trades, trade_log
    cutoff = (datetime.now() - timedelta(days=period_days)).strftime("%Y-%m-%d")
    f_det = [d for d in detections if d["trigger_dt"] >= cutoff]
    f_trd = [t for t in trades if t["trigger_dt"] >= cutoff]
    trig_set = {t["trigger_dt"] for t in f_trd}
    f_log = [e for e in trade_log if e["trigger_dt"] in trig_set]
    return f_det, f_trd, f_log


def _render_one(name, ticker, enriched, detections, trades, tlog, ma_period, pd_, ksuf=""):
    # 기간 필터링: 차트표시기간 내 트레이드만
    detections, trades, tlog = _filter_by_period(detections, trades, tlog, pd_)

    s = _calc_stats(trades)
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("트레이드", f"{s['total']}건")
    c2.metric("익절", f"{s['wins']}건")
    c3.metric("손절", f"{s['losses']}건")
    c4.metric("트레일링", f"{s['trails']}건")
    c5.metric("승률", f"{s['win_rate']:.0f}%")
    c6.metric("평균수익률", f"{s['avg_roi']:+.1f}%")

    # 손익 요약
    c7, c8, c9 = st.columns(3)
    pnl_color = "🟢" if s["total_pnl"] >= 0 else "🔴"
    c7.metric("총 수익금", f"{s['total_profit']:+,.0f}원")
    c8.metric("총 손실금", f"{s['total_loss']:,.0f}원")
    c9.metric(f"{pnl_color} 순손익", f"{s['total_pnl']:+,.0f}원")

    if s["holds"] > 0: st.info(f"📌 보유중 {s['holds']}건")

    fig = _build_chart(enriched, detections, trades, tlog, name, ma_period, pd_)
    uk = f"{ksuf}_{ticker}"
    st.plotly_chart(fig, use_container_width=True, key=f"ch2{uk}",
        config={"displayModeBar": True, "displaylogo": False, "scrollZoom": True,
                "modeBarButtonsToRemove": ["lasso2d", "select2d", "autoScale2d", "toggleSpikelines"]})
    if trades:
        st.markdown("#### 📋 트레이드 상세")
        st.dataframe(_summary_df(trades), use_container_width=True, hide_index=True, key=f"tb2{uk}")
        csv = _summary_df(trades).to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
        st.download_button("📥 CSV", csv, f"{name}_scan2.csv", "text/csv", key=f"csv2{uk}")

        # 티어별 분포 + 경로별 분포
        tier_counts = {}
        path_counts = {"A(분할)": 0, "B(돌파)": 0}
        for t in trades:
            tier_counts[f"T{t['tier']}"] = tier_counts.get(f"T{t['tier']}", 0) + 1
            p = t.get("path", "A(분할)")
            path_counts[p] = path_counts.get(p, 0) + 1
        st.markdown("**티어별:** " + " / ".join(f"{k}: {v}건" for k, v in sorted(tier_counts.items()))
                    + "  |  **경로별:** " + " / ".join(f"{k}: {v}건" for k, v in path_counts.items() if v > 0))

        # 트레일링 디버그 로그
        ts_logs = [e for e in tlog if "트레일링" in (e.get("action") or "")]
        if ts_logs:
            with st.expander("🧪 트레일링 디버그 (peak/stop)"):
                st.dataframe(pd.DataFrame(ts_logs), use_container_width=True, hide_index=True)
    elif detections:
        st.success(f"패턴 {len(detections)}개 탐지")
    else:
        st.info("패턴 미탐지")

def _render_multi(results, ma_period, pd_, ksuf=""):
    ov = []
    grand_profit = 0
    grand_loss = 0
    grand_trades = 0
    grand_wins = 0
    grand_losses = 0
    grand_trails = 0
    for it in results:
        if it.get("error"): continue
        # 기간 필터링
        _, f_trades, _ = _filter_by_period(it.get("detections", []), it["trades"], it.get("trade_log", []), pd_)
        s = _calc_stats(f_trades)
        grand_profit += s["total_profit"]
        grand_loss += s["total_loss"]
        grand_trades += s["total"]
        grand_wins += s["wins"]
        grand_losses += s["losses"]
        grand_trails += s["trails"]
        if s["total"] == 0 and not it.get("detections"): continue
        ov.append({
            "종목": f"{it['name']}({it['ticker']})",
            "패턴": len(it.get("detections", [])),
            "트레이드": s["total"], "익절": s["wins"], "손절": s["losses"],
            "트레일링": s["trails"],
            "승률": f"{s['win_rate']:.0f}%", "평균수익률": f"{s['avg_roi']:+.1f}%",
            "수익금": f"{s['total_profit']:+,.0f}",
            "손실금": f"{s['total_loss']:,.0f}",
            "순손익": f"{s['total_pnl']:+,.0f}",
        })

    # ── 전체 종합 요약 ──
    grand_pnl = grand_profit + grand_loss
    st.markdown("#### 📊 전체 종합 결과")
    g1, g2, g3, g4, g5, g6 = st.columns(6)
    g1.metric("총 트레이드", f"{grand_trades}건")
    g2.metric("총 익절", f"{grand_wins}건")
    g3.metric("총 손절", f"{grand_losses}건")
    g4.metric("총 수익금", f"{grand_profit:+,.0f}원")
    g5.metric("총 손실금", f"{grand_loss:,.0f}원")
    pnl_icon = "🟢" if grand_pnl >= 0 else "🔴"
    g6.metric(f"{pnl_icon} 총 순손익", f"{grand_pnl:+,.0f}원")

    if ov:
        st.markdown("#### 📋 종목별 요약")
        st.dataframe(pd.DataFrame(ov), use_container_width=True, hide_index=True)
        csv = pd.DataFrame(ov).to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
        st.download_button("📥 종합 CSV", csv, "scan2_summary.csv", "text/csv", key=f"csv_grand{ksuf}")

    errors = [r for r in results if r.get("error")]
    if errors:
        with st.expander(f"⚠️ 오류 {len(errors)}건"):
            for e in errors: st.warning(f"{e['query']}: {e['error']}")


def main():
    st.set_page_config(page_title="패턴 스캔 v2", page_icon="📊", layout="wide", initial_sidebar_state="expanded")
    st.markdown(_CSS, unsafe_allow_html=True)

    with st.sidebar:
        st.markdown("## ⚙️ 과거차트 시그널 스캔")
        input_mode = st.radio("입력 방식", ["직접 입력", "파일 업로드"], horizontal=True)
        if input_mode == "직접 입력":
            ti = st.text_input("종목명/코드", placeholder="예: 삼성전자"); uf = None
        else:
            ti = None; uf = st.file_uploader("종목 목록(.txt/.md)", type=["txt", "md"])

        st.markdown("---")
        st.markdown("##### 📐 기본 설정")
        cp = st.selectbox("차트 표시 기간", PERIOD_OPTIONS, index=len(PERIOD_OPTIONS) - 1)
        ma_period = st.selectbox("이동평균선", [120, 240], index=0, format_func=lambda x: f"MA{x}")
        min_cap = st.number_input("최소 시총(억원)", 100, 100000, 1000, 100)

        st.markdown("---")
        st.markdown("##### 🔍 시그널 조건")
        lb_label = st.selectbox("분석 기간 (트리거 유효성 + 과거 시그널)", LOOKBACK_OPTIONS, index=1)
        lookback_days = LOOKBACK_MAP[lb_label]
        threshold_pct = st.selectbox("과거 시그널 임계치", [50, 75], index=0, format_func=lambda x: f"트리거 종가의 {x}% 이상")
        min_sig = st.number_input("최소 시그널 횟수", 1, 20, 3)

        vol_override = st.checkbox("거래량 배수 직접 입력", False)
        if vol_override:
            vol_manual = st.slider("거래량 배수", 1.5, 10.0, 3.0, 0.5)
        else:
            vol_manual = None

        st.markdown("---")
        st.markdown("##### 💰 매매 설정")
        steps = st.selectbox("가격 분할 단계", [4, 8], index=0, format_func=lambda x: f"{x}단계")
        base_unit = st.number_input("기본단위(원)", 10_000, 10_000_000, 300_000, 10_000,
                                    help="1차=기본단위, 2차=×2, 3차=×4")
        st.caption(f"매수 금액: 1차 {base_unit:,.0f} / 2차 {base_unit * 2:,.0f} / 3차 {base_unit * 4:,.0f}")

        st.markdown("##### 🔄 경로B (상한돌파 트레일링)")
        trail_pct = st.slider("트레일링 스탑(%)", 3.0, 15.0, 7.0, 0.5,
                              help="분할매수 미체결 + 상한돌파 양봉 → 다음날 시가 매수 → 고점대비 N% 하락 시 매도")

        max_pages = st.number_input("API 페이지", 5, 100, 40, 5)
        run = st.button("🔍 스캔 실행", use_container_width=True, type="primary")

        st.markdown("---")
        # 범례
        st.markdown(f"""<div style="font-size:.73rem;color:#888;line-height:1.8">
        <b style="color:#FFD600">★</b> 트리거: 양봉+거래량+MA{ma_period}위+최고종가<br>
        <b style="color:#FF5252">━</b> 상한가
        <b style="color:#448AFF">━</b> 하한가<br>
        <b style="color:#00E676">▲</b> 매수 (경로A: 분할 / 경로B: 돌파시가)<br>
        <b style="color:#2196F3">▼</b> 익절
        <b style="color:#F44336">▼</b> 손절
        <b style="color:#00BCD4">▼</b> 트레일링<br><br>
        <b>경로A</b>: 분할매수 → 평균매수가 익절/손절<br>
        <b>경로B</b>: 매수미체결+상한돌파양봉 → 다음날시가+트레일링{trail_pct:.0f}%<br><br>
        <b>티어별 규칙 ({steps}단계)</b><br>
        T3: 횟수3~4 / 익절+5.0%<br>
        T5: 횟수5~6 / 익절+7.5%<br>
        T7: 횟수7~8 / 익절+10.0%<br>
        T9: 횟수9+ / 익절+12.5%
        </div>""", unsafe_allow_html=True)

    st.markdown(
        "<h2 style='margin-bottom:0'>📊 과거차트 시그널 기반 매매 스캔</h2>"
        "<p style='color:#888;margin-top:4px'>트리거(최고종가) → 과거 시그널 횟수 → 티어별 분할매수</p>",
        unsafe_allow_html=True)

    # session_state
    if "scan2_results" not in st.session_state: st.session_state.scan2_results = None
    if "scan2_mode" not in st.session_state: st.session_state.scan2_mode = None
    if "scan2_pd" not in st.session_state: st.session_state.scan2_pd = 0
    if "scan2_ma" not in st.session_state: st.session_state.scan2_ma = 120

    if run:
        cd = PERIOD_MAP.get(cp, 0)

        if input_mode == "직접 입력":
            if not ti or not ti.strip(): st.error("종목명 입력"); return
            try:
                with st.spinner("종목 확인..."):
                    ticker, name = _resolve(ti.strip())
                with st.spinner("시총 확인..."):
                    token = core.get_token(core.APP_KEY, core.APP_SECRET)
                    cap = _get_market_cap(token, ticker)
                    if cap > 0 and cap < min_cap:
                        st.error(f"{name} 시총 {cap:,.0f}억 < {min_cap:,.0f}억"); return
                vm = vol_manual if vol_override and vol_manual else _volume_multiplier(cap if cap > 0 else 1000)
                if cap > 0:
                    st.info(f"📊 {name} 시총: {cap:,.0f}억 → 거래량 기준: {_calc_volume_pct(cap):.0f}% ({vm:.1f}배)")
                with st.spinner(f"{name} 데이터 조회..."):
                    days = _fetch_ohlcv_cached(token, ticker, max_pages)
                with st.spinner("분석..."):
                    dets = _detect_all(days, vm, ma_period, lookback_days, threshold_pct, min_sig)
                    trades, tlog = _simulate(days, dets, steps, base_unit, trail_pct)
                    enriched = _enrich(days, ma_period)
                st.session_state.scan2_results = {
                    "name": name, "ticker": ticker, "enriched": enriched,
                    "detections": dets, "trades": trades, "trade_log": tlog}
                st.session_state.scan2_mode = "single"
                st.session_state.scan2_pd = cd
                st.session_state.scan2_ma = ma_period
            except Exception as e:
                st.error(f"오류: {e}"); st.exception(e); return
        else:
            if not uf: st.warning("파일 업로드 필요"); return
            qs = _parse_file(uf.read().decode("utf-8"))
            if not qs: st.error("종목 없음"); return
            st.info(f"📂 {len(qs)}개: {', '.join(qs[:20])}{'...' if len(qs) > 20 else ''}")
            token = core.get_token(core.APP_KEY, core.APP_SECRET)
            results = []
            pg = st.progress(0)

            # 종합결과 표시 영역 (상단 고정, 실시간 갱신)
            summary_area = st.container()

            # 종목별 결과를 실시간으로 표시
            for idx, q in enumerate(qs):
                pg.progress((idx + 1) / len(qs), f"({idx + 1}/{len(qs)}) {q}")
                try:
                    tk, nm = _resolve(q)
                    cap = _get_market_cap(token, tk)
                    if cap > 0 and cap < min_cap:
                        results.append({"query": q, "ticker": tk, "name": nm, "enriched": [],
                                        "detections": [], "trades": [], "trade_log": [],
                                        "error": f"시총 {cap:,.0f}억 < {min_cap:,.0f}억"})
                        continue
                    vm = vol_manual if vol_override and vol_manual else _volume_multiplier(cap if cap > 0 else 1000)
                    days = _fetch_ohlcv_cached(token, tk, max_pages)
                    dets = _detect_all(days, vm, ma_period, lookback_days, threshold_pct, min_sig)
                    trs, tl = _simulate(days, dets, steps, base_unit, trail_pct)
                    en = _enrich(days, ma_period)
                    it = {"query": q, "ticker": tk, "name": nm, "enriched": en,
                          "detections": dets, "trades": trs, "trade_log": tl, "error": None}
                    results.append(it)

                    # ── 종목별 즉시 표시 (패턴/트레이드 있을 때만) ──
                    if dets or trs:
                        _, f_trs, _ = _filter_by_period(dets, trs, tl, cd)
                        s = _calc_stats(f_trs)
                        ic = "🟢" if s["avg_roi"] >= 0 else "🔴"
                        with st.expander(f"{ic} ({idx+1}/{len(qs)}) {nm}({tk}) — 트레이드 {s['total']}건 / 순손익 {s['total_pnl']:+,.0f}원", expanded=False):
                            _render_one(nm, tk, en, dets, trs, tl, ma_period, cd, ksuf=f"_s{idx}")
                except Exception as e:
                    results.append({"query": q, "ticker": "", "name": q, "enriched": [],
                                    "detections": [], "trades": [], "trade_log": [], "error": str(e)})

            pg.empty()

            # ── 전체 완료 후 종합결과 상단에 표시 ──
            with summary_area:
                _render_multi(results, ma_period, cd, ksuf="_s")

            st.session_state.scan2_results = results
            st.session_state.scan2_mode = "multi_done"
            st.session_state.scan2_pd = cd
            st.session_state.scan2_ma = ma_period

    # 결과 표시
    if st.session_state.scan2_results is not None:
        pd_ = st.session_state.scan2_pd
        ma_p = st.session_state.scan2_ma
        if st.session_state.scan2_mode == "single":
            r = st.session_state.scan2_results
            st.markdown(f"### {r['name']} ({r['ticker']})")
            _render_one(r["name"], r["ticker"], r["enriched"], r["detections"],
                        r["trades"], r["trade_log"], ma_p, pd_, ksuf="_r")
        elif st.session_state.scan2_mode == "multi_done":
            # rerun 시 session_state에서 복원 표시
            results = st.session_state.scan2_results
            _render_multi(results, ma_p, pd_, ksuf="_r")
            found = [it for it in results
                     if not it.get("error") and (it.get("detections") or it.get("trades"))]
            if found:
                st.markdown(f"#### 📈 패턴 탐지 종목 ({len(found)}개)")
                for i, it in enumerate(found):
                    _, f_trd, _ = _filter_by_period(it.get("detections", []), it["trades"], it.get("trade_log", []), pd_)
                    s = _calc_stats(f_trd)
                    ic = "🟢" if s["avg_roi"] >= 0 else "🔴"
                    with st.expander(f"{ic} {it['name']}({it['ticker']}) — 트레이드 {s['total']}건 / 순손익 {s['total_pnl']:+,.0f}원"):
                        _render_one(it["name"], it["ticker"], it["enriched"], it["detections"],
                                    it["trades"], it["trade_log"], ma_p, pd_, ksuf=f"_r{i}")
    else:
        st.info("👈 설정 후 **스캔 실행**을 누르세요.")
        with st.expander("💡 로직 가이드", expanded=True):
            st.markdown("""
**[트리거]** 양봉 + 거래량폭증 + MA위 + 설정기간 내 최고종가

**[과거차트 시그널]** 설정기간 내 (종가≥트리거종가×임계% AND 거래량폭증) 횟수 카운트  
→ 3회 이상이면 시그널 발생

**[매수]** 티어(3/5/7/9)별 3단계 분할매수 (비율 1:2:4)  
횟수↑ → 매수가↑, 손절선↑, 익절률↑ (검증된 강한 종목에 공격적 진입)

**[매도]** 평균매수가 기준 티어별 익절률 (5%~12.5%), 즉시 손절
""")


if __name__ == "__main__":
    main()
