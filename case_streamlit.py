"""
시나리오 기반 돌파 시그널 분석기 — case_streamlit.py
══════════════════════════════════════════════════════
- 시나리오 1: 관심가격 돌파 추적
- 시나리오 2: 상승가능성 복합 판별
- 단일 종목 직접 입력 + 다종목 파일 업로드 지원
- 키움 REST API (ka10081) 일봉 OHLCV 기반

실행: streamlit run case_streamlit.py
"""

from __future__ import annotations

import os
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
    "bull":          "#E53935",
    "bear":          "#1E88E5",
    "bull_vol":      "#EF9A9A",
    "bear_vol":      "#90CAF9",
    "breakout":      "#E53935",
    "followup":      "#FF9800",
    "newhigh":       "#FFD600",
    "newhigh_vol":   "#FFD600",
    "breakout_vol":  "#E53935",
    "followup_vol":  "#FF9800",
    "scenario1":     "#00E676",   # 관심가격 돌파 (녹색)
    "scenario2":     "#E040FB",   # 상승가능성 (마젠타)
    "ma20":          "#FFA726",
    "bg":            "#131722",
    "grid":          "#1E222D",
    "text":          "#D1D4DC",
}

_MA_COLORS = {60: "#AB47BC", 120: "#26A69A", 180: "#42A5F5"}
_MA_DASH = {60: "dash", 120: "dashdot", 180: "longdash"}

# 기간 맵 (공용)
PERIOD_OPTIONS = ["6개월", "1년", "1년 6개월", "2년", "2년 6개월", "3년", "4년", "5년", "전체"]
PERIOD_MAP = {
    "6개월": 180, "1년": 365, "1년 6개월": 548, "2년": 730,
    "2년 6개월": 913, "3년": 1095, "4년": 1460, "5년": 1825, "전체": 0,
}
NH_PERIOD_OPTIONS = ["비활성", "6개월", "1년", "1년 6개월", "2년", "2년 6개월", "3년", "4년", "5년"]
NH_PERIOD_MAP = {
    "비활성": 0, "6개월": 180, "1년": 365, "1년 6개월": 548, "2년": 730,
    "2년 6개월": 913, "3년": 1095, "4년": 1460, "5년": 1825,
}


# ═══════════════════════════════════════════════════════
#  유틸
# ═══════════════════════════════════════════════════════

def _to_int(v, default=0) -> int:
    if v is None:
        return default
    if isinstance(v, int):
        return v
    s = str(v).strip().replace(",", "")
    if not s:
        return default
    try:
        return int(float(s))
    except Exception:
        return default


