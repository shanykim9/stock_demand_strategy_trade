"""
돌파 시그널 탐색기 — 캔들스틱 + 거래량 트레이딩 차트 UI
═══════════════════════════════════════════════════════════
- 키움 REST API (ka10081) 일봉 OHLCV 기반
- 양봉(적색) / 음봉(청색) 캔들스틱
- 거래량 바: 양봉일=적색, 음봉일=청색, 시그널일=강조
- MA20 / MA60·120·180 이동평균선 오버레이 (선택 가능)
- ▲ 돌파 시그널 / ● 후속 급증 시그널 마커

실행: streamlit run breakout_ui.py
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
#  색상 팔레트 (트레이딩 스타일)
# ═══════════════════════════════════════════════════════
COLORS = {
    "bull":          "#E53935",   # 양봉 (적색)
    "bear":          "#1E88E5",   # 음봉 (청색)
    "bull_vol":      "#EF9A9A",   # 양봉일 거래량 (연적색)
    "bear_vol":      "#90CAF9",   # 음봉일 거래량 (연청색)
    "breakout":      "#E53935",   # 돌파 마커
    "followup":      "#FF9800",   # 후속 마커
    "newhigh":       "#FFD600",   # 신고가 마커 (골드)
    "newhigh_vol":   "#FFD600",   # 신고가일 거래량
    "breakout_vol":  "#E53935",   # 돌파일 거래량 (강조 적색)
    "followup_vol":  "#FF9800",   # 후속일 거래량 (강조 주황)
    "ma20":          "#FFA726",   # MA20 선
    "bg":            "#131722",   # 차트 배경 (다크)
    "grid":          "#1E222D",   # 그리드
    "text":          "#D1D4DC",   # 텍스트
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
            raise RuntimeError(err or "종목명을 다시 확인해 주세요.")
    name = ticker
    try:
        name = (core._krx_cache.get("name_by_code") or {}).get(ticker, ticker)
    except Exception:
        pass
    return ticker, name


# ═══════════════════════════════════════════════════════
#  데이터 조회 — OHLCV 전체 파싱
# ═══════════════════════════════════════════════════════

def _fetch_daily_ohlcv(token: str, ticker: str, max_pages: int = 40) -> list[dict]:
    """
    키움 ka10081 일봉에서 시가/고가/저가/종가/거래량을 모두 가져옵니다.
    demand.py fetch_ohlcv_ohlc_map과 동일한 필드명 후보 사용.
    """
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
                token=token,
                api_id="ka10081",
                body=body,
                endpoint="/api/dostk/chart",
                max_pages=max_pages,
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
                # OHLC 중 누락 시 종가로 보정
                if open_p <= 0:
                    open_p = close_p
                if high_p <= 0:
                    high_p = max(open_p, close_p)
                if low_p <= 0:
                    low_p = min(open_p, close_p)

                dedup[dt] = {
                    "dt": dt,
                    "open": open_p,
                    "high": high_p,
                    "low": low_p,
                    "close": close_p,
                    "volume": max(0, vol),
                }

            out = sorted(dedup.values(), key=lambda x: x["dt"])
            if out:
                return out
        except Exception as e:
            last_err = e
            continue

    raise RuntimeError(f"일봉 데이터 조회 실패: {last_err}")


# ═══════════════════════════════════════════════════════
#  기술지표 / 분석
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
    """선택된 이동평균선의 상승추세 판정 (선형회귀 기울기 > 0)"""
    if trend_window < 2 or i - trend_window + 1 < 0:
        return False
    window = ma_values[i - trend_window + 1 : i + 1]
    if not all(v is not None for v in window):
        return False
    return _linear_regression_slope([float(v) for v in window]) > 0


def _analyze_breakout(
    days: list[dict],
    ma_configs: dict[int, int] | None = None,
    volume_multiplier: float = 6.0,
    period_days: int = 365,
    newhigh_lookback_days: int = 365,
) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """
    Returns: (breakout_rows, followup_rows, newhigh_rows, enriched_days)

    ma_configs: {MA기간: 연속상승일수} → AND 조건
    newhigh_lookback_days: 신고가 비교 기간 (일). 0이면 신고가 비활성.
    """
    if not ma_configs:
        ma_configs = {60: 5}

    closes = [int(x["close"]) for x in days]
    vols = [int(x["volume"]) for x in days]
    dates = [x["dt"] for x in days]
    ma20 = _rolling_ma(closes, 20)

    # 선택된 MA별 이동평균 계산
    ma_lines: dict[int, list[Optional[float]]] = {}
    for period in ma_configs:
        ma_lines[period] = _rolling_ma(closes, period)

    if period_days > 0:
        cutoff = (datetime.now(core.TZ).date() - timedelta(days=period_days)).strftime("%Y-%m-%d")
    else:
        cutoff = "0000-00-00"

    breakout_rows: list[dict] = []
    followup_rows: list[dict] = []
    newhigh_rows: list[dict] = []
    enriched: list[dict] = []
    has_breakout_occurred = False

    for i in range(len(days)):
        d = days[i]
        row: dict = {
            "dt": d["dt"],
            "open": d["open"],
            "high": d["high"],
            "low": d["low"],
            "close": d["close"],
            "volume": d["volume"],
            "ma20": ma20[i],
            "signal": "",
        }
        for period, ma_vals in ma_lines.items():
            row[f"ma{period}"] = ma_vals[i]

        if i >= 1 and d["dt"] >= cutoff:
            prev_vol = vols[i - 1]
            curr_vol = vols[i]
            cond_volume = prev_vol > 0 and curr_vol >= prev_vol * volume_multiplier
            cond_bull = d["close"] >= d["open"]

            cond_all_ma_up = all(
                _check_ma_uptrend(ma_lines[period], i, tw)
                for period, tw in ma_configs.items()
            )

            prev_ma20 = ma20[i - 1]
            curr_ma20 = ma20[i]
            cond_ma20_cross = (
                prev_ma20 is not None
                and curr_ma20 is not None
                and closes[i - 1] <= float(prev_ma20)
                and closes[i] > float(curr_ma20)
            )

            # ── ▲ 돌파 ──
            if cond_bull and cond_volume and cond_all_ma_up and cond_ma20_cross:
                row["signal"] = "breakout"
                has_breakout_occurred = True
                breakout_rows.append({
                    "dt": d["dt"], "close": closes[i],
                    "volume": curr_vol, "ratio": curr_vol / float(prev_vol),
                })
            # ── ● 후속 급증 ──
            elif has_breakout_occurred and cond_bull and cond_volume and cond_all_ma_up:
                if curr_ma20 is not None and closes[i] > float(curr_ma20):
                    row["signal"] = "followup"
                    followup_rows.append({
                        "dt": d["dt"], "close": closes[i],
                        "volume": curr_vol, "ratio": curr_vol / float(prev_vol),
                    })

            # ── ★ 신고가 (독립 판정, 중복 허용) ──
            if newhigh_lookback_days > 0 and cond_bull and cond_all_ma_up:
                lookback_start = (
                    datetime.strptime(d["dt"], "%Y-%m-%d")
                    - timedelta(days=newhigh_lookback_days)
                ).strftime("%Y-%m-%d")
                # 과거 lookback 기간 내 종가 최대값 (당일 제외)
                past_max = 0
                for j in range(i):
                    if dates[j] >= lookback_start:
                        if closes[j] > past_max:
                            past_max = closes[j]
                if past_max > 0 and closes[i] > past_max:
                    newhigh_rows.append({
                        "dt": d["dt"], "close": closes[i],
                        "volume": curr_vol,
                        "prev_high": past_max,
                    })

        enriched.append(row)

    return breakout_rows, followup_rows, newhigh_rows, enriched


# ═══════════════════════════════════════════════════════
#  캔들스틱 + 거래량 차트 (Plotly, 트레이딩 다크 테마)
# ═══════════════════════════════════════════════════════

_MA_COLORS = {
    60:  "#AB47BC",   # 보라
    120: "#26A69A",   # 청록
    180: "#42A5F5",   # 하늘
}

_MA_DASH = {
    60:  "dash",
    120: "dashdot",
    180: "longdash",
}


def build_chart(
    enriched: list[dict],
    breakout_rows: list[dict],
    followup_rows: list[dict],
    newhigh_rows: list[dict],
    name: str,
    period_days: int = 365,
    ma_configs: dict[int, int] | None = None,
) -> go.Figure:
    if not ma_configs:
        ma_configs = {60: 5}

    df = pd.DataFrame(enriched)
    df["dt"] = pd.to_datetime(df["dt"])

    # 선택 기간만 표시 (0=전체)
    if period_days > 0:
        cutoff = datetime.now() - timedelta(days=period_days)
        df = df[df["dt"] >= cutoff].copy().reset_index(drop=True)
    else:
        df = df.copy().reset_index(drop=True)
    if df.empty:
        fig = go.Figure()
        fig.add_annotation(text="표시할 데이터가 없습니다.", showarrow=False)
        return fig

    # 양봉/음봉
    df["is_bull"] = df["close"] >= df["open"]

    breakout_dates = set(r["dt"] for r in breakout_rows)
    followup_dates = set(r["dt"] for r in followup_rows)
    newhigh_dates = set(r["dt"] for r in newhigh_rows)
    dt_str = df["dt"].dt.strftime("%Y-%m-%d")
    df["is_breakout"] = dt_str.isin(breakout_dates)
    df["is_followup"] = dt_str.isin(followup_dates)
    df["is_newhigh"] = dt_str.isin(newhigh_dates)

    # 거래량 바 색상 (우선순위: 신고가 > 돌파 > 후속 > 양봉/음봉)
    vol_colors = []
    for _, row in df.iterrows():
        if row["is_newhigh"]:
            vol_colors.append(COLORS["newhigh_vol"])
        elif row["is_breakout"]:
            vol_colors.append(COLORS["breakout_vol"])
        elif row["is_followup"]:
            vol_colors.append(COLORS["followup_vol"])
        elif row["is_bull"]:
            vol_colors.append(COLORS["bull_vol"])
        else:
            vol_colors.append(COLORS["bear_vol"])

    # 서브플롯: 가격(75%) + 거래량(25%)
    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.02,
        row_heights=[0.75, 0.25],
    )

    # ─── 캔들스틱 ───
    fig.add_trace(
        go.Candlestick(
            x=df["dt"],
            open=df["open"],
            high=df["high"],
            low=df["low"],
            close=df["close"],
            increasing=dict(line=dict(color=COLORS["bull"]), fillcolor=COLORS["bull"]),
            decreasing=dict(line=dict(color=COLORS["bear"]), fillcolor=COLORS["bear"]),
            name="일봉",
            hoverinfo="x+y",
        ),
        row=1, col=1,
    )

    # ─── MA20 ───
    fig.add_trace(
        go.Scatter(
            x=df["dt"], y=df["ma20"],
            name="MA20",
            line=dict(color=COLORS["ma20"], width=1.2, dash="dot"),
            hoverinfo="skip",
        ),
        row=1, col=1,
    )

    # ─── 선택된 MA 전부 표시 ───
    for period in sorted(ma_configs.keys()):
        col_name = f"ma{period}"
        if col_name not in df.columns:
            continue
        fig.add_trace(
            go.Scatter(
                x=df["dt"], y=df[col_name],
                name=f"MA{period}",
                line=dict(
                    color=_MA_COLORS.get(period, "#AB47BC"),
                    width=1.2,
                    dash=_MA_DASH.get(period, "dash"),
                ),
                hoverinfo="skip",
            ),
            row=1, col=1,
        )

    # ─── 돌파 마커 ▲ ───
    df_bo = df[df["is_breakout"]]
    if not df_bo.empty:
        fig.add_trace(
            go.Scatter(
                x=df_bo["dt"], y=df_bo["high"] * 1.02,
                mode="markers",
                name="▲ 돌파",
                marker=dict(
                    symbol="triangle-up", size=14,
                    color=COLORS["breakout"],
                    line=dict(width=1, color="#B71C1C"),
                ),
                hovertemplate=(
                    "%{x|%Y-%m-%d}<br><b>▲ MA20 돌파</b>"
                    "<br>종가: %{customdata:,.0f}원<extra></extra>"
                ),
                customdata=df_bo["close"],
            ),
            row=1, col=1,
        )

    # ─── 후속 마커 ● ───
    df_fu = df[df["is_followup"]]
    if not df_fu.empty:
        fig.add_trace(
            go.Scatter(
                x=df_fu["dt"], y=df_fu["high"] * 1.02,
                mode="markers",
                name="● 후속 급증",
                marker=dict(
                    symbol="circle", size=11,
                    color=COLORS["followup"],
                    line=dict(width=1, color="#E65100"),
                ),
                hovertemplate=(
                    "%{x|%Y-%m-%d}<br><b>● MA20 위 급증</b>"
                    "<br>종가: %{customdata:,.0f}원<extra></extra>"
                ),
                customdata=df_fu["close"],
            ),
            row=1, col=1,
        )

    # ─── 신고가 마커 ★ ───
    df_nh = df[df["is_newhigh"]]
    if not df_nh.empty:
        fig.add_trace(
            go.Scatter(
                x=df_nh["dt"], y=df_nh["high"] * 1.04,
                mode="markers",
                name="★ 신고가",
                marker=dict(
                    symbol="star", size=14,
                    color=COLORS["newhigh"],
                    line=dict(width=1, color="#F9A825"),
                ),
                hovertemplate=(
                    "%{x|%Y-%m-%d}<br><b>★ 신고가 돌파</b>"
                    "<br>종가: %{customdata:,.0f}원<extra></extra>"
                ),
                customdata=df_nh["close"],
            ),
            row=1, col=1,
        )

    # ─── 거래량 바 ───
    fig.add_trace(
        go.Bar(
            x=df["dt"], y=df["volume"],
            name="거래량",
            marker_color=vol_colors,
            marker_line_width=0,
            hovertemplate="%{x|%Y-%m-%d}<br>거래량: %{y:,.0f}<extra></extra>",
        ),
        row=2, col=1,
    )

    # ─── 레이아웃 (TradingView 다크 스타일) ───
    _ma_names = "+".join(f"MA{p}" for p in sorted(ma_configs.keys()))
    fig.update_layout(
        height=750,
        margin=dict(l=0, r=0, t=80, b=0),
        paper_bgcolor=COLORS["bg"],
        plot_bgcolor=COLORS["bg"],
        font=dict(color=COLORS["text"], size=12),
        title=dict(
            text=f"  {name}  일봉 · 돌파(▲) · 후속(●) — {_ma_names} 추세 기준",
            font=dict(size=16, color="#FFFFFF"),
            x=0, xanchor="left",
            y=0.98, yanchor="top",
        ),
        legend=dict(
            orientation="h",
            yanchor="top", y=1.0,
            xanchor="left", x=0,
            bgcolor="rgba(0,0,0,0)",
            font=dict(size=11, color=COLORS["text"]),
        ),
        modebar=dict(
            orientation="h",
            bgcolor="rgba(0,0,0,0)",
        ),
        hovermode="x unified",
        xaxis_rangeslider_visible=False,
    )

    # 가격 y축 (우측)
    fig.update_yaxes(
        row=1, col=1,
        title="",
        tickformat=",",
        gridcolor=COLORS["grid"],
        zeroline=False,
        side="right",
    )
    # 거래량 y축 (우측)
    fig.update_yaxes(
        row=2, col=1,
        title="",
        tickformat=".2s",
        gridcolor=COLORS["grid"],
        zeroline=False,
        side="right",
    )
    # ── 비거래일(주말+공휴일) 계산 → rangebreaks로 숨김 ──
    all_trading_dates = set(df["dt"].dt.normalize())
    date_min = df["dt"].min().normalize()
    date_max = df["dt"].max().normalize()
    all_calendar_dates = pd.date_range(date_min, date_max, freq="D")
    non_trading_dates = [d for d in all_calendar_dates if d not in all_trading_dates]

    # x축: rangebreaks 적용
    for row_n in (1, 2):
        fig.update_xaxes(
            row=row_n, col=1,
            gridcolor=COLORS["grid"],
            zeroline=False,
            showgrid=False,
            rangebreaks=[dict(values=[d.strftime("%Y-%m-%d") for d in non_trading_dates])],
        )
    fig.update_xaxes(row=2, col=1, tickformat="%y/%m/%d")

    return fig


# ═══════════════════════════════════════════════════════
#  Streamlit 메인 UI
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
    color: #8b8fa3 !important;
    font-size: 0.78rem !important;
}
[data-testid="stMetric"] [data-testid="stMetricValue"] {
    color: #e8eaed !important;
    font-size: 1.15rem !important;
    font-weight: 600 !important;
}
section[data-testid="stSidebar"] {
    background: #0f1117;
}
[data-testid="stDataFrame"] {
    border: 1px solid #2a2f42;
    border-radius: 8px;
}
</style>
"""


