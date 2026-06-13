from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from typing import Optional

try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv()
except Exception:
    pass

import demand as core


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


def _fetch_daily_ohlcv(token: str, ticker: str, max_pages: int = 40) -> list[dict]:
    end_dt = datetime.now(core.TZ).strftime("%Y%m%d")
    stex_tp = (core.os.getenv("KIWOOM_DMST_STEX_TP") or "KRX").strip().upper()
    upd_stkpc_tp = (core.os.getenv("KIWOOM_OHLCV_UPD_STKPC_TP") or "1").strip()

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
                dt = _parse_dt_any(_first_non_empty(r, ["dt", "date", "bas_dt", "base_dt", "trde_dt", "trd_dt"]))
                if not dt:
                    continue
                close_p = _to_int(_first_non_empty(r, ["close_pric", "close", "stck_clpr", "cur_prc", "cur_pric"]), 0)
                vol = _to_int(_first_non_empty(r, ["trde_qty", "volume", "acml_vol", "acc_trde_qty"]), 0)
                if close_p <= 0:
                    continue
                dedup[dt] = {"dt": dt, "close": close_p, "volume": max(0, vol)}

            out = sorted(dedup.values(), key=lambda x: x["dt"])
            if out:
                return out
        except Exception as e:
            last_err = e
            continue

    raise RuntimeError(f"일봉 데이터 조회 실패: {last_err}")


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
    numerator = 0.0
    denominator = 0.0
    for i, yi in enumerate(y):
        dx = i - x_mean
        numerator += dx * (yi - y_mean)
        denominator += dx * dx
    if denominator == 0.0:
        return 0.0
    return numerator / denominator


def _analyze_breakout(days: list[dict], trend_window: int = 20) -> list[dict]:
    closes = [int(x["close"]) for x in days]
    vols = [int(x["volume"]) for x in days]
    ma20 = _rolling_ma(closes, 20)
    ma180 = _rolling_ma(closes, 180)

    one_year_ago = (datetime.now(core.TZ).date() - timedelta(days=365)).strftime("%Y-%m-%d")
    results: list[dict] = []

    for i in range(1, len(days)):
        dt = days[i]["dt"]
        if dt < one_year_ago:
            continue

        prev_vol = vols[i - 1]
        curr_vol = vols[i]
        if prev_vol <= 0:
            continue

        cond_volume = curr_vol >= prev_vol * 6  # 전일 대비 +500% 이상

        cond_ma180_up = False
        if trend_window >= 2 and i - trend_window + 1 >= 0:
            ma180_window = ma180[i - trend_window + 1 : i + 1]
            if all(v is not None for v in ma180_window):
                slope = _linear_regression_slope([float(v) for v in ma180_window if v is not None])
                cond_ma180_up = slope > 0

        prev_ma20 = ma20[i - 1]
        curr_ma20 = ma20[i]
        cond_ma20_cross = (
            prev_ma20 is not None
            and curr_ma20 is not None
            and closes[i - 1] <= float(prev_ma20)
            and closes[i] > float(curr_ma20)
        )

        if cond_volume and cond_ma180_up and cond_ma20_cross:
            results.append(
                {
                    "dt": dt,
                    "close": closes[i],
                    "volume": curr_vol,
                    "ratio": curr_vol / float(prev_vol),
                }
            )
    return results


def _print_result_table(name: str, ticker: str, rows: list[dict]) -> None:
    print(f"\n종목: {name} ({ticker})")
    print("조건: 거래량 +500% 이상, MA180 상승추세(최근 n일 선형회귀 기울기>0), MA20 상향 돌파")
    print("-" * 78)
    print(f"{'날짜':<12} {'종가':>12} {'거래량':>16} {'전일대비 배수':>16}")
    print("-" * 78)

    if not rows:
        print("조건을 동시에 만족한 날짜가 없습니다.")
        print("-" * 78)
        return

    for r in rows:
        print(
            f"{r['dt']:<12} "
            f"{int(r['close']):>12,} "
            f"{int(r['volume']):>16,} "
            f"{float(r['ratio']):>15.2f}x"
        )
    print("-" * 78)
    print(f"총 {len(rows)}건")


def run(ticker_or_name: str, trend_window: int) -> None:
    if not core.APP_KEY or not core.APP_SECRET:
        raise RuntimeError("APP_KEY/APP_SECRET(.env) 설정이 필요합니다.")

    ticker, name = _resolve_ticker_and_name(ticker_or_name)
    token = core.get_token(core.APP_KEY, core.APP_SECRET)
    days = _fetch_daily_ohlcv(token=token, ticker=ticker, max_pages=40)
    result = _analyze_breakout(days=days, trend_window=trend_window)
    _print_result_table(name=name, ticker=ticker, rows=result)


def main():
    parser = argparse.ArgumentParser(
        description="거래량 급증 + MA180 상승추세 + MA20 상향 돌파 동시 만족일 탐색기"
    )
    parser.add_argument(
        "--ticker",
        default="",
        help="종목명 또는 6자리 종목코드 (예: 삼성전자 / 005930). 미입력 시 콘솔에서 입력받음",
    )
    parser.add_argument(
        "--trend-window",
        type=int,
        default=20,
        help="MA180 상승추세 회귀기울기 계산 구간(최근 n일, 기본값: 20)",
    )
    args = parser.parse_args()

    ticker_or_name = (args.ticker or "").strip()
    if not ticker_or_name:
        ticker_or_name = input("종목명(또는 6자리 종목코드)을 입력하세요: ").strip()

    if not ticker_or_name:
        raise RuntimeError("종목 입력이 비어 있습니다.")
    if args.trend_window < 2:
        raise RuntimeError("--trend-window는 2 이상이어야 합니다.")

    run(ticker_or_name=ticker_or_name, trend_window=args.trend_window)


if __name__ == "__main__":
    main()
