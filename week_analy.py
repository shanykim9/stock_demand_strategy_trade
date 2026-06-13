from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path
import webbrowser
from flask import Flask, jsonify, request

try:
	from dotenv import load_dotenv  # type: ignore
	load_dotenv()
except Exception:
	pass

import demand as core

app = Flask(__name__)


def _to_int(v, default=0) -> int:
	if v is None:
		return default
	if isinstance(v, int):
		return v
	s = str(v).strip().replace(",", "")
	if s == "":
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


def _dedupe_by_dt_desc(rows: list[dict], limit: int) -> list[dict]:
	rows2 = sorted(rows, key=lambda x: str(x.get("dt") or ""), reverse=True)
	seen = set()
	out = []
	for r in rows2:
		dt = str(r.get("dt") or "")
		if not dt or dt in seen:
			continue
		seen.add(dt)
		out.append(r)
		if len(out) >= limit:
			break
	return out


def _fetch_weekly_from_kiwoom(token: str, ticker: str, weeks: int) -> tuple[list[dict], dict]:
	end_dt = datetime.now(core.TZ).strftime("%Y%m%d")
	stex_tp = (os.getenv("KIWOOM_DMST_STEX_TP") or "KRX").strip().upper()
	upd_stkpc_tp = (os.getenv("KIWOOM_OHLCV_UPD_STKPC_TP") or "1").strip()

	api_candidates = ["ka10082"]  # 주봉
	endpoint = "/api/dostk/chart"
	last_err: Exception | None = None

	for api_id in api_candidates:
		common = {"stk_cd": ticker, "stex_tp": stex_tp, "dmst_stex_tp": stex_tp}
		bodies = [
			{**common, "base_dt": end_dt, "upd_stkpc_tp": upd_stkpc_tp},
			{**common, "base_dt": end_dt},
			{**common, "dt": end_dt, "upd_stkpc_tp": upd_stkpc_tp},
			{**common, "dt": end_dt},
		]
		for body in bodies:
			try:
				res = core.call_tr_all_pages(
					token=token,
					api_id=api_id,
					body=body,
					endpoint=endpoint,
					max_pages=40,
				)
				rows = res.get("rows") or []
				if not rows:
					continue
				out: list[dict] = []
				for r in rows:
					dt = _parse_dt_any(
						_first_non_empty(r, ["dt", "date", "bas_dt", "base_dt", "trde_dt", "trd_dt"])
					)
					if not dt:
						continue
					open_p = _to_int(_first_non_empty(r, ["open_pric", "open", "stck_oprc", "opn_prc"]), 0)
					high_p = _to_int(_first_non_empty(r, ["high_pric", "high", "stck_hgpr", "hgh_prc"]), 0)
					low_p = _to_int(_first_non_empty(r, ["low_pric", "low", "stck_lwpr", "low_prc"]), 0)
					close_p = _to_int(_first_non_empty(r, ["close_pric", "close", "stck_clpr", "cur_prc", "cur_pric"]), 0)
					vol = _to_int(_first_non_empty(r, ["trde_qty", "volume", "acml_vol", "acc_trde_qty"]), 0)
					amt = _to_int(
						_first_non_empty(
							r,
							["trde_prica", "trde_amt", "trde_val", "value", "acml_tr_pbmn", "acc_trde_amt", "tr_pbmn"],
						),
						0,
					)
					# 거래대금 필드가 없거나 0으로 내려오면 근사값(종가*거래량)으로 보정
					if amt <= 0 and close_p > 0 and vol > 0:
						amt = int(close_p) * int(vol)
					if max(open_p, high_p, low_p, close_p) <= 0:
						continue
					out.append(
						{
							"dt": dt,
							"open": open_p,
							"high": high_p,
							"low": low_p,
							"close": close_p,
							"volume": max(0, vol),
							"amount": max(0, amt),
						}
					)

				out = _dedupe_by_dt_desc(out, weeks)
				meta = {
					"source": "kiwoom_weekly",
					"api_id": api_id,
					"endpoint": endpoint,
					"raw_rows": int(res.get("raw_rows") or 0),
					"pages": int(res.get("pages") or 0),
					"body_keys": sorted(list(body.keys())),
				}
				return out, meta
			except Exception as e:
				last_err = e
				continue

	raise RuntimeError(f"주봉 조회 실패: {last_err}")