def main():
    st.set_page_config(
        page_title="돌파 시그널 탐색기",
        page_icon="📈",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    st.markdown(_CUSTOM_CSS, unsafe_allow_html=True)

    # ── 사이드바 ──
    with st.sidebar:
        st.markdown("## ⚙️ 분석 설정")

        ticker_input = st.text_input(
            "종목명 / 종목코드",
            placeholder="예: 삼성전자, 005930",
        )

        st.markdown("---")
        st.markdown("##### 파라미터")

        analysis_period = st.selectbox(
            "분석 / 차트 기간",
            options=["6개월", "1년", "1년 6개월", "2년", "2년 6개월", "3년", "4년", "5년", "전체"],
            index=1,
            help="시그널 탐색 및 차트 표시 기간",
        )

        st.markdown("##### 추세 판단 이동평균선")
        st.caption("1개 이상 선택 (복수 선택 시 AND 조건)")

        ma_configs: dict[int, int] = {}

        col_a, col_b, col_c = st.columns(3)
        use_ma60 = col_a.checkbox("MA60", value=True)
        use_ma120 = col_b.checkbox("MA120", value=False)
        use_ma180 = col_c.checkbox("MA180", value=False)

        if use_ma60:
            tw60 = st.slider("MA60 연속 상승 일수", 2, 30, 5, key="tw60")
            ma_configs[60] = tw60
        if use_ma120:
            tw120 = st.slider("MA120 연속 상승 일수", 2, 30, 5, key="tw120")
            ma_configs[120] = tw120
        if use_ma180:
            tw180 = st.slider("MA180 연속 상승 일수", 2, 30, 5, key="tw180")
            ma_configs[180] = tw180

        st.markdown("---")
        st.markdown("##### ★ 신고가 시그널")
        _NH_PERIOD_OPTIONS = ["비활성", "6개월", "1년", "1년 6개월", "2년", "2년 6개월", "3년", "4년", "5년"]
        newhigh_period_label = st.selectbox(
            "신고가 비교 기간",
            options=_NH_PERIOD_OPTIONS,
            index=2,
            help="종가가 과거 N기간 최고 종가를 돌파하면 ★ 표시 (MA상승+양봉 필수)",
        )
        _NH_PERIOD_MAP = {
            "비활성": 0, "6개월": 180, "1년": 365, "1년 6개월": 548, "2년": 730,
            "2년 6개월": 913, "3년": 1095, "4년": 1460, "5년": 1825,
        }
        newhigh_lookback_days = _NH_PERIOD_MAP.get(newhigh_period_label, 365)

        st.markdown("---")
        volume_multiplier = st.slider(
            "거래량 폭증 배수", 2.0, 20.0, 6.0, 0.5,
            help="전일 대비 배수 (6배 = +500%)",
        )
        max_pages = st.number_input(
            "API 페이지 수", 5, 100, 40, 5,
            help="키움 API 페이징 (클수록 과거 데이터↑)",
        )

        run_btn = st.button("🔍  분석 실행", use_container_width=True, type="primary")

        st.markdown("---")
        _active_ma = "+".join(f"MA{p}" for p in sorted(ma_configs.keys())) if ma_configs else "미선택"
        _nh_label = newhigh_period_label if newhigh_lookback_days > 0 else "비활성"
        st.markdown(
            f"""
            <div style="font-size:0.78rem; color:#888; line-height:1.6">
            <b style="color:#E53935">▲ 돌파</b> &nbsp;MA20 상향돌파 + 거래량폭증 + {_active_ma}↑ + 양봉<br>
            <b style="color:#FF9800">● 후속</b> &nbsp;돌파 후 MA20 위 + 거래량폭증 + {_active_ma}↑ + 양봉<br>
            <b style="color:#FFD600">★ 신고가</b> &nbsp;과거 {_nh_label} 최고종가 돌파 + {_active_ma}↑ + 양봉<br><br>
            <span style="color:#E53935">━</span> 양봉(적색)&ensp;
            <span style="color:#1E88E5">━</span> 음봉(청색)
            </div>
            """,
            unsafe_allow_html=True,
        )

    # ── 메인 ──
    st.markdown(
        "<h2 style='margin-bottom:0'>📈 돌파 시그널 탐색기</h2>"
        "<p style='color:#888; margin-top:4px'>"
        "MA20 돌파(▲) + 후속 거래량 급증(●) — 일봉 캔들스틱 차트</p>",
        unsafe_allow_html=True,
    )

    if not run_btn:
        st.info("👈 사이드바에서 종목을 입력하고 **분석 실행**을 눌러주세요.")
        with st.expander("💡 사용 가이드", expanded=True):
            st.markdown(
                """
                **사용 방법**  
                1. 종목명(삼성전자) 또는 6자리 코드(005930) 입력  
                2. 파라미터 조정 후 **분석 실행**  
                3. 캔들스틱 차트에서 ▲/●/★ 마커 확인  

                **시그널 해석**  
                - **▲ 돌파**: 선택한 MA 상승추세 + 양봉 + 거래량 폭증 + MA20 상향 돌파  
                - **● 후속 급증**: 돌파 후 MA20 위 유지 + 양봉 + 거래량 폭증  
                - **★ 신고가**: 종가가 과거 N기간 최고 종가 돌파 + MA 상승추세 + 양봉  

                **환경 설정**  
                `.env` 파일에 `APP_KEY`, `APP_SECRET` (키움 Open API) 필요  
                """
            )
        with st.expander("🖱️ 차트 도구 모음 (우측 상단)"):
            st.markdown(
                """
                | 아이콘 | 이름 | 기능 |
                |:---:|---|---|
                | 📷 | 이미지 저장 | 차트를 PNG 이미지로 다운로드 |
                | 🔍+ | 확대 (Zoom) | 드래그로 특정 영역 확대 |
                | 🔍- | 축소 (Zoom Out) | 한 단계 축소 |
                | ✋ | 이동 (Pan) | 드래그로 차트 좌우/상하 이동 |
                | 🏠 | 초기화 (Reset) | 원래 전체 보기로 복원 |

                **마우스 조작 팁**  
                - **스크롤**: 마우스 휠로 확대/축소  
                - **더블클릭**: 전체 보기로 즉시 복원  
                - **범례 클릭**: 해당 항목 표시/숨김 전환  
                - **범례 더블클릭**: 해당 항목만 보기  
                """
            )
        return

    if not ticker_input.strip():
        st.error("종목명 또는 종목코드를 입력해 주세요.")
        return

    if not ma_configs:
        st.error("추세 판단 이동평균선을 최소 1개 이상 선택해 주세요.")
        return

    if not core.APP_KEY or not core.APP_SECRET:
        st.error("`.env`에 APP_KEY / APP_SECRET 설정이 필요합니다.")
        return

    try:
        # 기간 선택 → 일수 변환
        _PERIOD_MAP = {
            "6개월": 180, "1년": 365, "1년 6개월": 548, "2년": 730,
            "2년 6개월": 913, "3년": 1095, "4년": 1460, "5년": 1825, "전체": 0,
        }
        period_days = _PERIOD_MAP.get(analysis_period, 365)

        with st.spinner("종목 코드 확인 중..."):
            ticker, name = _resolve_ticker_and_name(ticker_input.strip())

        with st.spinner(f"{name}({ticker}) 일봉 OHLCV 조회 중..."):
            token = core.get_token(core.APP_KEY, core.APP_SECRET)
            days = _fetch_daily_ohlcv(token=token, ticker=ticker, max_pages=max_pages)

        with st.spinner("돌파 조건 분석 중..."):
            breakout_rows, followup_rows, newhigh_rows, enriched = _analyze_breakout(
                days=days,
                ma_configs=ma_configs,
                volume_multiplier=volume_multiplier,
                period_days=period_days,
                newhigh_lookback_days=newhigh_lookback_days,
            )

        # ── 데이터 기간 + 요약 메트릭 ──
        first_dt = days[0]["dt"]
        last_dt = days[-1]["dt"]
        latest = days[-1]
        prev_close = days[-2]["close"] if len(days) >= 2 else latest["close"]
        change = latest["close"] - prev_close
        change_pct = (change / prev_close * 100) if prev_close else 0

        st.markdown(f"### {name} ({ticker})")

        c1, c2, c3, c4, c5, c6 = st.columns(6)
        c1.metric("📅 조회 기간", f"{first_dt} ~ {last_dt}")
        c2.metric("거래일 수", f"{len(days):,}일")
        c3.metric("최근 종가", f"{latest['close']:,}원",
                   delta=f"{change:+,}원 ({change_pct:+.2f}%)")
        c4.metric("▲ 돌파", f"{len(breakout_rows)}건")
        c5.metric("● 후속 급증", f"{len(followup_rows)}건")
        c6.metric("★ 신고가", f"{len(newhigh_rows)}건")

        _nh_cap = f" · ★신고가 {newhigh_period_label}" if newhigh_lookback_days > 0 else ""
        _ma_desc = " · ".join(f"MA{p} {tw}일↑" for p, tw in sorted(ma_configs.items()))
        st.caption(
            f"기간: {analysis_period} · {_ma_desc}{_nh_cap} · "
            f"거래량 ×{volume_multiplier:.1f} · 양봉=적색 / 음봉=청색"
        )

        # ── 캔들스틱 차트 ──
        fig = build_chart(enriched, breakout_rows, followup_rows, newhigh_rows, name, period_days, ma_configs)
        st.plotly_chart(fig, use_container_width=True, config={
            "displayModeBar": True,
            "modeBarButtonsToRemove": [
                "lasso2d", "select2d", "autoScale2d",
                "toggleSpikelines",
            ],
            "displaylogo": False,
            "scrollZoom": True,
        })

        # ── 시그널 테이블 ──
        st.markdown("#### 📋 시그널 상세")

        all_signals: list[dict] = []
        for r in breakout_rows:
            all_signals.append({
                "type": "▲ 돌파", "dt": r["dt"], "close": r["close"],
                "volume": r["volume"], "ratio": r.get("ratio"),
                "prev_high": None,
            })
        for r in followup_rows:
            all_signals.append({
                "type": "● 후속 급증", "dt": r["dt"], "close": r["close"],
                "volume": r["volume"], "ratio": r.get("ratio"),
                "prev_high": None,
            })
        for r in newhigh_rows:
            all_signals.append({
                "type": "★ 신고가", "dt": r["dt"], "close": r["close"],
                "volume": r["volume"], "ratio": None,
                "prev_high": r.get("prev_high"),
            })

        if all_signals:
            all_signals.sort(key=lambda x: x["dt"])
            df_result = pd.DataFrame(all_signals)
            df_result = df_result[["type", "dt", "close", "volume", "ratio", "prev_high"]]
            df_result.columns = ["시그널", "날짜", "종가", "거래량", "전일대비 배수", "이전 최고가"]

            df_display = df_result.copy()
            df_display["종가"] = df_display["종가"].apply(lambda x: f"{int(x):,}")
            df_display["거래량"] = df_display["거래량"].apply(lambda x: f"{int(x):,}")
            df_display["전일대비 배수"] = df_display["전일대비 배수"].apply(
                lambda x: f"{x:.2f}x" if pd.notna(x) else "-"
            )
            df_display["이전 최고가"] = df_display["이전 최고가"].apply(
                lambda x: f"{int(x):,}" if pd.notna(x) and x else "-"
            )

            st.dataframe(
                df_display,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "시그널": st.column_config.TextColumn(width="small"),
                    "날짜": st.column_config.TextColumn(width="medium"),
                    "종가": st.column_config.TextColumn(width="medium"),
                    "거래량": st.column_config.TextColumn(width="medium"),
                    "전일대비 배수": st.column_config.TextColumn(width="medium"),
                    "이전 최고가": st.column_config.TextColumn(width="medium"),
                },
            )

            csv_bytes = df_result.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
            st.download_button(
                "📥 CSV 다운로드", csv_bytes,
                file_name=f"{name}_{ticker}_signals.csv",
                mime="text/csv",
            )
        else:
            st.info("조건을 만족한 시그널이 없습니다. 파라미터를 조정해 보세요.")

    except RuntimeError as e:
        st.error(f"오류: {e}")
    except Exception as e:
        st.error(f"예기치 않은 오류: {e}")
        st.exception(e)


if __name__ == "__main__":
    main()