"""
pattern_scan3.py — 눌림매수 3‑시나리오 시뮬레이션 (v3)
═══════════════════════════════════════════════════════════
실행: streamlit run pattern_scan3.py

[0] 공통
    - 키움 REST API, OHLCV 캐시(parquet/csv)
    - 장중 당일봉 불완전 → 전일 봉까지만 판단
    - 단일 입력 / 파일 다중 입력

[1] 거래량 기준
    차트표시기간 내 거래량 min/max 산출
    vol_threshold = min + (max - min) × 비율%
    (0%=최소거래량, 100%=최대거래량)
    시총 필터는 별도 유지 (ka10001 API)

[2] UI 파라미터
    - 차트표시기간 = 거래량 산출기간 (공유)
    - vol_threshold = min + (max-min) × 거래량비율%
    - MA 선택: 120 / 240
    - MA 상승: 240→5일연속, 120→10일연속 (MA오늘>MA어제)
    - 종가/저가 선택 (price_ref)
    - 최대 감시기간(거래일) 기본 60
    - 상한 확정 옵션: peak_high only / max(peak, bear_high)
    - 투자금 설정 (기본 200만원)

[3] 트리거
    양봉 + 거래량 ≥ vol_threshold + MA 연속 상승

[4] 하한기준가(base_lower): 트리거 봉부터 역추적 (제한 없음)
    - 양봉: 계속
    - 음봉 종가 > 전일종가: 양봉 간주, 계속
    - 음봉 종가 ≤ 전일종가: 진짜 음봉 STOP
    - base_lower = 추적 봉 중 최저 시가

[5] 상한기준가(base_upper): 트리거 이후 양봉 고가 추적
    - 양봉/상승음봉: 고가 갱신하며 계속
    - 확정 음봉: 옵션에 따라 peak_high or max(peak, bear_high)

[6] 기준선
    range = upper - lower
    1/4 = lower + range×1/4  (손절가이드)
    1/2 = lower + range×1/2  (중간기준가 mid)
    3/4 = lower + range×3/4  (돌파가이드)
    5/8 = lower + range×5/8
    7/8 = lower + range×7/8

[7] 상한 갱신 루프
    - 감시 중 종가 < mid → 패턴 종료 (매수 전만)
    - 3/4 이하 안 내려가고 종가 > upper → 갱신 루프 진입
    - high > upper but close ≤ upper → upper=high 갱신, 감시 계속
    - 종가 < 3/4 → 눌림 시작

[8] 시나리오 (먼저 충족된 1개만 발동)
    S1(고가): ref가 mid 위 유지 + ref<5/8 → 양봉 종가>5/8
      매수: 1차=sig종가(50%), 2차=(sig시가+종가)/2(50%)
      중단: 종가>upper or 감시기간초과
      익절: high≥upper → upper에서 매도
      손절: 종가<mid → 즉시 매도

    S2(중가): ref가 mid 아래 갔다가 → 양봉 종가>mid
      매수: 동일
      중단: 종가>7/8 or 감시기간초과
      익절: high≥7/8 → 7/8에서 매도
      손절: 종가<1/4 → 즉시 매도

    S3(저가): ref가 1/4 아래 갔다가 → 양봉 종가>1/4
      매수: 동일
      중단: 종가>5/8 or 감시기간초과
      익절: high≥5/8 → 5/8에서 매도
      손절: 종가<lower → 즉시 매도

[9] 같은 날 매수+매도 금지 (매도 우선)

[10] 자동 실행
    MA(120/240) × 거래주식수비율(10~100%) 조합 순차 실행
    요약 테이블: 조합별 익절성공율·수익률·누적손익
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
    "bv": "#EF9A9A", "bev": "#90CAF9",
    "trig": "#FFD600", "upper": "#FF5252", "lower": "#448AFF",
    "g34": "#FF9800", "g12": "#AB47BC", "g14": "#795548",
    "g58": "#00BCD4", "g78": "#E91E63",
    "buy": "#00E676", "tp": "#2196F3", "sl": "#F44336",
    "ma120": "#26A69A", "ma240": "#FF7043",
    "bg": "#131722", "grid": "#1E222D", "txt": "#D1D4DC",
}
PERIOD_OPTIONS = ["6개월", "1년", "1년6개월", "2년", "3년", "5년", "전체"]
PERIOD_MAP = {"6개월": 180, "1년": 365, "1년6개월": 548, "2년": 730,
              "3년": 1095, "5년": 1825, "전체": 0}
CACHE_DIR = Path(".cache/pattern_scan3")
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# ═══════════════════════════════════════════════════════
#  유틸
# ═══════════════════════════════════════════════════════
def _int(v, d=0):
    if v is None: return d
    if isinstance(v, int): return v
    s = str(v).strip().replace(",", "").replace("+", "").replace("-", "", 1).strip()
    if not s: return d
    try: return int(float(s))
    except: return d

def _pdt(v):
    if v is None: return None
    s = str(v).strip()
    if len(s) >= 8 and s[:8].isdigit(): return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    if len(s) >= 10 and s[4] == "-": return s[:10]
    return None

def _f(row, keys):
    for k in keys:
        if k in row and str(row.get(k)).strip() != "": return row.get(k)
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

def _parse_file(txt):
    return [t.strip() for t in txt.replace("\n", ",").replace("\r", ",").split(",") if t.strip()]

def _today():
    return datetime.now(core.TZ).strftime("%Y-%m-%d")

def _intraday():
    now = datetime.now(core.TZ)
    return now < now.replace(hour=15, minute=50, second=0)

# ═══════════════════════════════════════════════════════
#  API: 시총 조회
# ═══════════════════════════════════════════════════════
def _get_cap(token, ticker):
    """ka10001 API로 시총(억원) 조회"""
    try:
        res = core.call_tr_all_pages(token=token, api_id="ka10001",
              body={"stk_cd": ticker}, endpoint="/api/dostk/stkinfo", max_pages=1)
        rows = res.get("rows") or []
        d = rows[0] if rows else (res.get("data") or res)
        cap_keys = ["mktc", "market_cap", "시가총액", "mkt_cap", "tot_mktc",
                     "stk_mktc", "stkMktc", "totMktc"]
        return abs(_int(_f(d, cap_keys)))
    except:
        return 0


# ═══════════════════════════════════════════════════════
#  거래량 기준 산출 (기간 내 min/max 기반)
# ═══════════════════════════════════════════════════════
def _calc_vol_range(days, period_days):
    """기간 내 거래량 min/max 산출. period_days=0이면 전체."""
    if not days: return 0, 0
    if period_days > 0:
        cutoff = (datetime.now() - timedelta(days=period_days)).strftime("%Y-%m-%d")
        subset = [d for d in days if d["dt"] >= cutoff]
    else:
        subset = days
    if not subset: subset = days
    vols = [d["volume"] for d in subset if d["volume"] > 0]
    if not vols: return 0, 0
    return min(vols), max(vols)

def _vol_threshold(vol_min, vol_max, pct):
    """min~max 범위에서 pct% 위치의 거래량 (0%=min, 100%=max)"""
    return vol_min + (vol_max - vol_min) * pct / 100.0

# ═══════════════════════════════════════════════════════
#  OHLCV 조회 + 캐시
# ═══════════════════════════════════════════════════════
def _fetch_raw(token, ticker, mp=40):
    edt = datetime.now(core.TZ).strftime("%Y%m%d")
    stex = (os.getenv("KIWOOM_DMST_STEX_TP") or "KRX").strip().upper()
    upd = (os.getenv("KIWOOM_OHLCV_UPD_STKPC_TP") or "1").strip()
    cm = {"stk_cd": ticker, "stex_tp": stex, "dmst_stex_tp": stex}
    for body in [{**cm, "base_dt": edt, "upd_stkpc_tp": upd}, {**cm, "base_dt": edt},
                 {**cm, "dt": edt, "upd_stkpc_tp": upd}, {**cm, "dt": edt}]:
        try:
            res = core.call_tr_all_pages(token=token, api_id="ka10081", body=body,
                                         endpoint="/api/dostk/chart", max_pages=mp)
            rows = res.get("rows") or []
            if not rows: continue
            dd = {}
            for r in rows:
                dt = _pdt(_f(r, ["dt", "date", "bas_dt", "base_dt", "trde_dt", "trd_dt"]))
                if not dt: continue
                o = _int(_f(r, ["open_pric", "open", "stck_oprc", "opn_prc"]), 0)
                h = _int(_f(r, ["high_pric", "high", "stck_hgpr", "hgh_prc"]), 0)
                l = _int(_f(r, ["low_pric", "low", "stck_lwpr", "low_prc"]), 0)
                c = _int(_f(r, ["close_pric", "close", "stck_clpr", "cur_prc", "cur_pric"]), 0)
                v = _int(_f(r, ["trde_qty", "volume", "acml_vol", "acc_trde_qty"]), 0)
                if c <= 0: continue
                if o <= 0: o = c
                if h <= 0: h = max(o, c)
                if l <= 0: l = min(o, c)
                dd[dt] = {"dt": dt, "open": o, "high": h, "low": l, "close": c, "volume": max(0, v)}
            out = sorted(dd.values(), key=lambda x: x["dt"])
            if out: return out
        except: continue
    raise RuntimeError("일봉 조회 실패")

def _cpath(tk):
    return CACHE_DIR / f"{tk}.parquet", CACHE_DIR / f"{tk}.csv"

def _load_c(tk):
    for p, rd in [(_cpath(tk)[0], lambda p: pd.read_parquet(p)),
                  (_cpath(tk)[1], lambda p: pd.read_csv(p))]:
        try:
            if p.exists():
                df = rd(p); df["dt"] = df["dt"].astype(str)
                if not df.empty: return df.to_dict("records")
        except: pass
    return []

def _save_c(tk, days):
    if not days: return
    pp, pc = _cpath(tk); df = pd.DataFrame(days)
    try: df.to_parquet(pp, index=False); return
    except: pass
    try: df.to_csv(pc, index=False, encoding="utf-8-sig")
    except: pass

def _fetch(token, tk, mp=40):
    today = _today(); intra = _intraday()
    cached = _load_c(tk)
    if cached:
        last = cached[-1]["dt"]
        if intra:
            fresh = _fetch_raw(token, tk, min(mp, 3))
            m = {d["dt"]: d for d in cached}
            for d in fresh: m[d["dt"]] = d
            out = sorted(m.values(), key=lambda x: x["dt"])
            _save_c(tk, out); return out
        if last >= today: return cached
        try: delta = (datetime.strptime(today, "%Y-%m-%d") - datetime.strptime(last, "%Y-%m-%d")).days
        except: delta = 30
        pg = min(mp, max(2, math.ceil((delta + 10) / 80)))
        fresh = _fetch_raw(token, tk, pg)
        m = {d["dt"]: d for d in cached}
        for d in fresh: m[d["dt"]] = d
        out = sorted(m.values(), key=lambda x: x["dt"])
        _save_c(tk, out); return out
    fresh = _fetch_raw(token, tk, mp)
    _save_c(tk, fresh); return fresh

# ═══════════════════════════════════════════════════════
#  MA + 연속 상승
# ═══════════════════════════════════════════════════════
def _ma(vals, w):
    out = [None] * len(vals); s = 0.0
    for i, v in enumerate(vals):
        s += float(v)
        if i >= w: s -= float(vals[i - w])
        if i >= w - 1: out[i] = s / float(w)
    return out

def _ma_up(mv, i, n):
    if i < n: return False
    for k in range(n):
        a, b = mv[i - k], mv[i - k - 1]
        if a is None or b is None or a <= b: return False
    return True

# ═══════════════════════════════════════════════════════
#  하한기준가: 역추적 (제한없음)
# ═══════════════════════════════════════════════════════
def _base_lower(days, ti):
    mn = days[ti]["open"]; i = ti - 1
    while i >= 0:
        d = days[i]
        if d["close"] >= d["open"]:
            mn = min(mn, d["open"]); i -= 1; continue
        if i >= 1 and d["close"] > days[i - 1]["close"]:
            mn = min(mn, d["open"]); i -= 1; continue
        break
    return mn

# ═══════════════════════════════════════════════════════
#  상한기준가: 트리거 이후 추적
# ═══════════════════════════════════════════════════════
def _base_upper(days, si, use_bear_high):
    """Returns (upper, confirm_idx)"""
    peak = days[si]["high"]
    for i in range(si + 1, len(days)):
        d = days[i]
        is_up = d["close"] >= d["open"]
        if not is_up and i >= 1 and d["close"] > days[i - 1]["close"]:
            is_up = True
        if is_up:
            peak = max(peak, d["high"]); continue
        # 확정 음봉
        if use_bear_high:
            return max(peak, d["high"]), i
        return peak, i
    return peak, len(days) - 1

# ═══════════════════════════════════════════════════════
#  가이드선
# ═══════════════════════════════════════════════════════
def _guides(lo, up):
    r = up - lo
    return {"lo": lo, "up": up, "rng": r,
            "p14": lo + r * 0.25, "mid": lo + r * 0.5,
            "p34": lo + r * 0.75, "p58": lo + r * 0.625,
            "p78": lo + r * 0.875}

# ═══════════════════════════════════════════════════════
#  핵심 분석 엔진
# ═══════════════════════════════════════════════════════
def _analyze(days, ma_period, vol_thr, invest, price_ref, max_watch, use_bear_high):
    n = len(days)
    if n < 30: return [], [], []
    closes = [d["close"] for d in days]
    opens  = [d["open"]  for d in days]
    highs  = [d["high"]  for d in days]
    lows   = [d["low"]   for d in days]
    vols   = [d["volume"] for d in days]
    mv     = _ma(closes, ma_period)
    ma_n   = 5 if ma_period == 240 else 10

    dets, trades, tlog = [], [], []

    # ─── 상태 ───
    ST_IDLE, ST_TRACK, ST_MON, ST_PULL = "IDLE", "TRACK", "MON", "PULL"
    ST_BUY_WAIT, ST_POS = "BUY_WAIT", "POS"
    state = ST_IDLE
    ti_idx = 0
    b_lo = b_up = 0.0
    g = {}
    peak_h = 0.0
    above_34 = True
    pull = False
    went_below_mid = False
    went_below_14 = False
    prev_ref_below_58 = False
    prev_ref_below_mid = False
    prev_ref_below_14 = False
    scen = ""
    sig_idx = 0
    sig_dt = ""            # 시그널 발생일
    buy1_p = buy2_p = 0.0
    buy1_ok = buy2_ok = False
    tot_cost = 0.0; tot_qty = 0
    watch_start = 0
    det_info = {}
    first_buy_dt = ""       # ★ 첫 매수일 추적
    trade_done = False       # ★ 이 트리거에서 매매 완료 플래그

    def _reset():
        nonlocal state, pull, went_below_mid, went_below_14
        nonlocal prev_ref_below_58, prev_ref_below_mid, prev_ref_below_14
        nonlocal above_34, scen, buy1_ok, buy2_ok, tot_cost, tot_qty
        nonlocal first_buy_dt, trade_done, sig_dt
        state = ST_IDLE; pull = False
        went_below_mid = went_below_14 = False
        prev_ref_below_58 = prev_ref_below_mid = prev_ref_below_14 = False
        above_34 = True; scen = ""; sig_dt = ""
        buy1_ok = buy2_ok = False; tot_cost = 0.0; tot_qty = 0
        first_buy_dt = ""; trade_done = False

    def _mk_trade(sell_dt, sell_p, sell_type):
        avg = tot_cost / tot_qty if tot_qty else 0
        sa = sell_p * tot_qty; pnl = sa - tot_cost
        roi = (sell_p / avg - 1) * 100 if avg else 0
        trades.append({
            "trigger_dt": days[ti_idx]["dt"], "signal_dt": sig_dt, "scen": scen,
            "base_lower": b_lo, "base_upper": b_up,
            "buy_dt": first_buy_dt, "avg_price": avg,
            "total_cost": tot_cost, "total_qty": tot_qty,
            "sell_dt": sell_dt, "sell_price": sell_p, "sell_type": sell_type,
            "sell_amount": sa, "pnl": pnl, "roi_pct": roi,
        })

    for i in range(1, n):
        d = days[i]; o, h, l, c = d["open"], d["high"], d["low"], d["close"]
        bull = c >= o
        ref = c if price_ref == "종가" else l

        # ═══════════ IDLE ═══════════
        if state == ST_IDLE:
            if mv[i] is None: continue
            if not _ma_up(mv, i, ma_n): continue
            if not bull: continue
            if vols[i] < vol_thr: continue
            ti_idx = i
            b_lo = _base_lower(days, i)
            peak_h = highs[i]; state = ST_TRACK
            above_34 = True; pull = False
            went_below_mid = went_below_14 = False
            prev_ref_below_58 = prev_ref_below_mid = prev_ref_below_14 = False
            trade_done = False; first_buy_dt = ""; sig_dt = ""
            continue

        # ═══════════ TRACK (상한 추적) ═══════════
        if state == ST_TRACK:
            is_up = bull
            if not is_up and i >= 1 and c > closes[i - 1]:
                is_up = True
            if is_up:
                peak_h = max(peak_h, h); continue
            if use_bear_high:
                b_up = max(peak_h, h)
            else:
                b_up = peak_h
            if b_up <= b_lo: _reset(); continue
            g = _guides(b_lo, b_up)
            det_info = {"trigger_idx": ti_idx, "trigger_dt": days[ti_idx]["dt"],
                        "confirm_dt": d["dt"], "base_lower": b_lo, "base_upper": b_up, "guides": dict(g)}
            dets.append(det_info)
            state = ST_MON; continue

        # ═══════════ POS (보유 중 — 매도 우선) ═══════════
        if state == ST_POS:
            avg = tot_cost / tot_qty if tot_qty else 0
            sold = False

            # ── 익절 체크 ──
            if scen == "S1": tp_p = g["up"]
            elif scen == "S2": tp_p = g["p78"]
            else: tp_p = g["p58"]

            if h >= tp_p:
                lbl = {"S1": "익절(상한)", "S2": "익절(7/8)", "S3": "익절(5/8)"}[scen]
                tlog.append({"trigger_dt": days[ti_idx]["dt"], "action": lbl,
                             "dt": d["dt"], "price": tp_p, "qty": tot_qty, "scen": scen})
                _mk_trade(d["dt"], tp_p, lbl); sold = True

            # ── 손절 체크 ──
            if not sold:
                sl_hit = False; sl_p = c; sl_lbl = ""
                if scen == "S1" and c < g["mid"]:
                    sl_hit = True; sl_lbl = "손절(mid)"
                elif scen == "S2" and c < g["p14"]:
                    sl_hit = True; sl_lbl = "손절(1/4)"
                elif scen == "S3" and c < g["lo"]:
                    sl_hit = True; sl_lbl = "손절(하한)"
                if sl_hit:
                    tlog.append({"trigger_dt": days[ti_idx]["dt"], "action": sl_lbl,
                                 "dt": d["dt"], "price": sl_p, "qty": tot_qty, "scen": scen})
                    _mk_trade(d["dt"], sl_p, sl_lbl); sold = True

            if sold:
                _reset(); continue  # ★ 완전 종료 → IDLE (같은 트리거 재진입 불가)

            # ── 매도 없으면 → buy2 미체결분 체결 시도 (같은 날 매도 안 했으므로 OK) ──
            if not buy2_ok and l <= buy2_p:
                fp = buy2_p; half = invest // 2
                q2 = int(half / fp) if fp > 0 else 0
                if q2 > 0:
                    buy2_ok = True; tot_cost += fp * q2; tot_qty += q2
                    tlog.append({"trigger_dt": days[ti_idx]["dt"], "action": f"매수2차({scen})",
                                 "dt": d["dt"], "price": fp, "qty": q2, "scen": scen})
            continue

        # ═══════════ BUY_WAIT (매수 대기) ═══════════
        if state == ST_BUY_WAIT:
            elapsed = i - watch_start

            # 매수 중단 체크
            cancel = False
            if scen == "S1" and c > g["up"]: cancel = True
            if scen == "S2" and c > g["p78"]: cancel = True
            if scen == "S3" and c > g["p58"]: cancel = True
            if elapsed > max_watch: cancel = True

            if cancel:
                tlog.append({"trigger_dt": days[ti_idx]["dt"], "action": f"매수중단({scen})",
                             "dt": d["dt"], "price": 0, "qty": 0, "scen": scen})
                if tot_qty > 0:
                    # ★ 부분매수 상태에서 중단 → 시장가 청산 후 완전 종료
                    _mk_trade(d["dt"], c, "매수중단청산")
                    _reset(); continue
                # 매수 0주 → 눌림/감시 복귀 (같은 패턴에서 다른 시나리오 가능)
                scen = ""
                buy1_ok = buy2_ok = False; buy1_p = buy2_p = 0.0
                if c < g["p34"]:
                    pull = True; state = ST_PULL
                else:
                    pull = False; state = ST_MON
                continue

            # ── 매수 시도 ──
            bought_today = False

            # 1차 매수
            if not buy1_ok and l <= buy1_p:
                fp = min(o, buy1_p); half = invest // 2
                q1 = int(half / fp) if fp > 0 else 0
                if q1 > 0:
                    buy1_ok = True; tot_cost += fp * q1; tot_qty += q1
                    if not first_buy_dt: first_buy_dt = d["dt"]  # ★ 첫 매수일 기록
                    tlog.append({"trigger_dt": days[ti_idx]["dt"], "action": f"매수1차({scen})",
                                 "dt": d["dt"], "price": fp, "qty": q1, "scen": scen})
                    bought_today = True

            # 2차 매수
            if not buy2_ok and l <= buy2_p:
                fp = buy2_p; half = invest // 2
                q2 = int(half / fp) if fp > 0 else 0
                if q2 > 0:
                    buy2_ok = True; tot_cost += fp * q2; tot_qty += q2
                    if not first_buy_dt: first_buy_dt = d["dt"]
                    tlog.append({"trigger_dt": days[ti_idx]["dt"], "action": f"매수2차({scen})",
                                 "dt": d["dt"], "price": fp, "qty": q2, "scen": scen})
                    bought_today = True

            if tot_qty > 0 and bought_today:
                state = ST_POS  # 매수일은 매도 안 함
            continue

        # ═══════════ MON (감시) ═══════════
        if state == ST_MON:
            # ★ 이 트리거에서 매매 완료 → IDLE
            if trade_done: _reset(); continue

            # 패턴 종료: 종가 < mid (매수 전)
            if c < g["mid"]:
                tlog.append({"trigger_dt": days[ti_idx]["dt"], "action": "패턴종료(mid이하)",
                             "dt": d["dt"], "price": c, "qty": 0, "scen": ""})
                _reset(); continue

            # high > upper but close ≤ upper → 상한 확대
            if h > b_up and c <= b_up:
                b_up = h; g = _guides(b_lo, b_up)
                det_info["base_upper"] = b_up; det_info["guides"] = dict(g)
                if dets and dets[-1]["trigger_dt"] == days[ti_idx]["dt"]:
                    dets[-1]["base_upper"] = b_up; dets[-1]["guides"] = dict(g)
                continue

            if above_34:
                if c < g["p34"]:
                    above_34 = False; pull = True
                    went_below_mid = went_below_14 = False
                    prev_ref_below_58 = prev_ref_below_mid = prev_ref_below_14 = False
                    state = ST_PULL; continue
                if c > b_up:
                    peak_h = h; state = ST_TRACK; continue
            else:
                if c < g["p34"]:
                    pull = True; state = ST_PULL; continue
                above_34 = True
            continue

        # ═══════════ PULL (눌림 — 시나리오 탐색) ═══════════
        if state == ST_PULL:
            # ★ 이 트리거에서 매매 완료 → IDLE
            if trade_done: _reset(); continue

            ref_val = c if price_ref == "종가" else l

            # went_below 갱신
            if ref_val < g["mid"]: went_below_mid = True
            if ref_val < g["p14"]: went_below_14 = True

            # ── S1: mid 위 유지 + 5/8 bounce ──
            if not went_below_mid:
                if prev_ref_below_58 and bull and c > g["p58"]:
                    scen = "S1"; sig_idx = i; sig_dt = d["dt"]
                    buy1_p = c; buy2_p = (o + c) / 2
                    watch_start = i; state = ST_BUY_WAIT
                    prev_ref_below_58 = False
                    tlog.append({"trigger_dt": days[ti_idx]["dt"], "action": "시그널(S1:고가)",
                                 "dt": d["dt"], "price": c, "qty": 0, "scen": "S1"})
                    continue
                prev_ref_below_58 = (ref_val < g["p58"])
            else:
                # ── S2: mid 아래 → mid 복귀 ──
                if not went_below_14:
                    if prev_ref_below_mid and bull and c > g["mid"]:
                        scen = "S2"; sig_idx = i; sig_dt = d["dt"]
                        buy1_p = c; buy2_p = (o + c) / 2
                        watch_start = i; state = ST_BUY_WAIT
                        prev_ref_below_mid = False
                        tlog.append({"trigger_dt": days[ti_idx]["dt"], "action": "시그널(S2:중가)",
                                     "dt": d["dt"], "price": c, "qty": 0, "scen": "S2"})
                        continue
                    prev_ref_below_mid = (ref_val < g["mid"])
                else:
                    # ── S3: 1/4 아래 → 1/4 복귀 ──
                    if prev_ref_below_14 and bull and c > g["p14"]:
                        scen = "S3"; sig_idx = i; sig_dt = d["dt"]
                        buy1_p = c; buy2_p = (o + c) / 2
                        watch_start = i; state = ST_BUY_WAIT
                        prev_ref_below_14 = False
                        tlog.append({"trigger_dt": days[ti_idx]["dt"], "action": "시그널(S3:저가)",
                                     "dt": d["dt"], "price": c, "qty": 0, "scen": "S3"})
                        continue
                    prev_ref_below_14 = (ref_val < g["p14"])

            # 상한 돌파 → 갱신 루프
            if c > b_up:
                peak_h = h; state = ST_TRACK; pull = False; above_34 = True
            continue

    # ── 미청산 ──
    if state == ST_POS and tot_qty > 0:
        _mk_trade(days[-1]["dt"], days[-1]["close"], "보유중")
    elif state == ST_BUY_WAIT and tot_qty > 0:
        _mk_trade(days[-1]["dt"], days[-1]["close"], "매수대기중")

    return dets, trades, tlog


# ═══════════════════════════════════════════════════════
#  enriched + 차트
# ═══════════════════════════════════════════════════════
def _enrich(days, ma_p):
    c = [d["close"] for d in days]; m = _ma(c, ma_p)
    return [{**d, f"ma{ma_p}": m[i]} for i, d in enumerate(days)]

def _chart(en, dets, trades, tlog, name, ma_p, pd_=0):
    df = pd.DataFrame(en); df["dt"] = pd.to_datetime(df["dt"])
    if pd_ > 0:
        df = df[df["dt"] >= datetime.now() - timedelta(days=pd_)].copy().reset_index(drop=True)
    if df.empty:
        fig = go.Figure(); fig.add_annotation(text="데이터 없음", showarrow=False); return fig

    df["bull"] = df["close"] >= df["open"]
    ts = {d["trigger_dt"] for d in dets}
    ds = df["dt"].dt.strftime("%Y-%m-%d")
    df["trig"] = ds.isin(ts)

    vc = [COLORS["trig"] if row["trig"] else (COLORS["bv"] if row["bull"] else COLORS["bev"])
          for _, row in df.iterrows()]

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.02, row_heights=[0.75, 0.25])
    fig.add_trace(go.Candlestick(
        x=df["dt"], open=df["open"], high=df["high"], low=df["low"], close=df["close"],
        increasing=dict(line=dict(color=COLORS["bull"]), fillcolor=COLORS["bull"]),
        decreasing=dict(line=dict(color=COLORS["bear"]), fillcolor=COLORS["bear"]),
        name="일봉"), row=1, col=1)

    mc = f"ma{ma_p}"
    if mc in df.columns:
        clr = COLORS["ma120"] if ma_p == 120 else COLORS["ma240"]
        fig.add_trace(go.Scatter(x=df["dt"], y=df[mc], name=f"MA{ma_p}",
            line=dict(color=clr, width=1.2, dash="dash"), hoverinfo="skip"), row=1, col=1)

    fig.add_trace(go.Bar(x=df["dt"], y=df["volume"], name="거래량",
                         marker_color=vc, opacity=0.55), row=2, col=1)

    # 트리거 마커
    dt_ = df[df["trig"]]
    if not dt_.empty:
        fig.add_trace(go.Scatter(x=dt_["dt"], y=dt_["high"] * 1.04, mode="markers", name="★트리거",
            marker=dict(symbol="star", size=14, color=COLORS["trig"], line=dict(width=1, color="#F9A825")),
            hovertemplate="%{x|%Y-%m-%d}<br><b>★트리거</b><extra></extra>"), row=1, col=1)

    # 가이드선
    for det in dets:
        gg = det.get("guides", {})
        dt0 = pd.to_datetime(det["trigger_dt"]); dt1 = df["dt"].iloc[-1]
        for v, cl, lb, da in [
            (det["base_upper"], COLORS["upper"], "상한", "dot"),
            (det["base_lower"], COLORS["lower"], "하한", "dot"),
            (gg.get("p34"), COLORS["g34"], "3/4", "dashdot"),
            (gg.get("mid"), COLORS["g12"], "1/2", "dash"),
            (gg.get("p14"), COLORS["g14"], "1/4", "dashdot"),
            (gg.get("p58"), COLORS["g58"], "5/8", "dot"),
            (gg.get("p78"), COLORS["g78"], "7/8", "dot"),
        ]:
            if v:
                fig.add_shape(type="line", x0=dt0, x1=dt1, y0=v, y1=v,
                              line=dict(color=cl, width=0.8, dash=da), row=1, col=1)

    # 매수/매도 마커
    for e in tlog:
        edt = pd.to_datetime(e["dt"])
        if "매수" in e.get("action", ""):
            fig.add_trace(go.Scatter(x=[edt], y=[e["price"] * 0.97], mode="markers",
                showlegend=False, name=e["action"],
                marker=dict(symbol="triangle-up", size=12, color=COLORS["buy"]),
                hovertemplate=f"%{{x|%Y-%m-%d}}<br>{e['action']}<br>{e['price']:,.0f}<extra></extra>"), row=1, col=1)
        elif "익절" in e.get("action", ""):
            fig.add_trace(go.Scatter(x=[edt], y=[e["price"] * 1.03], mode="markers",
                showlegend=False, name=e["action"],
                marker=dict(symbol="triangle-down", size=12, color=COLORS["tp"]),
                hovertemplate=f"%{{x|%Y-%m-%d}}<br>{e['action']}<br>{e['price']:,.0f}<extra></extra>"), row=1, col=1)
        elif "손절" in e.get("action", ""):
            fig.add_trace(go.Scatter(x=[edt], y=[e["price"] * 1.03], mode="markers",
                showlegend=False, name=e["action"],
                marker=dict(symbol="triangle-down", size=12, color=COLORS["sl"]),
                hovertemplate=f"%{{x|%Y-%m-%d}}<br>{e['action']}<br>{e['price']:,.0f}<extra></extra>"), row=1, col=1)
        elif "시그널" in e.get("action", ""):
            fig.add_trace(go.Scatter(x=[edt], y=[e["price"] * 1.04], mode="markers",
                showlegend=False, name=e["action"],
                marker=dict(symbol="diamond", size=10, color="#FFAB00"),
                hovertemplate=f"%{{x|%Y-%m-%d}}<br>{e['action']}<br>{e['price']:,.0f}<extra></extra>"), row=1, col=1)

    fig.update_layout(
        title=f"📊 {name} — 눌림매수 v3 (MA{ma_p})", template="plotly_dark", height=680,
        paper_bgcolor=COLORS["bg"], plot_bgcolor=COLORS["bg"], font=dict(color=COLORS["txt"]),
        xaxis_rangeslider_visible=False, showlegend=True,
        legend=dict(orientation="h", y=1.06, x=0, font=dict(size=9)),
        margin=dict(l=50, r=20, t=80, b=10))
    for rn in (1, 2):
        fig.update_yaxes(row=rn, col=1, gridcolor=COLORS["grid"], zeroline=False)
    at = set(df["dt"]); cal = pd.bdate_range(df["dt"].min(), df["dt"].max())
    nt = [d for d in cal if d not in at]
    for rn in (1, 2):
        fig.update_xaxes(row=rn, col=1, gridcolor=COLORS["grid"], zeroline=False, showgrid=False,
                         rangebreaks=[dict(values=[d.strftime("%Y-%m-%d") for d in nt])])
    fig.update_xaxes(row=2, col=1, tickformat="%y/%m/%d")
    return fig


# ═══════════════════════════════════════════════════════
#  통계
# ═══════════════════════════════════════════════════════
def _filt(dets, trd, tlog, pd_):
    if pd_ <= 0: return dets, trd, tlog
    cut = (datetime.now() - timedelta(days=pd_)).strftime("%Y-%m-%d")
    fd = [d for d in dets if d["trigger_dt"] >= cut]
    ft = [t for t in trd if t["trigger_dt"] >= cut]
    ts = {t["trigger_dt"] for t in ft}
    fl = [e for e in tlog if e["trigger_dt"] in ts]
    return fd, ft, fl

def _stats(trd):
    if not trd: return {"n": 0, "w": 0, "l": 0, "h": 0, "wr": 0, "ar": 0, "tp": 0, "tl": 0, "pnl": 0}
    w = [t for t in trd if "익절" in t["sell_type"]]
    lo = [t for t in trd if "손절" in t["sell_type"]]
    ho = [t for t in trd if "보유" in t["sell_type"] or "대기" in t["sell_type"]]
    cl = [t for t in trd if t["sell_type"] not in ("보유중", "매수대기중")]
    pr = sum(t["pnl"] for t in cl if t["pnl"] > 0)
    ls = sum(t["pnl"] for t in cl if t["pnl"] < 0)
    wa = [t for t in cl if t["pnl"] > 0]
    return {"n": len(trd), "w": len(w), "l": len(lo), "h": len(ho),
            "wr": (len(wa) / len(cl) * 100) if cl else 0,
            "ar": (sum(t["roi_pct"] for t in cl) / len(cl)) if cl else 0,
            "tp": pr, "tl": ls, "pnl": pr + ls}

def _sdf(trd):
    rows = []
    for i, t in enumerate(trd):
        pn = t.get("pnl", 0)
        rows.append({
            "No": i + 1, "트리거": t["trigger_dt"], "시그널": t.get("signal_dt", ""),
            "시나리오": t["scen"],
            "하한": f"{t['base_lower']:,.0f}", "상한": f"{t['base_upper']:,.0f}",
            "매수일": t.get("buy_dt", ""), "평균매수가": f"{t['avg_price']:,.0f}" if t["avg_price"] else "-",
            "투자금": f"{t['total_cost']:,.0f}",
            "매도일": t.get("sell_dt", ""), "매도유형": t["sell_type"],
            "매도금": f"{t.get('sell_amount', 0):,.0f}",
            "손익금": f"{pn:+,.0f}", "수익률": f"{t['roi_pct']:+.1f}%",
        })
    return pd.DataFrame(rows)


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

def _r1(nm, tk, en, dets, trd, tlog, map_, pd_, ksuf=""):
    dets, trd, tlog = _filt(dets, trd, tlog, pd_)
    s = _stats(trd)
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("트레이드", f"{s['n']}건"); c2.metric("익절", f"{s['w']}건")
    c3.metric("손절", f"{s['l']}건"); c4.metric("승률", f"{s['wr']:.0f}%")
    c5.metric("평균수익률", f"{s['ar']:+.1f}%")
    c6, c7, c8 = st.columns(3)
    ic = "🟢" if s["pnl"] >= 0 else "🔴"
    c6.metric("수익금", f"{s['tp']:+,.0f}원"); c7.metric("손실금", f"{s['tl']:,.0f}원")
    c8.metric(f"{ic} 순손익", f"{s['pnl']:+,.0f}원")
    if s["h"] > 0: st.info(f"📌 보유/대기 {s['h']}건")
    fig = _chart(en, dets, trd, tlog, nm, map_, pd_)
    uk = f"{ksuf}_{tk}"
    st.plotly_chart(fig, use_container_width=True, key=f"c3{uk}",
        config={"displayModeBar": True, "displaylogo": False, "scrollZoom": True,
                "modeBarButtonsToRemove": ["lasso2d", "select2d", "autoScale2d", "toggleSpikelines"]})
    if trd:
        st.markdown("#### 📋 트레이드 상세")
        st.dataframe(_sdf(trd), use_container_width=True, hide_index=True, key=f"t3{uk}")
        csv = _sdf(trd).to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
        st.download_button("📥 CSV", csv, f"{nm}_scan3.csv", "text/csv", key=f"d3{uk}")
        sc = {}
        for t in trd: sc[t["scen"]] = sc.get(t["scen"], 0) + 1
        st.markdown("**시나리오별:** " + " / ".join(f"{k}: {v}건" for k, v in sc.items() if v > 0))
    elif dets:
        st.success(f"패턴 {len(dets)}개 (매수 미발생)")
    else:
        st.info("패턴 미탐지")

def _rm(results, map_, pd_, ksuf=""):
    gp = gl = gn = gw = glo = 0; ov = []
    for it in results:
        if it.get("error"): continue
        _, ft, _ = _filt(it.get("dets", []), it["trd"], it.get("tlog", []), pd_)
        s = _stats(ft)
        gp += s["tp"]; gl += s["tl"]; gn += s["n"]; gw += s["w"]; glo += s["l"]
        if s["n"] == 0 and not it.get("dets"): continue
        ov.append({"종목": f"{it['name']}({it['tk']})", "패턴": len(it.get("dets", [])),
                   "트레이드": s["n"], "익절": s["w"], "손절": s["l"],
                   "승률": f"{s['wr']:.0f}%", "평균수익률": f"{s['ar']:+.1f}%",
                   "수익금": f"{s['tp']:+,.0f}", "손실금": f"{s['tl']:,.0f}",
                   "순손익": f"{s['pnl']:+,.0f}"})
    gpnl = gp + gl
    st.markdown("#### 📊 전체 종합")
    g1, g2, g3, g4, g5, g6 = st.columns(6)
    g1.metric("총트레이드", f"{gn}건"); g2.metric("총익절", f"{gw}건"); g3.metric("총손절", f"{glo}건")
    g4.metric("수익금", f"{gp:+,.0f}원"); g5.metric("손실금", f"{gl:,.0f}원")
    ic = "🟢" if gpnl >= 0 else "🔴"
    g6.metric(f"{ic} 총순손익", f"{gpnl:+,.0f}원")
    if ov:
        st.dataframe(pd.DataFrame(ov), use_container_width=True, hide_index=True)
        csv = pd.DataFrame(ov).to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
        st.download_button("📥 종합CSV", csv, "scan3_summary.csv", "text/csv", key=f"gcsv{ksuf}")
    errs = [r for r in results if r.get("error")]
    if errs:
        with st.expander(f"⚠️ 오류 {len(errs)}건"):
            for e in errs: st.warning(f"{e['query']}: {e['error']}")


# ═══════════════════════════════════════════════════════
#  자동 실행
# ═══════════════════════════════════════════════════════
def _auto_run(token, queries, infos, days_map, vol_pcts, ma_list, invest,
              price_ref, max_watch, use_bear_high, min_cap, cd):
    """MA × vol_pct 조합별 요약 테이블 (거래량 범위 기반)"""
    rows = []
    combos = [(ma, vp) for ma in ma_list for vp in vol_pcts]
    pg = st.progress(0)
    for ci, (ma_p, vp) in enumerate(combos):
        pg.progress((ci + 1) / len(combos), f"MA{ma_p} / {vp}%")
        all_trades = []
        for q in queries:
            info = infos.get(q)
            if not info or info.get("error"): continue
            days = days_map.get(q)
            if not days: continue
            # 종목별 거래량 범위 산출
            vm, vx = info.get("vol_min", 0), info.get("vol_max", 0)
            if vx <= 0:
                vm, vx = _calc_vol_range(days, cd)
            if vx <= 0: continue
            vt = int(_vol_threshold(vm, vx, vp))
            if vt <= 0: continue
            _, trd, _ = _analyze(days, ma_p, vt, invest, price_ref, max_watch, use_bear_high)
            if cd > 0:
                _, trd, _ = _filt([], trd, [], cd)
            all_trades.extend(trd)
        s = _stats(all_trades)
        rows.append({
            "MA": f"MA{ma_p}", "거래비율": f"{vp}%",
            "트레이드": s["n"], "익절": s["w"], "손절": s["l"],
            "익절성공율": f"{(s['w']/s['n']*100):.1f}%" if s["n"] else "-",
            "평균수익률": f"{s['ar']:+.1f}%",
            "누적수익": f"{s['tp']:+,.0f}",
            "누적손실": f"{s['tl']:,.0f}",
            "누적손익": f"{s['pnl']:+,.0f}",
        })
    pg.empty()
    if rows:
        st.markdown("#### 🤖 자동 실행 결과")
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    else:
        st.warning("분석 결과 없음")


# ═══════════════════════════════════════════════════════
#  메인
# ═══════════════════════════════════════════════════════
def main():
    st.set_page_config(page_title="패턴 스캔 v3", page_icon="📊", layout="wide",
                       initial_sidebar_state="expanded")
    st.markdown(_CSS, unsafe_allow_html=True)

    with st.sidebar:
        st.markdown("## ⚙️ 눌림매수 스캔 v3")
        input_mode = st.radio("입력", ["직접 입력", "파일 업로드"], horizontal=True)
        ti = uf = None
        if input_mode == "직접 입력":
            ti = st.text_input("종목명/코드", placeholder="삼성전자")
        else:
            uf = st.file_uploader("종목목록(.txt/.md)", type=["txt", "md"])

        st.markdown("---")
        st.markdown("##### 📐 기본")
        cp = st.selectbox("차트 표시 기간", PERIOD_OPTIONS, index=len(PERIOD_OPTIONS) - 1,
                          help="거래량 min/max 산출 기간도 동일 적용")
        min_cap = st.number_input("최소 시총(억원)", 100, 100000, 1000, 100)

        st.markdown("---")
        auto_mode = st.checkbox("🤖 자동 실행 모드", value=False,
                                help="MA × 거래비율 조합 자동 분석")

        if not auto_mode:
            ma_p = st.selectbox("이동평균선", [240, 120], format_func=lambda x: f"MA{x}")
            trade_pct = st.select_slider("거래량 기준 비율(%)",
                                          options=list(range(10, 110, 10)), value=30,
                                          help="기간 내 거래량 min(0%)~max(100%) 범위에서 비율 위치")
        else:
            st.markdown("##### 🤖 자동 실행 설정")
            auto_ma = st.multiselect("MA 선택", [120, 240], default=[120, 240],
                                      format_func=lambda x: f"MA{x}")
            auto_pcts = st.multiselect("거래량 기준 비율(%)",
                                        list(range(10, 110, 10)), default=[10, 30, 50])
            ma_p = 240; trade_pct = 30

        st.markdown("---")
        st.markdown("##### 💰 매수 설정")
        invest = st.number_input("총 투자금(원)", 100_000, 100_000_000, 2_000_000, 100_000)
        st.caption(f"1차: {invest // 2:,.0f}원 / 2차: {invest // 2:,.0f}원")
        price_ref = st.radio("눌림 판단 기준", ["종가", "저가"], horizontal=True)

        st.markdown("---")
        st.markdown("##### ⚙️ 고급")
        max_watch = st.number_input("최대 감시기간(거래일)", 10, 500, 60, 10)
        use_bear_high = st.checkbox("상한 확정 시 음봉고가 포함",
                                     value=True, help="OFF: peak_high만 / ON: max(peak, 음봉고가)")
        max_pages = st.slider("OHLCV 페이지", 10, 80, 40, 5)

        st.markdown("---")
        st.markdown(f"""<div style="font-size:.72rem;color:#888;line-height:1.8">
        <b style="color:#FFD600">★</b> 트리거: 양봉+거래량(기간min~max%)+ MA상승<br>
        <b style="color:{COLORS['upper']}">━</b> 상한
        <b style="color:{COLORS['lower']}">━</b> 하한<br>
        <b style="color:{COLORS['g34']}">━</b> 3/4
        <b style="color:{COLORS['g12']}">━</b> 1/2
        <b style="color:{COLORS['g14']}">━</b> 1/4<br>
        <b style="color:{COLORS['g58']}">━</b> 5/8
        <b style="color:{COLORS['g78']}">━</b> 7/8<br><br>
        <b>S1(고가)</b>: mid위 유지+5/8 bounce → 익절:상한 / 손절:mid<br>
        <b>S2(중가)</b>: mid아래→mid 복귀 → 익절:7/8 / 손절:1/4<br>
        <b>S3(저가)</b>: 1/4아래→1/4 복귀 → 익절:5/8 / 손절:하한
        </div>""", unsafe_allow_html=True)

        scan = st.button("🔍 스캔 실행", use_container_width=True, type="primary")

    # ── 세션 ──
    for k in ["s3r", "s3m", "s3p", "s3ma"]:
        if k not in st.session_state: st.session_state[k] = None
    cd = PERIOD_MAP.get(cp, 0)

    if scan:
        st.session_state.s3r = None

        if input_mode == "직접 입력":
            if not ti: st.warning("종목명 입력 필요"); return
            try:
                tk, nm = _resolve(ti)
                token = core.get_token(core.APP_KEY, core.APP_SECRET)
                cap = _get_cap(token, tk)
                if cap > 0 and cap < min_cap:
                    st.warning(f"시총 {cap:,.0f}억 < {min_cap:,.0f}억"); return
                days = _fetch(token, tk, max_pages)

                # ── 거래량 기준 산출 ──
                vol_min, vol_max = _calc_vol_range(days, cd)
                if vol_max <= 0:
                    st.error("거래량 데이터 없음"); return

                if auto_mode:
                    _auto_run(token, [ti], {ti: {"tk": tk, "nm": nm}},
                              {ti: days}, auto_pcts, auto_ma,
                              invest, price_ref, max_watch, use_bear_high, min_cap, cd)
                else:
                    vt = int(_vol_threshold(vol_min, vol_max, trade_pct))
                    st.caption(f"📊 거래량 범위: {vol_min:,} ~ {vol_max:,} → 기준({trade_pct}%): {vt:,}")
                    dets, trd, tlog = _analyze(days, ma_p, vt, invest, price_ref,
                                               max_watch, use_bear_high)
                    en = _enrich(days, ma_p)
                    st.session_state.s3r = {"tk": tk, "name": nm, "en": en,
                                            "dets": dets, "trd": trd, "tlog": tlog}
                    st.session_state.s3m = "single"
                    st.session_state.s3p = cd; st.session_state.s3ma = ma_p
            except Exception as e:
                st.error(f"오류: {e}"); st.exception(e); return

        else:  # 파일 업로드
            if not uf: st.warning("파일 업로드 필요"); return
            qs = _parse_file(uf.read().decode("utf-8"))
            if not qs: st.error("종목 없음"); return
            st.info(f"📂 {len(qs)}개: {', '.join(qs[:20])}{'...' if len(qs) > 20 else ''}")
            token = core.get_token(core.APP_KEY, core.APP_SECRET)

            # 1차: 시총 체크 + OHLCV 수집
            infos = {}; days_map = {}; results = []
            pg0 = st.progress(0)
            for idx, q in enumerate(qs):
                pg0.progress((idx + 1) / len(qs), f"데이터 수집 ({idx + 1}/{len(qs)}) {q}")
                try:
                    tk, nm = _resolve(q)
                    cap = _get_cap(token, tk)
                    if cap > 0 and cap < min_cap:
                        infos[q] = {"error": f"시총 {cap:,.0f}억 < {min_cap:,.0f}억"}
                        results.append({"query": q, "tk": tk, "name": nm, "dets": [], "trd": [],
                                        "tlog": [], "en": [], "error": infos[q]["error"]})
                        continue
                    days = _fetch(token, tk, max_pages)
                    vol_min, vol_max = _calc_vol_range(days, cd)
                    if vol_max <= 0:
                        infos[q] = {"error": "거래량 데이터 없음"}
                        results.append({"query": q, "tk": tk, "name": nm, "dets": [], "trd": [],
                                        "tlog": [], "en": [], "error": "거래량 데이터 없음"})
                        continue
                    infos[q] = {"tk": tk, "nm": nm, "vol_min": vol_min, "vol_max": vol_max, "error": None}
                    days_map[q] = days
                except Exception as e:
                    infos[q] = {"error": str(e)}
                    results.append({"query": q, "tk": "", "name": q, "dets": [], "trd": [],
                                    "tlog": [], "en": [], "error": str(e)})
            pg0.empty()

            if auto_mode:
                valid_qs = [q for q in qs if infos.get(q) and not infos[q].get("error")]
                _auto_run(token, valid_qs, infos, days_map, auto_pcts, auto_ma,
                          invest, price_ref, max_watch, use_bear_high, min_cap, cd)
                errs = [q for q in qs if infos.get(q, {}).get("error")]
                if errs:
                    with st.expander(f"⚠️ 오류 {len(errs)}건"):
                        for q in errs: st.warning(f"{q}: {infos[q]['error']}")
            else:
                summary_area = st.container()
                pg = st.progress(0)
                for idx, q in enumerate(qs):
                    inf = infos.get(q)
                    if not inf or inf.get("error"): continue
                    pg.progress((idx + 1) / len(qs), f"분석 ({idx + 1}/{len(qs)}) {q}")
                    tk = inf["tk"]; nm = inf["nm"]
                    vt = int(_vol_threshold(inf["vol_min"], inf["vol_max"], trade_pct))
                    days = days_map.get(q)
                    if not days: continue
                    dets, trd, tlog_ = _analyze(days, ma_p, vt, invest, price_ref,
                                                max_watch, use_bear_high)
                    en = _enrich(days, ma_p)
                    it = {"query": q, "tk": tk, "name": nm, "en": en,
                          "dets": dets, "trd": trd, "tlog": tlog_, "error": None}
                    results.append(it)
                    if dets or trd:
                        _, ft, _ = _filt(dets, trd, tlog_, cd)
                        s = _stats(ft)
                        ic_ = "🟢" if s["ar"] >= 0 else "🔴"
                        with st.expander(f"{ic_} ({idx+1}/{len(qs)}) {nm}({tk}) — {s['n']}건 / {s['pnl']:+,.0f}원", expanded=False):
                            _r1(nm, tk, en, dets, trd, tlog_, ma_p, cd, ksuf=f"_s{idx}")
                pg.empty()
                with summary_area:
                    _rm(results, ma_p, cd, ksuf="_s")
                st.session_state.s3r = results
                st.session_state.s3m = "multi"; st.session_state.s3p = cd; st.session_state.s3ma = ma_p

    # ── 결과 표시 ──
    if st.session_state.s3r is not None:
        pd_ = st.session_state.s3p; mp = st.session_state.s3ma
        if st.session_state.s3m == "single":
            r = st.session_state.s3r
            st.markdown(f"### {r['name']} ({r['tk']})")
            _r1(r["name"], r["tk"], r["en"], r["dets"], r["trd"], r["tlog"], mp, pd_, ksuf="_r")
        elif st.session_state.s3m == "multi":
            results = st.session_state.s3r
            _rm(results, mp, pd_, ksuf="_r")
            found = [it for it in results if not it.get("error") and (it.get("dets") or it.get("trd"))]
            if found:
                st.markdown(f"#### 📈 패턴 탐지 ({len(found)}개)")
                for i, it in enumerate(found):
                    _, ft, _ = _filt(it.get("dets", []), it["trd"], it.get("tlog", []), pd_)
                    s = _stats(ft)
                    ic_ = "🟢" if s["ar"] >= 0 else "🔴"
                    with st.expander(f"{ic_} {it['name']}({it['tk']}) — {s['n']}건 / {s['pnl']:+,.0f}원"):
                        _r1(it["name"], it["tk"], it["en"], it.get("dets", []),
                            it["trd"], it.get("tlog", []), mp, pd_, ksuf=f"_r{i}")
    else:
        st.info("👈 설정 후 **스캔 실행**을 누르세요.")
        with st.expander("💡 로직 가이드", expanded=True):
            st.markdown("""
**[트리거]** 양봉 + 거래량 ≥ 기간 min~max 범위 비율% + MA 연속 상승

**[하한]** 트리거봉부터 역추적 → 진짜 음봉까지 최저 시가  
**[상한]** 트리거 이후 양봉 고가 추적 → 음봉 확정 시 결정

**[눌림]** 종가 < 돌파가이드(3/4)

**[S1 고가]** mid 위 유지 + 5/8 bounce → 익절: 상한 / 손절: mid미만  
**[S2 중가]** mid 아래 → mid 복귀 → 익절: 7/8 / 손절: 1/4미만  
**[S3 저가]** 1/4 아래 → 1/4 복귀 → 익절: 5/8 / 손절: 하한미만

**[공통]** 먼저 충족된 시나리오 1개만 발동 / 같은 날 매수+매도 금지 / 트리거당 1회 매매
""")


if __name__ == "__main__":
    main()