def _fetch_daily_aggregate_weekly(token: str, ticker: str, weeks: int) -> tuple[list[dict], dict]:
	end_dt = datetime.now(core.TZ).strftime("%Y%m%d")
	stex_tp = (os.getenv("KIWOOM_DMST_STEX_TP") or "KRX").strip().upper()
	upd_stkpc_tp = (os.getenv("KIWOOM_OHLCV_UPD_STKPC_TP") or "1").strip()

	common = {"stk_cd": ticker, "stex_tp": stex_tp, "dmst_stex_tp": stex_tp}
	bodies = [
		{**common, "base_dt": end_dt, "upd_stkpc_tp": upd_stkpc_tp},
		{**common, "base_dt": end_dt},
		{**common, "dt": end_dt, "upd_stkpc_tp": upd_stkpc_tp},
	]
	last_err: Exception | None = None

	for body in bodies:
		try:
			res = core.call_tr_all_pages(
				token=token,
				api_id="ka10081",
				body=body,
				endpoint="/api/dostk/chart",
				max_pages=80,
			)
			rows = res.get("rows") or []
			if not rows:
				continue

			daily: list[dict] = []
			for r in rows:
				dt = _parse_dt_any(_first_non_empty(r, ["dt", "date", "bas_dt", "base_dt", "trde_dt", "trd_dt"]))
				if not dt:
					continue
				open_p = _to_int(_first_non_empty(r, ["open_pric", "open", "stck_oprc", "opn_prc"]), 0)
				high_p = _to_int(_first_non_empty(r, ["high_pric", "high", "stck_hgpr", "hgh_prc"]), 0)
				low_p = _to_int(_first_non_empty(r, ["low_pric", "low", "stck_lwpr", "low_prc"]), 0)
				close_p = _to_int(_first_non_empty(r, ["close_pric", "close", "stck_clpr", "cur_prc", "cur_pric"]), 0)
				vol = _to_int(_first_non_empty(r, ["trde_qty", "volume", "acml_vol", "acc_trde_qty"]), 0)
				amt = _to_int(
					_first_non_empty(
						r,
						["trde_prica", "trde_amt", "trde_val", "value", "acml_tr_pbmn", "acc_trde_amt", "tr_pbmn"],
					),
					0,
				)
				if amt <= 0 and close_p > 0 and vol > 0:
					amt = int(close_p) * int(vol)
				if max(open_p, high_p, low_p, close_p) <= 0:
					continue
				daily.append(
					{
						"dt": dt,
						"open": open_p,
						"high": high_p,
						"low": low_p,
						"close": close_p,
						"volume": max(0, vol),
						"amount": max(0, amt),
					}
				)

			if not daily:
				continue

			daily.sort(key=lambda x: x["dt"])  # asc
			weekly_by_key: dict[str, list[dict]] = {}
			for d in daily:
				dt = datetime.strptime(d["dt"], "%Y-%m-%d")
				y, w, _ = dt.isocalendar()
				key = f"{y:04d}-W{w:02d}"
				weekly_by_key.setdefault(key, []).append(d)

			weekly: list[dict] = []
			for key in sorted(weekly_by_key.keys()):
				items = weekly_by_key[key]
				items.sort(key=lambda x: x["dt"])
				open_p = int(items[0]["open"])
				close_p = int(items[-1]["close"])
				high_p = max(int(x["high"]) for x in items)
				low_p = min(int(x["low"]) for x in items)
				vol = sum(int(x["volume"]) for x in items)
				amt = sum(int(x["amount"]) for x in items)
				weekly.append(
					{
						"dt": items[-1]["dt"],  # 주 마지막 거래일 기준
						"open": open_p,
						"high": high_p,
						"low": low_p,
						"close": close_p,
						"volume": vol,
						"amount": amt,
					}
				)

			weekly = sorted(weekly, key=lambda x: x["dt"], reverse=True)[:weeks]
			meta = {
				"source": "daily_aggregated_weekly",
				"api_id": "ka10081",
				"endpoint": "/api/dostk/chart",
				"raw_rows": int(res.get("raw_rows") or 0),
				"pages": int(res.get("pages") or 0),
				"body_keys": sorted(list(body.keys())),
			}
			return weekly, meta
		except Exception as e:
			last_err = e
			continue

	raise RuntimeError(f"일봉 집계 기반 주봉 생성 실패: {last_err}")