def _parse_dt_any(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    if len(s) >= 8 and s[:8].isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    return None


def _first_non_empty(row: dict, keys: list[str]):
    for k in keys:
        if k in row and str(row.get(k)).strip() != "":
            return row.get(k)
    return None


def _resolve_ticker_and_name(query: str) -> tuple[str, str]:
    q = (query or "").strip()
    if not q:
        raise RuntimeError("종목명을 입력해 주세요.")
    if q.isdigit() and len(q) == 6:
        ticker = q
    else:
        ticker, err = core.resolve_ticker(q)
        if err or not ticker:
            raise RuntimeError(err or f"'{q}' 종목명을 확인해 주세요.")
    name = ticker
    try:
        name = (core._krx_cache.get("name_by_code") or {}).get(ticker, ticker)
    except Exception:
        pass
    return ticker, name


def _parse_ticker_file(content: str) -> list[str]:
    """txt/md 파일 내용에서 쉼표 구분 종목명 파싱"""
    tickers = []
    for part in content.replace("\n", ",").replace("\r", ",").split(","):
        t = part.strip()
        if t:
            tickers.append(t)
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

    last_err: Exception | None = None
    for body in bodies:
        try:
            res = core.call_tr_all_pages(
                token=token, api_id="ka10081", body=body,
                endpoint="/api/dostk/chart", max_pages=max_pages,
            )
            rows = res.get("rows") or []
            if not rows:
                continue

            dedup: dict[str, dict] = {}
            for r in rows:
                dt = _parse_dt_any(
                    _first_non_empty(r, ["dt", "date", "bas_dt", "base_dt", "trde_dt", "trd_dt"])
                )
                if not dt:
                    continue
                open_p  = _to_int(_first_non_empty(r, ["open_pric", "open", "stck_oprc", "opn_prc"]), 0)
                high_p  = _to_int(_first_non_empty(r, ["high_pric", "high", "stck_hgpr", "hgh_prc"]), 0)
                low_p   = _to_int(_first_non_empty(r, ["low_pric", "low", "stck_lwpr", "low_prc"]), 0)
                close_p = _to_int(_first_non_empty(r, ["close_pric", "close", "stck_clpr", "cur_prc", "cur_pric"]), 0)
                vol     = _to_int(_first_non_empty(r, ["trde_qty", "volume", "acml_vol", "acc_trde_qty"]), 0)
                if close_p <= 0:
                    continue
                if open_p <= 0:
                    open_p = close_p
                if high_p <= 0:
                    high_p = max(open_p, close_p)
                if low_p <= 0:
                    low_p = min(open_p, close_p)
                dedup[dt] = {
                    "dt": dt, "open": open_p, "high": high_p,
                    "low": low_p, "close": close_p, "volume": max(0, vol),
                }

            out = sorted(dedup.values(), key=lambda x: x["dt"])
            if out:
                return out
        except Exception as e:
            last_err = e
            continue

    raise RuntimeError(f"일봉 데이터 조회 실패: {last_err}")


# ═══════════════════════════════════════════════════════
#  기술지표
# ═══════════════════════════════════════════════════════

def _rolling_ma(values: list[int], window: int) -> list[Optional[float]]:
    out: list[Optional[float]] = [None] * len(values)
    if window <= 0:
        return out
    run_sum = 0.0
    for i, v in enumerate(values):
        run_sum += float(v)
        if i >= window:
            run_sum -= float(values[i - window])
        if i >= window - 1:
            out[i] = run_sum / float(window)
    return out


def _linear_regression_slope(y: list[float]) -> float:
    n = len(y)
    if n < 2:
        return 0.0
    x_mean = (n - 1) / 2.0
    y_mean = sum(y) / n
    num = sum((i - x_mean) * (yi - y_mean) for i, yi in enumerate(y))
    den = sum((i - x_mean) ** 2 for i in range(n))
    return num / den if den != 0 else 0.0


def _check_ma_uptrend(ma_values: list, i: int, trend_window: int) -> bool:
    if trend_window < 2 or i - trend_window + 1 < 0:
        return False
    window = ma_values[i - trend_window + 1 : i + 1]
    if not all(v is not None for v in window):
        return False
    return _linear_regression_slope([float(v) for v in window]) > 0


# ═══════════════════════════════════════════════════════
#  기본 분석 (▲돌파 / ●후속 / ★신고가)
# ═══════════════════════════════════════════════════════

def _analyze_base(
    days: list[dict],
    ma_configs: dict[int, int],
    volume_multiplier: float = 6.0,
    period_days: int = 365,
    newhigh_lookback_days: int = 365,
) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """
    Returns: (breakout_rows, followup_rows, newhigh_rows, enriched)
    """
    closes = [int(x["close"]) for x in days]
    opens = [int(x["open"]) for x in days]
    vols = [int(x["volume"]) for x in days]
    dates = [x["dt"] for x in days]
    ma20 = _rolling_ma(closes, 20)

    ma_lines: dict[int, list[Optional[float]]] = {}
    for period in ma_configs:
        ma_lines[period] = _rolling_ma(closes, period)

    cutoff = "0000-00-00"
    if period_days > 0:
        cutoff = (datetime.now(core.TZ).date() - timedelta(days=period_days)).strftime("%Y-%m-%d")

    breakout_rows, followup_rows, newhigh_rows = [], [], []
    enriched: list[dict] = []
    has_breakout_occurred = False

    for i in range(len(days)):
        d = days[i]
        row: dict = {
            "dt": d["dt"], "open": d["open"], "high": d["high"],
            "low": d["low"], "close": d["close"], "volume": d["volume"],
            "ma20": ma20[i], "signal": "",
        }
        for period, ma_vals in ma_lines.items():
            row[f"ma{period}"] = ma_vals[i]

        if i >= 1 and d["dt"] >= cutoff:
            prev_vol = vols[i - 1]
            curr_vol = vols[i]
            cond_volume = prev_vol > 0 and curr_vol >= prev_vol * volume_multiplier
            cond_bull = closes[i] >= opens[i]

            cond_all_ma_up = all(
                _check_ma_uptrend(ma_lines[p], i, tw)
                for p, tw in ma_configs.items()
            )

            prev_ma20 = ma20[i - 1]
            curr_ma20 = ma20[i]
            cond_ma20_cross = (
                prev_ma20 is not None and curr_ma20 is not None
                and closes[i - 1] <= float(prev_ma20)
                and closes[i] > float(curr_ma20)
            )

            # ▲ 돌파
            if cond_bull and cond_volume and cond_all_ma_up and cond_ma20_cross:
                row["signal"] = "breakout"
                has_breakout_occurred = True
                breakout_rows.append({
                    "dt": d["dt"], "close": closes[i], "open": opens[i],
                    "volume": curr_vol, "ratio": curr_vol / float(prev_vol),
                })
            # ● 후속 급증
            elif has_breakout_occurred and cond_bull and cond_volume and cond_all_ma_up:
                if curr_ma20 is not None and closes[i] > float(curr_ma20):
                    row["signal"] = "followup"
                    followup_rows.append({
                        "dt": d["dt"], "close": closes[i],
                        "volume": curr_vol, "ratio": curr_vol / float(prev_vol),
                    })

            # ★ 신고가
            if newhigh_lookback_days > 0 and cond_bull and cond_all_ma_up:
                lb_start = (
                    datetime.strptime(d["dt"], "%Y-%m-%d")
                    - timedelta(days=newhigh_lookback_days)
                ).strftime("%Y-%m-%d")
                past_max = 0
                for j in range(i):
                    if dates[j] >= lb_start and closes[j] > past_max:
                        past_max = closes[j]
                if past_max > 0 and closes[i] > past_max:
                    newhigh_rows.append({
                        "dt": d["dt"], "close": closes[i],
                        "volume": curr_vol, "prev_high": past_max,
                    })

        enriched.append(row)

    return breakout_rows, followup_rows, newhigh_rows, enriched


# ═══════════════════════════════════════════════════════
#  시나리오 1: 관심가격 돌파 추적
# ═══════════════════════════════════════════════════════

def _analyze_scenario1(
    days: list[dict],
    breakout_rows: list[dict],
    followup_rows: list[dict],
    newhigh_rows: list[dict],
    enriched: list[dict],
    ma_configs: dict[int, int],
    volume_multiplier: float,
) -> list[dict]:
    """
    관심기준가격 돌파 추적 (조정 후 재상승 포인트 탐색)

    트리거 (관심기준가격 설정):
      A) ▲ MA20 돌파
      B) ★신고가 + ●후속급증 동시 발생일
    → 관심기준가격 = 시가 + (종가-시가)/4

    무효화 조건 (어느 하나라도 해당 시 즉시 폐기):
      1) 시가·종가 둘 다 관심가 미만 → 하락 이탈
      2) 종가 > 트리거봉 종가 → 조정 없이 상승 (의미 없음)
    → 새 트리거 발생 전까지 비활성

    이후 조건 충족 시 ◆ 첫 1회만 표시:
      양봉 + 시가·종가 모두 ≥ 관심기준가격 + MA20위 + 거래량 + MA상승추세

    새 트리거 발생 → 관심기준가격 갱신 + 다시 1회 표시 가능
    """
    closes = [int(x["close"]) for x in days]
    opens = [int(x["open"]) for x in days]
    vols = [int(x["volume"]) for x in days]
    ma20 = _rolling_ma(closes, 20)

    ma_lines: dict[int, list[Optional[float]]] = {}
    for period in ma_configs:
        ma_lines[period] = _rolling_ma(closes, period)

    # 트리거A: ▲돌파일
    breakout_dates = {r["dt"] for r in breakout_rows}
    # 트리거B: ★신고가 + ●후속 동시 발생일
    newhigh_dates = {r["dt"] for r in newhigh_rows}
    followup_dates = {r["dt"] for r in followup_rows}
    newhigh_followup_dates = newhigh_dates & followup_dates
    # 전체 트리거 = A ∪ B
    trigger_dates = breakout_dates | newhigh_followup_dates

    if not trigger_dates:
        return []

    scenario1_rows: list[dict] = []
    ref_price: float | None = None
    trigger_close: int = 0       # 트리거봉의 종가 (무효화 판정용)
    ref_date: str = ""
    already_fired: bool = True   # 트리거 전이므로 발사 불가 상태

    for i in range(len(days)):
        d = days[i]

        # ── 트리거 발생: 관심기준가격 설정/갱신 + 1회 발사 가능으로 리셋 ──
        if d["dt"] in trigger_dates:
            diff = closes[i] - opens[i]
            ref_price = opens[i] + diff / 4.0
            trigger_close = closes[i]  # 트리거봉 종가 저장
            ref_date = d["dt"]
            already_fired = False
            continue

        # ── 관심기준가격 무효화 체크 ──
        if ref_price is not None and not already_fired:
            # 무효화1: 시가·종가 둘 다 관심가 미만 → 하락 이탈
            if opens[i] < ref_price and closes[i] < ref_price:
                ref_price = None
                already_fired = True
            # 무효화2: 종가 > 트리거봉 종가 → 조정 없이 상승 (의미 없음)
            elif closes[i] > trigger_close:
                ref_price = None
                already_fired = True

        # 관심기준가격 미설정 or 이미 1회 표시됨 or 무효화됨 → 스킵
        if ref_price is None or already_fired or i < 1:
            continue

        cond_bull = closes[i] >= opens[i]
        cond_both_above_ref = opens[i] >= ref_price and closes[i] >= ref_price
        cond_ma20_above = ma20[i] is not None and closes[i] > float(ma20[i])

        prev_vol = vols[i - 1]
        curr_vol = vols[i]
        cond_volume = prev_vol > 0 and curr_vol >= prev_vol * volume_multiplier

        cond_all_ma_up = all(
            _check_ma_uptrend(ma_lines[p], i, tw)
            for p, tw in ma_configs.items()
        )

        if cond_bull and cond_both_above_ref and cond_ma20_above and cond_volume and cond_all_ma_up:
            scenario1_rows.append({
                "dt": d["dt"],
                "close": closes[i],
                "open": opens[i],
                "volume": curr_vol,
                "ratio": curr_vol / float(prev_vol),
                "ref_price": int(ref_price),
                "ref_date": ref_date,
            })
            already_fired = True  # 1회 표시 완료 → 다음 트리거까지 중단

    return scenario1_rows


# ═══════════════════════════════════════════════════════
#  시나리오 2: 상승가능성 복합 판별
# ═══════════════════════════════════════════════════════

def _analyze_scenario2(
    breakout_rows: list[dict],
    followup_rows: list[dict],
    newhigh_rows: list[dict],
) -> list[dict]:
    """
    상승가능성 복합 판별

    ★신고가 + ●후속급증 동시 발생일에:
      해당일 기준 6개월 이내 & 마지막 클리어 이후에
      ▲돌파 ≥ 2건 AND ●후속 ≥ 2건 → ⬟ 상승가능성 시그널

    ⬟ 발생 시 카운트 클리어 → 다음 ⬟에는 새로 누적된 건만 사용
    """
    newhigh_dates = {r["dt"] for r in newhigh_rows}
    followup_dates = {r["dt"] for r in followup_rows}

    # ★신고가 + ●후속 동시 발생일 (시간순)
    both_dates = sorted(newhigh_dates & followup_dates)
    if not both_dates:
        return []

    scenario2_rows: list[dict] = []
    last_clear_dt = "0000-00-00"  # 마지막 클리어 시점

    for target_dt in both_dates:
        target_date = datetime.strptime(target_dt, "%Y-%m-%d")
        lookback_start = (target_date - timedelta(days=180)).strftime("%Y-%m-%d")

        # 유효 구간: max(6개월 전, 마지막 클리어 이후) ~ 해당일 미포함
        effective_start = max(lookback_start, last_clear_dt)

        bo_count = sum(
            1 for r in breakout_rows
            if effective_start < r["dt"] < target_dt
        )
        fu_count = sum(
            1 for r in followup_rows
            if effective_start < r["dt"] < target_dt
        )

        if bo_count >= 2 and fu_count >= 2:
            nh_info = next((r for r in newhigh_rows if r["dt"] == target_dt), {})
            fu_info = next((r for r in followup_rows if r["dt"] == target_dt), {})
            scenario2_rows.append({
                "dt": target_dt,
                "close": nh_info.get("close", fu_info.get("close", 0)),
                "volume": nh_info.get("volume", fu_info.get("volume", 0)),
                "prev_high": nh_info.get("prev_high", 0),
                "bo_count_6m": bo_count,
                "fu_count_6m": fu_count,
            })
            # ⬟ 발생 → 카운트 클리어 (이 날짜 이전 건은 다음번에 사용 불가)
            last_clear_dt = target_dt

    return scenario2_rows


# ═══════════════════════════════════════════════════════
#  통합 분석 실행
# ═══════════════════════════════════════════════════════

def run_full_analysis(
    days: list[dict],
    ma_configs: dict[int, int],
    volume_multiplier: float,
    period_days: int,
    newhigh_lookback_days: int,
    scenario: int,
) -> dict:
    """
    Returns dict with all analysis results.
    """
    breakout_rows, followup_rows, newhigh_rows, enriched = _analyze_base(
        days=days,
        ma_configs=ma_configs,
        volume_multiplier=volume_multiplier,
        period_days=period_days,
        newhigh_lookback_days=newhigh_lookback_days,
    )

    result = {
        "breakout": breakout_rows,
        "followup": followup_rows,
        "newhigh": newhigh_rows,
        "enriched": enriched,
        "scenario1": [],
        "scenario2": [],
    }

    if scenario == 1:
        result["scenario1"] = _analyze_scenario1(
            days=days,
            breakout_rows=breakout_rows,
            followup_rows=followup_rows,
            newhigh_rows=newhigh_rows,
            enriched=enriched,
            ma_configs=ma_configs,
            volume_multiplier=volume_multiplier,
        )
    elif scenario == 2:
        result["scenario2"] = _analyze_scenario2(
            breakout_rows=breakout_rows,
            followup_rows=followup_rows,
            newhigh_rows=newhigh_rows,
        )

    return result


# ═══════════════════════════════════════════════════════
#  캔들스틱 차트
# ═══════════════════════════════════════════════════════

def build_chart(
    enriched: list[dict],
    breakout_rows: list[dict],
    followup_rows: list[dict],
    newhigh_rows: list[dict],
    name: str,
    period_days: int = 365,
    ma_configs: dict[int, int] | None = None,
    scenario1_rows: list[dict] | None = None,
    scenario2_rows: list[dict] | None = None,
) -> go.Figure:
    if not ma_configs:
        ma_configs = {60: 5}
    scenario1_rows = scenario1_rows or []
    scenario2_rows = scenario2_rows or []

    df = pd.DataFrame(enriched)
    df["dt"] = pd.to_datetime(df["dt"])

    if period_days > 0:
        cutoff = datetime.now() - timedelta(days=period_days)
        df = df[df["dt"] >= cutoff].copy().reset_index(drop=True)
    else:
        df = df.copy().reset_index(drop=True)
    if df.empty:
        fig = go.Figure()
        fig.add_annotation(text="표시할 데이터가 없습니다.", showarrow=False)
        return fig

    df["is_bull"] = df["close"] >= df["open"]

    breakout_dates = set(r["dt"] for r in breakout_rows)
    followup_dates = set(r["dt"] for r in followup_rows)
    newhigh_dates = set(r["dt"] for r in newhigh_rows)
    s1_dates = set(r["dt"] for r in scenario1_rows)
    s2_dates = set(r["dt"] for r in scenario2_rows)
    dt_str = df["dt"].dt.strftime("%Y-%m-%d")
    df["is_breakout"] = dt_str.isin(breakout_dates)
    df["is_followup"] = dt_str.isin(followup_dates)
    df["is_newhigh"] = dt_str.isin(newhigh_dates)
    df["is_s1"] = dt_str.isin(s1_dates)
    df["is_s2"] = dt_str.isin(s2_dates)

    # 거래량 바 색상
    vol_colors = []
    for _, row in df.iterrows():
        if row["is_s2"]:
            vol_colors.append(COLORS["scenario2"])
        elif row["is_s1"]:
            vol_colors.append(COLORS["scenario1"])
        elif row["is_newhigh"]:
            vol_colors.append(COLORS["newhigh_vol"])
        elif row["is_breakout"]:
            vol_colors.append(COLORS["breakout_vol"])
        elif row["is_followup"]:
            vol_colors.append(COLORS["followup_vol"])
        elif row["is_bull"]:
            vol_colors.append(COLORS["bull_vol"])
        else:
            vol_colors.append(COLORS["bear_vol"])

    fig = make_subplots(
        rows=2, cols=1, shared_xaxes=True,
        vertical_spacing=0.02, row_heights=[0.75, 0.25],
    )

    # 캔들스틱
    fig.add_trace(
        go.Candlestick(
            x=df["dt"], open=df["open"], high=df["high"],
            low=df["low"], close=df["close"],
            increasing=dict(line=dict(color=COLORS["bull"]), fillcolor=COLORS["bull"]),
            decreasing=dict(line=dict(color=COLORS["bear"]), fillcolor=COLORS["bear"]),
            name="일봉", hoverinfo="x+y",
        ),
        row=1, col=1,
    )

    # MA20
    fig.add_trace(
        go.Scatter(
            x=df["dt"], y=df["ma20"], name="MA20",
            line=dict(color=COLORS["ma20"], width=1.2, dash="dot"),
            hoverinfo="skip",
        ),
        row=1, col=1,
    )

    # 선택 MA
    for period in sorted(ma_configs.keys()):
        col_name = f"ma{period}"
        if col_name not in df.columns:
            continue
        fig.add_trace(
            go.Scatter(
                x=df["dt"], y=df[col_name], name=f"MA{period}",
                line=dict(
                    color=_MA_COLORS.get(period, "#AB47BC"),
                    width=1.2, dash=_MA_DASH.get(period, "dash"),
                ),
                hoverinfo="skip",
            ),
            row=1, col=1,
        )

    # ▲ 돌파
    df_bo = df[df["is_breakout"]]
    if not df_bo.empty:
        fig.add_trace(
            go.Scatter(
                x=df_bo["dt"], y=df_bo["high"] * 1.02,
                mode="markers", name="▲ 돌파",
                marker=dict(symbol="triangle-up", size=14, color=COLORS["breakout"],
                            line=dict(width=1, color="#B71C1C")),
                hovertemplate="%{x|%Y-%m-%d}<br><b>▲ MA20 돌파</b><br>종가: %{customdata:,.0f}원<extra></extra>",
                customdata=df_bo["close"],
            ),
            row=1, col=1,
        )

    # ● 후속
    df_fu = df[df["is_followup"]]
    if not df_fu.empty:
        fig.add_trace(
            go.Scatter(
                x=df_fu["dt"], y=df_fu["high"] * 1.02,
                mode="markers", name="● 후속 급증",
                marker=dict(symbol="circle", size=11, color=COLORS["followup"],
                            line=dict(width=1, color="#E65100")),
                hovertemplate="%{x|%Y-%m-%d}<br><b>● MA20 위 급증</b><br>종가: %{customdata:,.0f}원<extra></extra>",
                customdata=df_fu["close"],
            ),
            row=1, col=1,
        )

    # ★ 신고가
    df_nh = df[df["is_newhigh"]]
    if not df_nh.empty:
        fig.add_trace(
            go.Scatter(
                x=df_nh["dt"], y=df_nh["high"] * 1.04,
                mode="markers", name="★ 신고가",
                marker=dict(symbol="star", size=14, color=COLORS["newhigh"],
                            line=dict(width=1, color="#F9A825")),
                hovertemplate="%{x|%Y-%m-%d}<br><b>★ 신고가</b><br>종가: %{customdata:,.0f}원<extra></extra>",
                customdata=df_nh["close"],
            ),
            row=1, col=1,
        )

    # ◆ 시나리오1 마커
    df_s1 = df[df["is_s1"]]
    if not df_s1.empty:
        fig.add_trace(
            go.Scatter(
                x=df_s1["dt"], y=df_s1["high"] * 1.06,
                mode="markers", name="◆ 관심가 돌파",
                marker=dict(symbol="diamond", size=13, color=COLORS["scenario1"],
                            line=dict(width=1, color="#00C853")),
                hovertemplate="%{x|%Y-%m-%d}<br><b>◆ 관심가격 돌파</b><br>종가: %{customdata:,.0f}원<extra></extra>",
                customdata=df_s1["close"],
            ),
            row=1, col=1,
        )

    # ⬟ 시나리오2 마커
    df_s2 = df[df["is_s2"]]
    if not df_s2.empty:
        fig.add_trace(
            go.Scatter(
                x=df_s2["dt"], y=df_s2["high"] * 1.06,
                mode="markers", name="⬟ 상승가능성",
                marker=dict(symbol="hexagram", size=16, color=COLORS["scenario2"],
                            line=dict(width=1.5, color="#AA00FF")),
                hovertemplate="%{x|%Y-%m-%d}<br><b>⬟ 상승가능성</b><br>종가: %{customdata:,.0f}원<extra></extra>",
                customdata=df_s2["close"],
            ),
            row=1, col=1,
        )

    # 거래량
    fig.add_trace(
        go.Bar(
            x=df["dt"], y=df["volume"], name="거래량",
            marker_color=vol_colors, marker_line_width=0,
            hovertemplate="%{x|%Y-%m-%d}<br>거래량: %{y:,.0f}<extra></extra>",
        ),
        row=2, col=1,
    )

    # 레이아웃
    _ma_names = "+".join(f"MA{p}" for p in sorted(ma_configs.keys()))
    fig.update_layout(
        height=750,
        margin=dict(l=0, r=0, t=80, b=0),
        paper_bgcolor=COLORS["bg"],
        plot_bgcolor=COLORS["bg"],
        font=dict(color=COLORS["text"], size=12),
        title=dict(
            text=f"  {name} — {_ma_names} 추세 기준",
            font=dict(size=16, color="#FFFFFF"),
            x=0, xanchor="left", y=0.98, yanchor="top",
        ),
        legend=dict(
            orientation="h", yanchor="top", y=1.0,
            xanchor="left", x=0,
            bgcolor="rgba(0,0,0,0)",
            font=dict(size=11, color=COLORS["text"]),
        ),
        modebar=dict(orientation="h", bgcolor="rgba(0,0,0,0)"),
        hovermode="x unified",
        xaxis_rangeslider_visible=False,
    )

    fig.update_yaxes(row=1, col=1, title="", tickformat=",",
                     gridcolor=COLORS["grid"], zeroline=False, side="right")
    fig.update_yaxes(row=2, col=1, title="", tickformat=".2s",
                     gridcolor=COLORS["grid"], zeroline=False, side="right")

    # 비거래일 rangebreaks
    all_trading = set(df["dt"].dt.normalize())
    cal = pd.date_range(df["dt"].min().normalize(), df["dt"].max().normalize(), freq="D")
    non_trading = [d for d in cal if d not in all_trading]
    for rn in (1, 2):
        fig.update_xaxes(
            row=rn, col=1, gridcolor=COLORS["grid"], zeroline=False, showgrid=False,
            rangebreaks=[dict(values=[d.strftime("%Y-%m-%d") for d in non_trading])],
        )
    fig.update_xaxes(row=2, col=1, tickformat="%y/%m/%d")

    return fig


# ═══════════════════════════════════════════════════════
#  Streamlit UI
# ═══════════════════════════════════════════════════════

_CUSTOM_CSS = """
<style>
[data-testid="stMetric"] {
    background: linear-gradient(135deg, #1a1f2e 0%, #151926 100%);
    border: 1px solid #2a2f42;
    border-radius: 10px;
    padding: 14px 18px;
}
[data-testid="stMetric"] label {
    color: #8b8fa3 !important; font-size: 0.78rem !important;
}
[data-testid="stMetric"] [data-testid="stMetricValue"] {
    color: #e8eaed !important; font-size: 1.15rem !important; font-weight: 600 !important;
}
section[data-testid="stSidebar"] { background: #0f1117; }
[data-testid="stDataFrame"] { border: 1px solid #2a2f42; border-radius: 8px; }
</style>
"""

SCENARIO_NAMES = {
    1: "관심가격 돌파 추적",
    2: "상승가능성 복합 판별",
}


def _build_signal_table(result: dict, scenario: int, name: str, ticker: str) -> pd.DataFrame:
    """분석 결과를 하나의 DataFrame으로 정리"""
    rows = []

    for r in result["breakout"]:
        rows.append({
            "종목": f"{name}({ticker})", "시그널": "▲ 돌파", "날짜": r["dt"],
            "종가": r["close"], "거래량": r["volume"],
            "비고": f"전일대비 {r.get('ratio', 0):.1f}x",
        })
    for r in result["followup"]:
        rows.append({
            "종목": f"{name}({ticker})", "시그널": "● 후속 급증", "날짜": r["dt"],
            "종가": r["close"], "거래량": r["volume"],
            "비고": f"전일대비 {r.get('ratio', 0):.1f}x",
        })
    for r in result["newhigh"]:
        rows.append({
            "종목": f"{name}({ticker})", "시그널": "★ 신고가", "날짜": r["dt"],
            "종가": r["close"], "거래량": r["volume"],
            "비고": f"이전최고 {r.get('prev_high', 0):,}",
        })

    if scenario == 1:
        for r in result["scenario1"]:
            rows.append({
                "종목": f"{name}({ticker})", "시그널": "◆ 관심가 돌파", "날짜": r["dt"],
                "종가": r["close"], "거래량": r["volume"],
                "비고": f"관심가 {r['ref_price']:,} (기준일 {r['ref_date']})",
            })
    elif scenario == 2:
        for r in result["scenario2"]:
            rows.append({
                "종목": f"{name}({ticker})", "시그널": "⬟ 상승가능성", "날짜": r["dt"],
                "종가": r["close"], "거래량": r["volume"],
                "비고": f"6개월내 돌파{r['bo_count_6m']}건·후속{r['fu_count_6m']}건",
            })

    return pd.DataFrame(rows)


def _render_single_result(
    name: str, ticker: str, result: dict, scenario: int,
    period_days: int, ma_configs: dict[int, int],
):
    """단일 종목 결과 렌더링 (차트 + 테이블)"""
    days_data = result.get("_days")

    # 메트릭
    bo_n = len(result["breakout"])
    fu_n = len(result["followup"])
    nh_n = len(result["newhigh"])

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("▲ 돌파", f"{bo_n}건")
    c2.metric("● 후속", f"{fu_n}건")
    c3.metric("★ 신고가", f"{nh_n}건")
    if scenario == 1:
        c4.metric("◆ 관심가 돌파", f"{len(result['scenario1'])}건")
    elif scenario == 2:
        c4.metric("⬟ 상승가능성", f"{len(result['scenario2'])}건")

    # 차트
    fig = build_chart(
        enriched=result["enriched"],
        breakout_rows=result["breakout"],
        followup_rows=result["followup"],
        newhigh_rows=result["newhigh"],
        name=name,
        period_days=period_days,
        ma_configs=ma_configs,
        scenario1_rows=result.get("scenario1", []),
        scenario2_rows=result.get("scenario2", []),
    )
    st.plotly_chart(fig, use_container_width=True, key=f"chart_{ticker}", config={
        "displayModeBar": True,
        "modeBarButtonsToRemove": ["lasso2d", "select2d", "autoScale2d", "toggleSpikelines"],
        "displaylogo": False, "scrollZoom": True,
    })

    # 시그널 테이블
    df_sig = _build_signal_table(result, scenario, name, ticker)
    if not df_sig.empty:
        df_sig = df_sig.sort_values("날짜")
        df_show = df_sig.copy()
        df_show["종가"] = df_show["종가"].apply(lambda x: f"{int(x):,}")
        df_show["거래량"] = df_show["거래량"].apply(lambda x: f"{int(x):,}")
        st.dataframe(df_show.drop(columns=["종목"]), use_container_width=True, hide_index=True, key=f"table_{ticker}")


def _render_multi_results(
    all_results: list[dict],
    scenario: int,
    period_days: int,
    ma_configs: dict[int, int],
):
    """다종목 파일 업로드 결과: 종합 테이블 + 개별 차트 expander"""
    # 종합 테이블 생성
    all_dfs = []
    for item in all_results:
        if item.get("error"):
            continue
        df_sig = _build_signal_table(item["result"], scenario, item["name"], item["ticker"])
        if not df_sig.empty:
            all_dfs.append(df_sig)

    if not all_dfs:
        st.info("조건을 만족한 시그널이 없습니다.")
        return

    combined = pd.concat(all_dfs, ignore_index=True).sort_values(["종목", "날짜"])

    # 요약 카드
    st.markdown("#### 📊 종합 결과")
    s_counts = combined["시그널"].value_counts()
    cols = st.columns(min(len(s_counts), 6))
    for idx, (sig_name, cnt) in enumerate(s_counts.items()):
        cols[idx % len(cols)].metric(sig_name, f"{cnt}건")

    # 종합 테이블 (포매팅)
    st.markdown("#### 📋 전체 시그널 상세")
    df_show = combined.copy()
    df_show["종가"] = df_show["종가"].apply(lambda x: f"{int(x):,}")
    df_show["거래량"] = df_show["거래량"].apply(lambda x: f"{int(x):,}")
    st.dataframe(df_show, use_container_width=True, hide_index=True)

    # CSV 다운로드
    csv_bytes = combined.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
    st.download_button("📥 전체 CSV 다운로드", csv_bytes,
                       file_name="signals_all.csv", mime="text/csv")

    # 종목별 차트 (expander)
    st.markdown("#### 📈 종목별 차트")
    for item in all_results:
        if item.get("error"):
            with st.expander(f"❌ {item['query']} — 오류"):
                st.error(item["error"])
            continue

        name, ticker = item["name"], item["ticker"]
        result = item["result"]
        sig_total = (
            len(result["breakout"]) + len(result["followup"])
            + len(result["newhigh"])
            + len(result.get("scenario1", []))
            + len(result.get("scenario2", []))
        )

        with st.expander(f"📈 {name}({ticker}) — 시그널 {sig_total}건"):
            _render_single_result(name, ticker, result, scenario, period_days, ma_configs)


# ═══════════════════════════════════════════════════════
#  메인
# ═══════════════════════════════════════════════════════

def main():
    st.set_page_config(
        page_title="시나리오 시그널 분석기",
        page_icon="📊",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    st.markdown(_CUSTOM_CSS, unsafe_allow_html=True)

    # ── 사이드바 ──
    with st.sidebar:
        st.markdown("## ⚙️ 분석 설정")

        # 시나리오 선택
        scenario_choice = st.radio(
            "시나리오 선택",
            options=[1, 2],
            format_func=lambda x: f"시나리오 {x}: {SCENARIO_NAMES[x]}",
            horizontal=False,
        )

        st.markdown("---")

        # 입력 방식
        input_mode = st.radio(
            "종목 입력 방식",
            options=["직접 입력", "파일 업로드"],
            horizontal=True,
        )

        if input_mode == "직접 입력":
            ticker_input = st.text_input("종목명 / 종목코드", placeholder="예: 삼성전자, 005930")
            uploaded_file = None
        else:
            ticker_input = None
            uploaded_file = st.file_uploader(
                "종목 목록 파일 (.txt, .md)",
                type=["txt", "md"],
                help="쉼표(,)로 구분된 종목명 (예: 삼성전자, SK하이닉스, 현대자동차)",
            )

        st.markdown("---")
        st.markdown("##### 분석 기간")
        analysis_period = st.selectbox("분석 / 차트 기간", options=PERIOD_OPTIONS, index=1)

        st.markdown("##### 추세 판단 이동평균선")
        st.caption("1개 이상 선택 (복수 시 AND)")
        ma_configs: dict[int, int] = {}
        col_a, col_b, col_c = st.columns(3)
        use_ma60 = col_a.checkbox("MA60", value=True)
        use_ma120 = col_b.checkbox("MA120", value=False)
        use_ma180 = col_c.checkbox("MA180", value=False)
        if use_ma60:
            ma_configs[60] = st.slider("MA60 연속 상승 일수", 2, 30, 5, key="tw60")
        if use_ma120:
            ma_configs[120] = st.slider("MA120 연속 상승 일수", 2, 30, 5, key="tw120")
        if use_ma180:
            ma_configs[180] = st.slider("MA180 연속 상승 일수", 2, 30, 5, key="tw180")

        st.markdown("---")
        st.markdown("##### ★ 신고가 시그널")
        nh_label = st.selectbox("신고가 비교 기간", options=NH_PERIOD_OPTIONS, index=2)
        nh_lookback = NH_PERIOD_MAP.get(nh_label, 365)

        st.markdown("---")
        volume_multiplier = st.slider("거래량 폭증 배수", 2.0, 20.0, 6.0, 0.5)
        max_pages = st.number_input("API 페이지 수", 5, 100, 40, 5)

        run_btn = st.button("🔍  분석 실행", use_container_width=True, type="primary")

        # 사이드바 범례
        st.markdown("---")
        _ma_label = "+".join(f"MA{p}" for p in sorted(ma_configs.keys())) if ma_configs else "미선택"
        st.markdown(
            f"""
            <div style="font-size:0.75rem; color:#888; line-height:1.7">
            <b style="color:#E53935">▲ 돌파</b> MA20 상향돌파 + 거래량 + {_ma_label}↑ + 양봉<br>
            <b style="color:#FF9800">● 후속</b> MA20 위 + 거래량 + {_ma_label}↑ + 양봉<br>
            <b style="color:#FFD600">★ 신고가</b> N기간 최고종가 돌파 + {_ma_label}↑ + 양봉<br>
            <b style="color:#00E676">◆ 관심가 돌파</b> 트리거 후 관심가 이상 + MA20위 + 거래량 + 양봉 (1회)<br>
            <b style="color:#E040FB">⬟ 상승가능성</b> 신고가+후속 동시 + 6개월내 돌파·후속 각≥2건
            </div>
            """,
            unsafe_allow_html=True,
        )

    # ── 메인 영역 ──
    st.markdown(
        "<h2 style='margin-bottom:0'>📊 시나리오 시그널 분석기</h2>"
        f"<p style='color:#888; margin-top:4px'>시나리오 {scenario_choice}: "
        f"{SCENARIO_NAMES[scenario_choice]}</p>",
        unsafe_allow_html=True,
    )

    if not run_btn:
        st.info("👈 사이드바에서 설정 후 **분석 실행**을 눌러주세요.")

        with st.expander("💡 사용 가이드", expanded=True):
            st.markdown(
                """
                **사용 방법**  
                1. 시나리오 선택 (관심가격 추적 / 상승가능성 판별)  
                2. 종목 직접 입력 또는 txt/md 파일 업로드  
                3. 파라미터 조정 후 **분석 실행**  

                **시나리오 1 — 관심가격 돌파 추적**  
                트리거: ▲MA20 돌파 또는 ★신고가+●후속 동시 발생 →
                관심기준가격 = 시가+(종가-시가)/4 산출.  
                이후 시가·종가 모두 관심가 이상 + MA20 위 + 양봉 + 거래량 시 ◆ **첫 1회만** 표시.  
                새 트리거 발생 → 관심기준가격 갱신 + 다시 1회 표시 가능.  

                **시나리오 2 — 상승가능성 복합 판별**  
                ★신고가 + ●후속급증 동시 발생일 →
                해당일 기준 6개월 내 ▲돌파 ≥ 2건 AND ●후속 ≥ 2건 → ⬟ 표시  

                **파일 업로드**  
                쉼표(,) 구분 종목명 파일 (예: `삼성전자, SK하이닉스, 현대자동차`)  
                """
            )

        with st.expander("🖱️ 차트 도구 모음 (우측 상단)"):
            st.markdown(
                """
                | 아이콘 | 이름 | 기능 |
                |:---:|---|---|
                | 📷 | 이미지 저장 | 차트를 PNG로 다운로드 |
                | 🔍+ | 확대 | 드래그로 영역 확대 |
                | 🔍- | 축소 | 한 단계 축소 |
                | ✋ | 이동 | 드래그로 좌우/상하 이동 |
                | 🏠 | 초기화 | 원래 전체 보기 복원 |

                **마우스 팁**: 스크롤=확대축소 · 더블클릭=초기화 · 범례클릭=표시토글
                """
            )
        return

    # 유효성 검사
    if not ma_configs:
        st.error("추세 판단 이동평균선을 최소 1개 이상 선택해 주세요.")
        return

    if not core.APP_KEY or not core.APP_SECRET:
        st.error("`.env`에 APP_KEY / APP_SECRET 설정이 필요합니다.")
        return

    period_days = PERIOD_MAP.get(analysis_period, 365)

    # ── 단일 종목 모드 ──
    if input_mode == "직접 입력":
        if not ticker_input or not ticker_input.strip():
            st.error("종목명 또는 종목코드를 입력해 주세요.")
            return

        try:
            with st.spinner("종목 코드 확인 중..."):
                ticker, name = _resolve_ticker_and_name(ticker_input.strip())

            with st.spinner(f"{name}({ticker}) 일봉 조회 중..."):
                token = core.get_token(core.APP_KEY, core.APP_SECRET)
                days = _fetch_daily_ohlcv(token=token, ticker=ticker, max_pages=max_pages)

            with st.spinner("분석 중..."):
                result = run_full_analysis(
                    days=days, ma_configs=ma_configs,
                    volume_multiplier=volume_multiplier,
                    period_days=period_days,
                    newhigh_lookback_days=nh_lookback,
                    scenario=scenario_choice,
                )

            st.markdown(f"### {name} ({ticker})")
            _render_single_result(name, ticker, result, scenario_choice, period_days, ma_configs)

        except Exception as e:
            st.error(f"오류: {e}")
            st.exception(e)

    # ── 파일 업로드 모드 ──
    else:
        if not uploaded_file:
            st.warning("종목 목록 파일을 업로드해 주세요.")
            return

        content = uploaded_file.read().decode("utf-8")
        ticker_queries = _parse_ticker_file(content)
        if not ticker_queries:
            st.error("파일에서 종목명을 찾을 수 없습니다.")
            return

        st.info(f"📂 파일에서 **{len(ticker_queries)}개** 종목 감지: {', '.join(ticker_queries)}")

        token = core.get_token(core.APP_KEY, core.APP_SECRET)
        all_results: list[dict] = []
        progress = st.progress(0, text="분석 시작...")

        for idx, query in enumerate(ticker_queries):
            progress.progress(
                (idx + 1) / len(ticker_queries),
                text=f"({idx+1}/{len(ticker_queries)}) {query} 분석 중...",
            )
            try:
                ticker, name = _resolve_ticker_and_name(query)
                days = _fetch_daily_ohlcv(token=token, ticker=ticker, max_pages=max_pages)
                result = run_full_analysis(
                    days=days, ma_configs=ma_configs,
                    volume_multiplier=volume_multiplier,
                    period_days=period_days,
                    newhigh_lookback_days=nh_lookback,
                    scenario=scenario_choice,
                )
                result["_days"] = days
                all_results.append({
                    "query": query, "ticker": ticker, "name": name,
                    "result": result, "error": None,
                })
            except Exception as e:
                all_results.append({
                    "query": query, "ticker": "", "name": query,
                    "result": None, "error": str(e),
                })

        progress.empty()

        # 에러 종목 표시
        errors = [r for r in all_results if r.get("error")]
        if errors:
            with st.expander(f"⚠️ 오류 발생 종목 ({len(errors)}건)", expanded=False):
                for e in errors:
                    st.warning(f"{e['query']}: {e['error']}")

        _render_multi_results(all_results, scenario_choice, period_days, ma_configs)


if __name__ == "__main__":
    main()