def _build_chart_html(ticker: str, name: str, weekly_desc: list[dict], meta: dict) -> str:
	data_asc = sorted(weekly_desc, key=lambda x: x["dt"])
	dates = [x["dt"] for x in data_asc]
	candles = [[x["open"], x["close"], x["low"], x["high"]] for x in data_asc]
	volumes = [int(x["volume"]) for x in data_asc]
	amounts = [int(x["amount"]) for x in data_asc]

	info_note = (
		f"source={meta.get('source','')} / api={meta.get('api_id','')} / pages={meta.get('pages',0)} / raw_rows={meta.get('raw_rows',0)}"
	)

	return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>주봉 분석 {ticker}</title>
  <script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 14px; background:#fafafa; color:#111; }}
    .title {{ font-size: 20px; font-weight: 700; margin-bottom: 4px; }}
    .meta {{ color:#666; font-size: 12px; margin-bottom: 10px; }}
    #chart {{ width: 100%; height: 920px; background:#fff; border:1px solid #e5e7eb; border-radius:10px; }}
  </style>
</head>
<body>
  <div class="title">주봉 차트 (300주): {name} ({ticker})</div>
  <div class="meta">{info_note}</div>
  <div id="chart"></div>
  <script>
    const dates = {json.dumps(dates, ensure_ascii=False)};
    const candles = {json.dumps(candles, ensure_ascii=False)};
    const volumes = {json.dumps(volumes, ensure_ascii=False)};
    const amounts = {json.dumps(amounts, ensure_ascii=False)};

    function calcMA(period, src){{
      const out = [];
      for (let i = 0; i < src.length; i++){{ 
        if (i < period - 1){{
          out.push("-");
          continue;
        }}
        let sum = 0;
        for (let j = 0; j < period; j++){{ 
          // candles: [open, close, low, high]
          sum += Number(src[i - j][1] || 0);
        }}
        out.push(Number((sum / period).toFixed(2)));
      }}
      return out;
    }}
    const ma20 = calcMA(20, candles);
    const ma60 = calcMA(60, candles);

    const chart = echarts.init(document.getElementById("chart"));
    const option = {{
      animation: false,
      legend: {{ top: 6, data: ["주봉", "MA20", "MA60", "거래량", "거래대금"] }},
      axisPointer: {{ link: [{{xAxisIndex: [0, 1, 2]}}] }},
      tooltip: {{
        trigger: "axis",
        axisPointer: {{ type: "cross" }}
      }},
      grid: [
        {{ left: 70, right: 20, top: 40, height: "52%" }},
        {{ left: 70, right: 20, top: "64%", height: "14%" }},
        {{ left: 70, right: 20, top: "82%", height: "14%" }}
      ],
      xAxis: [
        {{ type: "category", data: dates, boundaryGap: true, axisLine: {{ onZero: false }}, min: "dataMin", max: "dataMax" }},
        {{ type: "category", gridIndex: 1, data: dates, boundaryGap: true, axisLine: {{ onZero: false }}, axisTick: {{ show: false }}, axisLabel: {{ show: false }}, min: "dataMin", max: "dataMax" }},
        {{ type: "category", gridIndex: 2, data: dates, boundaryGap: true, axisLine: {{ onZero: false }}, axisTick: {{ show: false }}, min: "dataMin", max: "dataMax" }}
      ],
      yAxis: [
        {{ scale: true, splitArea: {{ show: true }} }},
        {{
          scale: true,
          gridIndex: 1,
          splitNumber: 2,
          name: "거래량(천 단위)",
          axisLabel: {{ formatter: (v) => (v / 1000).toLocaleString() }}
        }},
        {{
          scale: true,
          gridIndex: 2,
          splitNumber: 2,
          name: "거래대금(백만원 단위)",
          axisLabel: {{ formatter: (v) => (v / 1000000).toLocaleString() }}
        }}
      ],
      dataZoom: [
        {{ type: "inside", xAxisIndex: [0,1,2], start: 0, end: 100 }},
        {{ show: true, xAxisIndex: [0,1,2], type: "slider", bottom: 8, start: 0, end: 100 }}
      ],
      series: [
        {{
          name: "주봉",
          type: "candlestick",
          data: candles,
          itemStyle: {{
            color: "#ef4444",
            color0: "#2563eb",
            borderColor: "#ef4444",
            borderColor0: "#2563eb"
          }}
        }},
        {{
          name: "MA20",
          type: "line",
          data: ma20,
          smooth: false,
          showSymbol: false,
          lineStyle: {{ color: "#166534", width: 2.6 }},
          emphasis: {{ lineStyle: {{ width: 3.2 }} }}
        }},
        {{
          name: "MA60",
          type: "line",
          data: ma60,
          smooth: false,
          showSymbol: false,
          lineStyle: {{ color: "#5b3a29", width: 2.6 }},
          emphasis: {{ lineStyle: {{ width: 3.2 }} }}
        }},
        {{
          name: "거래량",
          type: "bar",
          xAxisIndex: 1,
          yAxisIndex: 1,
          data: volumes,
          itemStyle: {{ color: "rgba(99,102,241,0.65)" }}
        }},
        {{
          name: "거래대금",
          type: "bar",
          xAxisIndex: 2,
          yAxisIndex: 2,
          data: amounts,
          itemStyle: {{ color: "rgba(16,185,129,0.65)" }}
        }}
      ]
    }};
    chart.setOption(option);
    window.addEventListener("resize", () => chart.resize());
  </script>
</body>
</html>
"""


def _get_weekly_dataset(ticker_or_name: str, weeks: int) -> tuple[str, str, list[dict], dict]:
	if not core.APP_KEY or not core.APP_SECRET:
		raise RuntimeError("APP_KEY/APP_SECRET(.env) 설정이 필요합니다.")

	ticker = ticker_or_name.strip()
	name = ""
	if not (ticker.isdigit() and len(ticker) == 6):
		tk, err = core.resolve_ticker(ticker_or_name)
		if err or not tk:
			raise RuntimeError(err or "종목명을 다시확인해 주세요")
		ticker = tk
		try:
			name = (core._krx_cache.get("name_by_code") or {}).get(ticker, "")
		except Exception:
			name = ""

	if not name:
		name = ticker

	token = core.get_token(core.APP_KEY, core.APP_SECRET)

	weekly_desc: list[dict]
	meta: dict
	try:
		weekly_desc, meta = _fetch_weekly_from_kiwoom(token, ticker, weeks=weeks)
		if not weekly_desc:
			raise RuntimeError("주봉 데이터가 비어 있음")
		# 주봉 응답에서 거래대금이 모두 0이면 일봉 집계로 재생성
		if all(int(x.get("amount") or 0) <= 0 for x in weekly_desc):
			weekly_desc, meta = _fetch_daily_aggregate_weekly(token, ticker, weeks=weeks)
	except Exception:
		weekly_desc, meta = _fetch_daily_aggregate_weekly(token, ticker, weeks=weeks)

	if not weekly_desc:
		raise RuntimeError("차트 생성에 사용할 주봉 데이터가 없습니다.")

	weekly_desc = sorted(weekly_desc, key=lambda x: x["dt"], reverse=True)[:weeks]
	return ticker, name, weekly_desc, meta


def run(ticker_or_name: str, weeks: int, output_html: str, open_browser: bool):
	ticker, name, weekly_desc, meta = _get_weekly_dataset(ticker_or_name=ticker_or_name, weeks=weeks)
	html = _build_chart_html(ticker=ticker, name=name, weekly_desc=weekly_desc, meta=meta)

	out_path = Path(output_html).resolve()
	out_path.parent.mkdir(parents=True, exist_ok=True)
	out_path.write_text(html, encoding="utf-8")

	print(f"[OK] chart: {out_path}")
	print(f"[INFO] ticker={ticker}, weeks={len(weekly_desc)}, source={meta.get('source')}, api={meta.get('api_id')}")

	if open_browser:
		webbrowser.open(out_path.as_uri())


_WEB_HTML = """<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>주봉 분석기</title>
  <script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
  <style>
    body { font-family: Arial, sans-serif; margin: 14px; background:#fafafa; color:#111; }
    .card { background:#fff; border:1px solid #e5e7eb; border-radius:10px; padding:12px; }
    .title { font-size: 20px; font-weight: 700; margin-bottom: 10px; }
    .controls { display:flex; gap:8px; align-items:end; flex-wrap:wrap; margin-bottom:10px; }
    .field { display:flex; flex-direction:column; gap:4px; min-width:160px; }
    .label { color:#374151; font-size:12px; }
    .input { border:1px solid #cbd5e1; border-radius:8px; padding:8px 10px; font-size:14px; }
    .btn { border:1px solid #93c5fd; background:#eff6ff; color:#1e3a8a; border-radius:8px; padding:8px 12px; cursor:pointer; font-weight:600; }
    .meta { color:#666; font-size:12px; margin-bottom:8px; min-height:18px; }
    #chart { width: 100%; height: 920px; border:1px solid #e5e7eb; border-radius:10px; background:#fff; }
  </style>
</head>
<body>
  <div class="card">
    <div class="title">주봉 분석기 (최대 300주 기본)</div>
    <div class="controls">
      <label class="field">
        <span class="label">종목명/종목코드</span>
        <input id="tickerInput" class="input" type="text" placeholder="예: 삼성전자 또는 005930" />
      </label>
      <label class="field">
        <span class="label">조회 주수</span>
        <input id="weeksInput" class="input" type="number" min="20" max="1500" value="300" />
      </label>
      <button id="runBtn" class="btn" type="button">실행</button>
    </div>
    <div id="meta" class="meta"></div>
    <div id="chart"></div>
  </div>
  <script>
    const metaEl = document.getElementById("meta");
    const tickerEl = document.getElementById("tickerInput");
    const weeksEl = document.getElementById("weeksInput");
    const runBtn = document.getElementById("runBtn");
    const chart = echarts.init(document.getElementById("chart"));

    function setMeta(s){ metaEl.textContent = s || ""; }
    function toNum(v, d){ const n = Number(v); return Number.isFinite(n) ? n : d; }
    function calcMA(period, src){
      const out = [];
      for (let i = 0; i < src.length; i++){
        if (i < period - 1){
          out.push("-");
          continue;
        }
        let sum = 0;
        for (let j = 0; j < period; j++){
          sum += Number(src[i - j][1] || 0);
        }
        out.push(Number((sum / period).toFixed(2)));
      }
      return out;
    }

    function render(res){
      const dates = res.dates || [];
      const candles = res.candles || [];
      const volumes = res.volumes || [];
      const amounts = res.amounts || [];
      const option = {
        animation: false,
        legend: { top: 6, data: ["주봉", "MA20", "MA60", "거래량", "거래대금"] },
        axisPointer: { link: [{xAxisIndex: [0,1,2]}] },
        tooltip: { trigger: "axis", axisPointer: { type: "cross" } },
        grid: [
          { left: 70, right: 20, top: 40, height: "52%" },
          { left: 70, right: 20, top: "64%", height: "14%" },
          { left: 70, right: 20, top: "82%", height: "14%" }
        ],
        xAxis: [
          { type: "category", data: dates, boundaryGap: true, axisLine: { onZero: false }, min: "dataMin", max: "dataMax" },
          { type: "category", gridIndex: 1, data: dates, boundaryGap: true, axisLine: { onZero: false }, axisTick: { show: false }, axisLabel: { show: false }, min: "dataMin", max: "dataMax" },
          { type: "category", gridIndex: 2, data: dates, boundaryGap: true, axisLine: { onZero: false }, axisTick: { show: false }, min: "dataMin", max: "dataMax" }
        ],
        yAxis: [
          { scale: true, splitArea: { show: true } },
          {
            scale: true,
            gridIndex: 1,
            splitNumber: 2,
            name: "거래량(천 단위)",
            axisLabel: { formatter: (v) => (v / 1000).toLocaleString() }
          },
          {
            scale: true,
            gridIndex: 2,
            splitNumber: 2,
            name: "거래대금(백만원 단위)",
            axisLabel: { formatter: (v) => (v / 1000000).toLocaleString() }
          }
        ],
        dataZoom: [
          { type: "inside", xAxisIndex: [0,1,2], start: 0, end: 100 },
          { show: true, xAxisIndex: [0,1,2], type: "slider", bottom: 8, start: 0, end: 100 }
        ],
        series: [
          { name: "주봉", type: "candlestick", data: candles,
            itemStyle: { color: "#ef4444", color0: "#2563eb", borderColor: "#ef4444", borderColor0: "#2563eb" } },
          { name: "MA20", type: "line", data: calcMA(20, candles), smooth: false, showSymbol: false,
            lineStyle: { color: "#166534", width: 2.6 }, emphasis: { lineStyle: { width: 3.2 } } },
          { name: "MA60", type: "line", data: calcMA(60, candles), smooth: false, showSymbol: false,
            lineStyle: { color: "#5b3a29", width: 2.6 }, emphasis: { lineStyle: { width: 3.2 } } },
          { name: "거래량", type: "bar", xAxisIndex: 1, yAxisIndex: 1, data: volumes, itemStyle: { color: "rgba(99,102,241,0.65)" } },
          { name: "거래대금", type: "bar", xAxisIndex: 2, yAxisIndex: 2, data: amounts, itemStyle: { color: "rgba(16,185,129,0.65)" } }
        ]
      };
      chart.setOption(option, true);
    }

    async function runQuery(){
      const ticker = (tickerEl.value || "").trim();
      const weeks = Math.max(20, Math.min(1500, Math.floor(toNum(weeksEl.value, 300))));
      if (!ticker){ setMeta("종목명 또는 종목코드를 입력해 주세요."); return; }
      setMeta("조회 중...");
      try{
        const r = await fetch(`/api/week-analy?ticker=${encodeURIComponent(ticker)}&weeks=${weeks}`);
        const d = await r.json();
        if (!d.ok){ setMeta(`오류: ${d.error || "unknown"}`); return; }
        render(d);
        setMeta(`${d.name}(${d.ticker}) · ${d.count}주 · source=${d.meta.source} / api=${d.meta.api_id} / pages=${d.meta.pages}`);
      }catch(e){
        setMeta(`오류: ${String(e)}`);
      }
    }

    runBtn.addEventListener("click", runQuery);
    tickerEl.addEventListener("keydown", (e) => { if (e.key === "Enter") runQuery(); });
    window.addEventListener("resize", () => chart.resize());
  </script>
</body>
</html>
"""


@app.get("/")
def index():
	return _WEB_HTML


@app.get("/api/week-analy")
def api_week_analy():
	ticker_or_name = (request.args.get("ticker") or "").strip()
	weeks_raw = (request.args.get("weeks") or "300").strip()
	if not ticker_or_name:
		return jsonify({"ok": False, "error": "ticker is required"}), 400
	try:
		weeks = max(20, min(1500, int(weeks_raw)))
	except Exception:
		weeks = 300

	try:
		ticker, name, weekly_desc, meta = _get_weekly_dataset(ticker_or_name=ticker_or_name, weeks=weeks)
		data_asc = sorted(weekly_desc, key=lambda x: x["dt"])
		return jsonify(
			{
				"ok": True,
				"ticker": ticker,
				"name": name,
				"count": len(data_asc),
				"dates": [x["dt"] for x in data_asc],
				"candles": [[x["open"], x["close"], x["low"], x["high"]] for x in data_asc],
				"volumes": [int(x["volume"]) for x in data_asc],
				"amounts": [int(x["amount"]) for x in data_asc],
				"meta": meta,
			}
		)
	except Exception as e:
		return jsonify({"ok": False, "error": str(e)}), 500


def serve(host: str = "127.0.0.1", port: int = 7792, open_browser: bool = False):
	url = f"http://{host}:{port}"
	print(f"[INFO] week_analy server: {url}")
	if open_browser:
		webbrowser.open(url)
	app.run(host=host, port=port, debug=False)


def main():
	parser = argparse.ArgumentParser(description="키움 주봉(300주) OHLC + 거래량 + 거래대금 차트")
	parser.add_argument("--ticker", help="6자리 종목코드 또는 종목명 (예: 005930 / 삼성전자)")
	parser.add_argument("--weeks", type=int, default=300, help="조회 주수 (기본 300)")
	parser.add_argument(
		"--output",
		default=str(Path("data") / "week_analy_chart.html"),
		help="출력 HTML 경로 (기본: data/week_analy_chart.html)",
	)
	parser.add_argument("--open", action="store_true", help="실행 시 브라우저 열기")
	parser.add_argument("--serve", action="store_true", help="웹 UI 서버 모드로 실행")
	parser.add_argument("--host", default="127.0.0.1", help="서버 호스트 (기본: 127.0.0.1)")
	parser.add_argument("--port", type=int, default=7792, help="서버 포트 (기본: 7792)")
	args = parser.parse_args()

	weeks = max(20, min(1500, int(args.weeks)))
	if args.serve or not args.ticker:
		serve(host=str(args.host), port=int(args.port), open_browser=bool(args.open))
		return

	run(ticker_or_name=args.ticker, weeks=weeks, output_html=args.output, open_browser=bool(args.open))


if __name__ == "__main__":
	main()
