from __future__ import annotations

import os
import json
import re
import time
import uuid
import threading
import queue
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from flask import Flask, jsonify, render_template_string, request, Response, stream_with_context

# 기존 demand.py의 검증된 유틸/REST 호출을 재사용합니다.
# - import 시 Flask app이 생성되지만 run 되지는 않음(__main__만 실행)
import demand as core


TZ = core.TZ

APP_KEY = os.getenv("KIWOOM_APP_KEY") or os.getenv("APP_KEY") or ""
APP_SECRET = os.getenv("KIWOOM_APP_SECRET") or os.getenv("APP_SECRET") or ""

DEFAULT_EXCHANGE = (os.getenv("KIWOOM_DMST_STEX_TP") or "KRX").strip().upper()
UPD_STKPC_TP = (os.getenv("KIWOOM_OHLCV_UPD_STKPC_TP") or "1").strip()


def _env_int(name: str, default: int, min_value: int | None = None, max_value: int | None = None) -> int:
	try:
		v = int((os.getenv(name) or str(default)).strip())
	except Exception:
		v = int(default)
	if min_value is not None:
		v = max(min_value, v)
	if max_value is not None:
		v = min(max_value, v)
	return v


def _env_float(name: str, default: float, min_value: float | None = None, max_value: float | None = None) -> float:
	try:
		v = float((os.getenv(name) or str(default)).strip())
	except Exception:
		v = float(default)
	if min_value is not None:
		v = max(min_value, v)
	if max_value is not None:
		v = min(max_value, v)
	return v


MAX_CANDIDATES = _env_int("SIMULDEMAND_MAX_CANDIDATES", 500, min_value=1, max_value=500)
SIMULDEMAND_WORKERS = _env_int("SIMULDEMAND_WORKERS", 2, min_value=1, max_value=3)
SIMULDEMAND_ITEM_SLEEP_SEC = _env_float("SIMULDEMAND_ITEM_SLEEP_SEC", 0.05, min_value=0.0, max_value=2.0)
SIMULDEMAND_API_ITEM_SLEEP_SEC = _env_float("SIMULDEMAND_API_ITEM_SLEEP_SEC", 0.05, min_value=0.0, max_value=2.0)


app = Flask(__name__)

DEFAULT_STOP_LOSS_PCT = 8
DEFAULT_TRAILING_DROP_PCT = 10
AUTO_STOP_CASES = [5, 7, 10]
AUTO_TRAILING_CASES = [10, 7, 5]


def _fmt_pct_int(v: int) -> str:
	return f"{int(v)}%"


def _build_assumption_text(stop_loss_pct: int, trailing_drop_pct: int) -> str:
	return (
		"가정: 신호일 다음 거래일 시가 매수. "
		f"익절은 매수 후 최고가 대비 -{_fmt_pct_int(trailing_drop_pct)} 트레일링 조건 발생 시 다음날 시가 매도, "
		f"손절은 당일 종가가 매수가 대비 -{_fmt_pct_int(stop_loss_pct)} 이하이면 다음날 시가 매도."
	)


ASSUMPTION_TEXT = _build_assumption_text(DEFAULT_STOP_LOSS_PCT, DEFAULT_TRAILING_DROP_PCT)


def _clamp_stop_loss_pct(v: int) -> int:
	return max(5, min(10, int(v)))


def _clamp_trailing_drop_pct(v: int) -> int:
	return max(3, min(10, int(v)))


@dataclass
class _Job:
	id: str
	created_at: float
	days: int
	stop_loss_pct: int
	trailing_drop_pct: int
	cands: list[str]
	total: int
	events: list[dict]
	q: "queue.Queue[dict]"
	done: bool = False
	error: str | None = None


@dataclass
class _AutoJob:
	id: str
	created_at: float
	days: int
	cands: list[str]
	total: int
	events: list[dict]
	q: "queue.Queue[dict]"
	done: bool = False
	error: str | None = None


_JOBS: dict[str, _Job] = {}
_JOBS_LOCK = threading.Lock()
_AUTO_JOBS: dict[str, _AutoJob] = {}
_AUTO_JOBS_LOCK = threading.Lock()


def _emit(job: _Job, payload: dict):
	# payload는 반드시 JSON 직렬화 가능해야 함
	job.events.append(payload)
	try:
		job.q.put(payload, timeout=0.1)
	except Exception:
		# SSE 리스너가 없거나 큐가 잠깐 막혀도 작업은 계속
		pass


@dataclass
class SignalSimRow:
	signal_dt: str
	buy_dt: str
	buy_open: int
	exit_type: str
	trigger_dt: str | None
	sell_dt: str | None
	sell_open: int | None
	ret_pct: float | None


def _norm_lines(text: str) -> list[str]:
	text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
	return [ln.strip() for ln in text.split("\n") if ln.strip()]


def _extract_candidates_from_md(md_text: str) -> list[str]:
	"""
	MD 파일에서 종목 후보를 최대한 넓게 추출합니다.
	- 6자리 종목코드(우선)
	- 그 외: 라인 전체를 종목명 후보로 취급(예: "- 한화솔루션")
	"""
	text = (md_text or "").replace("\r\n", "\n").replace("\r", "\n")
	# 다양한 구분자를 콤마로 통일
	text = text.replace("，", ",").replace("、", ",").replace(";", ",")

	# 핵심: 줄 단위가 아니라 "콤마/줄바꿈" 기준으로 토큰화
	raw_tokens = [t.strip() for t in re.split(r"[,\n]+", text) if t.strip()]

	out: list[str] = []
	for tok in raw_tokens:
		# 마크다운/목록 기호 제거 (예: "##### 제목", "- 항목", "> 인용")
		tok = re.sub(r"^\s*[#>*-]+\s*", "", tok).strip()
		# 괄호만 단독으로 남는 경우 제거
		tok = tok.strip(" \t-•·")
		if not tok:
			continue

		# 토큰 안에 6자리 코드가 섞여 있으면 코드 우선 추출
		codes = re.findall(r"\b\d{6}\b", tok)
		if codes:
			out.extend(codes)
			continue

		# 너무 짧은 토큰 제외
		if len(tok) >= 2:
			out.append(tok)
	# 중복 제거(순서 유지)
	seen = set()
	uniq: list[str] = []
	for x in out:
		if x in seen:
			continue
		seen.add(x)
		uniq.append(x)
	return uniq


def _resolve_to_ticker(q: str) -> tuple[str | None, str]:
	"""
	return: (ticker6 or None, display_name)
	"""
	q = (q or "").strip()
	if re.fullmatch(r"\d{6}", q):
		return q, q
	tk, err = core.resolve_ticker(q)
	if tk and not err:
		return tk, f"{q} → {tk}"
	return None, f"{q} (해석 실패)"


def _find_down2_up2_dates(daily_list: list[dict]) -> list[str]:
	"""
	연속 2일 이상 '감소' 후, 다시 연속 2일 이상 '증가'로 전환되는 날짜를 찾습니다.

	정의:
	- 감소: 오늘 수급 < 전일 수급
	- 증가: 오늘 수급 > 전일 수급
	- '연속 2일': 위 비교가 2회 연속(최소 3거래일 구간)
	- 기록 날짜: 증가 구간 시작일(dt)
	"""
	pts: list[tuple[str, int]] = []
	for x in (daily_list or []):
		dt = core._parse_dt_any(x.get("dt"))
		if not dt:
			continue
		try:
			v = int(x.get("net_trade_qty") or 0)
		except Exception:
			continue
		pts.append((dt, v))
	pts.sort(key=lambda t: t[0])
	n = len(pts)
	if n < 4:
		return []

	out: list[str] = []
	i = 1
	while i < n:
		neg = 0
		while i < n and pts[i][1] < pts[i - 1][1]:
			neg += 1
			i += 1
		if neg >= 2:
			pos = 0
			pos_start_idx = i
			while i < n and pts[i][1] > pts[i - 1][1]:
				if pos == 0:
					pos_start_idx = i
				pos += 1
				i += 1
			if pos >= 2 and 0 <= pos_start_idx < n:
				out.append(pts[pos_start_idx][0])
			continue
		if neg == 0:
			i += 1

	seen = set()
	uniq = []
	for d in out:
		if d in seen:
			continue
		seen.add(d)
		uniq.append(d)
	return uniq


def _fetch_investor_daily(token: str, stk_cd: str, days=90) -> dict[str, list[dict]]:
	"""
	ka10060 기반: 외국인/기관 일별 순매수 수량(수급)만 추출.
	"""
	end_dt = datetime.now(TZ).strftime("%Y%m%d")
	res = core.call_tr_all_pages(
		token=token,
		api_id="ka10060",
		body={
			"dt": end_dt,
			"stk_cd": stk_cd,
			"amt_qty_tp": "2",  # 수량
			"trde_tp": "0",     # 순매수
			"unit_tp": "1",     # 단주
		},
		endpoint="/api/dostk/chart",
		max_pages=30,
	)
	rows = res["rows"]

	foreign_daily = []
	inst_daily = []
	for row in rows:
		dt = core._parse_dt_any(core._first_non_empty(row, ["dt", "date", "trde_dt", "trd_dt", "bas_dt"]))
		if not dt:
			continue
		frgn = core._to_int(core._first_non_empty(row, ["frgnr_invsr", "frgnr"]), 0)
		orgn = core._to_int(core._first_non_empty(row, ["orgn"]), 0)
		foreign_daily.append({"dt": dt, "net_trade_qty": int(frgn)})
		inst_daily.append({"dt": dt, "net_trade_qty": int(orgn)})

	# 최신→과거로 내려오는 경우가 있어 날짜 unique 후 최신 기준으로 슬라이스
	def _unique(xs):
		seen = set()
		out = []
		for x in sorted(xs, key=lambda z: z["dt"], reverse=True):
			if x["dt"] in seen:
				continue
			seen.add(x["dt"])
			out.append(x)
		return out[: int(days)]

	return {"foreign": _unique(foreign_daily), "institution": _unique(inst_daily)}


def _fetch_ohlc_map(token: str, stk_cd: str, pages=18) -> dict[str, dict]:
	"""
	ka10081 일봉 OHLC (시가/고가/저가/종가) 맵을 가져옵니다.
	- 트레일링익절/손절 시뮬레이션용
	"""
	end_dt = datetime.now(TZ).strftime("%Y%m%d")
	body = {
		"stk_cd": stk_cd,
		"base_dt": end_dt,
		"upd_stkpc_tp": UPD_STKPC_TP,
		"stex_tp": DEFAULT_EXCHANGE,
		"dmst_stex_tp": DEFAULT_EXCHANGE,
	}
	res = core.call_tr_all_pages(token, api_id="ka10081", body=body, endpoint="/api/dostk/chart", max_pages=int(pages))
	rows = res["rows"]

	m: dict[str, dict] = {}
	for r in rows:
		dt = core._parse_dt_any(core._first_non_empty(r, ["dt", "date", "bas_dt", "base_dt", "trde_dt", "trd_dt"]))
		if not dt:
			continue
		o = core._to_int(core._first_non_empty(r, ["open_pric", "open"]), 0)
		hi = core._to_int(core._first_non_empty(r, ["high_pric", "high"]), 0)
		lo = core._to_int(core._first_non_empty(r, ["low_pric", "low"]), 0)
		cl = core._to_int(core._first_non_empty(r, ["close_pric", "close", "cur_prc", "cur_pric"]), 0)
		if min(o, hi, lo, cl) <= 0:
			continue
		m[dt] = {"open": int(o), "high": int(hi), "low": int(lo), "close": int(cl)}
	return m


def _prepare_stock_data(token: str, ticker: str, days=90) -> dict[str, Any]:
	inv = _fetch_investor_daily(token, ticker, days=days)
	f_rev = _find_down2_up2_dates(inv["foreign"])
	i_rev = _find_down2_up2_dates(inv["institution"])
	matched = sorted(list(set(f_rev).intersection(set(i_rev))))
	ohlc_map = _fetch_ohlc_map(token, ticker, pages=18)
	return {
		"ticker": ticker,
		"foreign_signal_dates": f_rev,
		"institution_signal_dates": i_rev,
		"matched_signal_dates": matched,
		"signals_count": len(matched),
		"ohlc_map": ohlc_map,
	}


def _simulate_trailing_stop(
	matched_dates: list[str],
	ohlc_map: dict[str, dict],
	stop_loss_pct: int = DEFAULT_STOP_LOSS_PCT,
	trailing_drop_pct: int = DEFAULT_TRAILING_DROP_PCT,
) -> list[SignalSimRow]:
	dates = sorted([d for d in ohlc_map.keys() if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d)])
	if not dates:
		return []
	idx = {d: i for i, d in enumerate(dates)}

	out: list[SignalSimRow] = []
	for sig in matched_dates:
		sig_dt = core._parse_dt_any(sig)
		if not sig_dt or sig_dt not in idx:
			continue
		i = idx[sig_dt]
		if i + 1 >= len(dates):
			continue
		buy_dt = dates[i + 1]
		buy_open = int((ohlc_map.get(buy_dt) or {}).get("open") or 0)
		if buy_open <= 0:
			continue

		peak_high = int((ohlc_map.get(buy_dt) or {}).get("high") or 0)
		if peak_high <= 0:
			peak_high = buy_open

		exit_type = "미청산"
		trigger_dt: str | None = None
		sell_dt: str | None = None
		sell_open: int | None = None
		ret_pct: float | None = None

		# 매수일 고가부터 최고가 추적 시작
		for j in range(i + 1, len(dates) - 1):
			dt = dates[j]
			row = ohlc_map.get(dt) or {}
			hi = int(row.get("high") or 0)
			lo = int(row.get("low") or 0)
			cl = int(row.get("close") or 0)
			if hi > 0:
				peak_high = max(peak_high, hi)

			trailing_trigger = False
			if peak_high > 0 and lo > 0:
				trailing_price = peak_high * (1.0 - (float(trailing_drop_pct) / 100.0))
				trailing_trigger = lo <= trailing_price

			stop_trigger = cl > 0 and cl <= (buy_open * (1.0 - (float(stop_loss_pct) / 100.0)))

			if not trailing_trigger and not stop_trigger:
				continue

			# 같은 날 동시 충족 시: 장중 저가 기반 트레일링이 종가 기반 손절보다 먼저 발생한 것으로 간주
			if trailing_trigger:
				exit_type = "익절(트레일링)"
			else:
				exit_type = "손절"
			trigger_dt = dt

			next_dt = dates[j + 1]
			next_open = int((ohlc_map.get(next_dt) or {}).get("open") or 0)
			if next_open > 0:
				sell_dt = next_dt
				sell_open = next_open
				ret_pct = round((next_open - buy_open) * 100.0 / buy_open, 2)
			else:
				exit_type = f"{exit_type} 신호(다음날 시가 없음)"
			break

		out.append(
			SignalSimRow(
				signal_dt=sig_dt,
				buy_dt=buy_dt,
				buy_open=buy_open,
				exit_type=exit_type,
				trigger_dt=trigger_dt,
				sell_dt=sell_dt,
				sell_open=sell_open,
				ret_pct=ret_pct,
			)
		)
	return out


def simulate_one(
	token: str,
	ticker: str,
	days=90,
	stop_loss_pct: int = DEFAULT_STOP_LOSS_PCT,
	trailing_drop_pct: int = DEFAULT_TRAILING_DROP_PCT,
) -> dict[str, Any]:
	"""
	종목 1개에 대해:
	- 수급(외국인/기관) 패턴 전환일 찾기
	- 일치 신호일 계산
	- 일치 신호일 기반 트레일링익절/손절 시뮬레이션
	"""
	prepared = _prepare_stock_data(token, ticker, days=days)
	f_rev = prepared["foreign_signal_dates"]
	i_rev = prepared["institution_signal_dates"]
	matched = prepared["matched_signal_dates"]
	ohlc_map = prepared["ohlc_map"]
	sim_rows = _simulate_trailing_stop(
		matched,
		ohlc_map,
		stop_loss_pct=int(stop_loss_pct),
		trailing_drop_pct=int(trailing_drop_pct),
	)

	# 요약: 가장 최근 신호(있으면)
	latest = None
	if sim_rows:
		latest = sim_rows[-1]

	return {
		"ticker": ticker,
		"foreign_signal_dates": f_rev,
		"institution_signal_dates": i_rev,
		"matched_signal_dates": matched,
		"signals_count": len(matched),
		"latest": latest.__dict__ if latest else None,
		"rows": [r.__dict__ for r in sim_rows],
		"assumption": _build_assumption_text(int(stop_loss_pct), int(trailing_drop_pct)),
		"definition": "감소: 오늘<전일, 증가: 오늘>전일. 각 '연속 2일'은 증감 비교 2회 연속(최소 3거래일)이며, 기록 날짜는 증가 구간 시작일(dt).",
	}


HTML = """
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>기관/외국인 수급 전략 시뮬레이터</title>
  <link rel="stylesheet" href="/static/styles.css" />
  <style>
    .mono { font-family: var(--mono); }
    .right { text-align: right; }
    .small { font-size: 12px; color: var(--muted); }
    .warn { color: var(--warn); }
    .ok { color: var(--good); }
    .err { color: var(--bad); }
    .rowDone { background: rgba(34,197,94,0.06); }
    .rowFail { background: rgba(239,68,68,0.06); }
    .btnSmall { padding: 6px 10px; font-size: 12px; }
    .signals { display: flex; flex-wrap: wrap; gap: 6px; }
    .sig { display: inline-block; padding: 2px 8px; border: 1px solid var(--border); border-radius: 999px; background: rgba(15,23,42,0.04); }
    .detailRow { background: rgba(15,23,42,0.02); }
    .hidden { display: none; }
  </style>
</head>
<body>
  <div class="container">
    <header class="header">
      <div>
        <div class="title">기관/외국인 수급 전략 시뮬레이터</div>
        <div class="subtitle">MD 업로드 → 종목별 일치 신호 → 다음날 시가 매수 → 트레일링익절/손절 시뮬레이션</div>
      </div>
      <div class="hint">서버: <span class="mono">{{ base }}</span></div>
    </header>

    <section class="card">
      <div class="cardTitle">MD 파일 업로드</div>
      <form method="post" action="/simulate" enctype="multipart/form-data" class="controls">
        <label class="field">
          <span class="label">파일(.md)</span>
          <input class="input mono" type="file" name="file" accept=".md,text/markdown,text/plain" required />
        </label>
        <label class="field">
          <span class="label">기간(days)</span>
          <input class="input mono" type="number" name="days" min="10" max="365" step="1" value="{{ days }}" />
        </label>
        <label class="field">
          <span class="label">손절률(%, 5~10)</span>
          <input class="input mono" type="number" name="stop_loss_pct" min="5" max="10" step="1" value="{{ stop_loss_pct }}" />
        </label>
        <label class="field">
          <span class="label">트레일링 스탑(최고점 대비 하락 %, 3~10)</span>
          <input class="input mono" type="number" name="trailing_drop_pct" min="3" max="10" step="1" value="{{ trailing_drop_pct }}" />
        </label>
        <button class="btn" type="submit">시뮬레이션 실행</button>
        <button class="btn" type="submit" formaction="/simulate-auto">자동분석(9 CASE)</button>
      </form>
      <div class="small" style="margin-top:8px">
        - 종목 표기는 <b>6자리 코드</b>(예: 005930) 또는 <b>종목명</b>(예: 한화솔루션)을 지원합니다.<br/>
        - “수급”은 ka10060의 <b>일별 순매수 수량</b>을 사용합니다.<br/>
        - 자동분석은 손절률(5/7/10) x 트레일링(10/7/5) 조합 9개를 순차 실행합니다.
      </div>
    </section>

    {% if job_id %}
    <section class="card">
      <div class="cardTitle">결과(종목별, 처리되는 대로 표시)</div>
      <div class="kv" style="margin-bottom:10px">
        <div><span class="k">JOB</span><span class="v mono">{{ job_id }}</span></div>
        <div><span class="k">진행</span><span class="v mono" id="prog">0 / {{ total }}</span></div>
        <div><span class="k">성공</span><span class="v mono ok" id="okCnt">0</span></div>
        <div><span class="k">실패</span><span class="v mono err" id="failCnt">0</span></div>
        <div><span class="k">상태</span><span class="v mono" id="status">대기</span></div>
      </div>
      <div style="margin-bottom:12px; border:1px solid var(--border); border-radius:10px; padding:10px; background:rgba(15,23,42,0.02)">
        <div class="cardTitle" style="margin-bottom:8px">결과 분석 요약</div>
        <div class="kv" style="margin-bottom:8px">
          <div><span class="k">분석완료 종목</span><span class="v mono" id="anaDone">0</span></div>
          <div><span class="k">청산완료 거래수</span><span class="v mono" id="anaClosedTrades">0</span></div>
          <div><span class="k">평균 수익률(%)</span><span class="v mono" id="anaAvgRet">0.00</span></div>
          <div><span class="k">누적 수익률(단순합, %)</span><span class="v mono" id="anaSumRet">0.00</span></div>
          <div><span class="k">수익 종목수</span><span class="v mono ok" id="anaProfitCnt">0</span></div>
          <div><span class="k">손실 종목수</span><span class="v mono err" id="anaLossCnt">0</span></div>
        </div>
        <div class="small" style="margin-bottom:4px"><b>수익 종목</b></div>
        <div id="anaProfitList" class="small mono" style="margin-bottom:8px">-</div>
        <div class="small" style="margin-bottom:4px"><b>손실 종목</b></div>
        <div id="anaLossList" class="small mono">-</div>
      </div>
      <div class="tableWrap">
        <table class="table" style="min-width: 980px">
          <thead>
            <tr>
              <th>종목명</th>
              <th>종목코드</th>
              <th class="right">일치 신호 개수</th>
              <th>신호일</th>
              <th class="right">처리시간(ms)</th>
              <th>보기</th>
            </tr>
          </thead>
          <tbody id="tbody"></tbody>
        </table>
      </div>
      <div class="note">{{ assumption }}</div>
      <div id="noSignalWrap" class="hidden" style="margin-top:12px">
        <button id="btnShowNoSignal" class="btn" type="button">신호일이 없는 종목 한번에 보기 (0)</button>
        <div id="noSignalPanel" class="hidden" style="margin-top:10px">
          <div class="small" style="margin-bottom:8px">신호일이 없거나 처리 중 오류가 발생한 종목입니다.</div>
          <div class="tableWrap">
            <table class="table" style="min-width: 860px">
              <thead>
                <tr>
                  <th>종목명</th>
                  <th>종목코드</th>
                  <th>상태</th>
                  <th class="right">처리시간(ms)</th>
                  <th>비고</th>
                </tr>
              </thead>
              <tbody id="noSignalBody"></tbody>
            </table>
          </div>
        </div>
      </div>
    </section>
    <script>
      (function(){
        const jobId = {{ job_id|tojson }};
        const total = {{ total|tojson }};
        const tbody = document.getElementById("tbody");
        const noSignalWrap = document.getElementById("noSignalWrap");
        const noSignalBody = document.getElementById("noSignalBody");
        const noSignalPanel = document.getElementById("noSignalPanel");
        const btnShowNoSignal = document.getElementById("btnShowNoSignal");
        const prog = document.getElementById("prog");
        const okCntEl = document.getElementById("okCnt");
        const failCntEl = document.getElementById("failCnt");
        const statusEl = document.getElementById("status");
        const anaDoneEl = document.getElementById("anaDone");
        const anaClosedTradesEl = document.getElementById("anaClosedTrades");
        const anaAvgRetEl = document.getElementById("anaAvgRet");
        const anaSumRetEl = document.getElementById("anaSumRet");
        const anaProfitCntEl = document.getElementById("anaProfitCnt");
        const anaLossCntEl = document.getElementById("anaLossCnt");
        const anaProfitListEl = document.getElementById("anaProfitList");
        const anaLossListEl = document.getElementById("anaLossList");
        let done = 0, ok = 0, fail = 0;
        let noSignalCount = 0;
        const stockAgg = new Map();

        function refreshNoSignalButtonText() {
          if (!btnShowNoSignal || !noSignalPanel) return;
          btnShowNoSignal.textContent = noSignalPanel.classList.contains("hidden")
            ? `신호일이 없는 종목 한번에 보기 (${noSignalCount})`
            : `신호일이 없는 종목 닫기 (${noSignalCount})`;
        }

        function fmtNum(v){
          if (v === null || v === undefined || v === "") return "";
          const n = Number(v);
          if (!Number.isFinite(n)) return String(v);
          return n.toLocaleString("ko-KR");
        }
        function fmtPct(v){
          if (v === null || v === undefined || v === "") return "";
          const n = Number(v);
          if (!Number.isFinite(n)) return String(v);
          const s = n.toFixed(2);
          return (n > 0 ? "+" : "") + s;
        }

        function renderSignals(signalDates){
          const xs = Array.isArray(signalDates) ? signalDates : [];
          if (!xs.length) return `<span class="small">-</span>`;
          const maxShow = 6;
          const shown = xs.slice(-maxShow);
          const more = xs.length - shown.length;
          const pills = shown.map(d => `<span class="sig mono">${d}</span>`).join("");
          const tail = more > 0 ? `<span class="sig mono">+${more}</span>` : "";
          return `<div class="signals">${pills}${tail}</div>`;
        }

        function renderDetailTable(rows){
          const xs = Array.isArray(rows) ? rows : [];
          if (!xs.length) return `<div class="small">일치 신호가 없어 상세 결과가 없습니다.</div>`;
          const head = `
            <table class="table" style="min-width: 980px; margin-top:8px">
              <thead>
                <tr>
                  <th>신호일</th>
                  <th>매수일</th>
                  <th class="right">매수가(시가)</th>
                  <th>청산유형</th>
                  <th>청산조건발생일</th>
                  <th>청산일</th>
                  <th class="right">청산가(시가)</th>
                  <th class="right">수익률(%)</th>
                </tr>
              </thead>
              <tbody>
          `;
          const body = xs.map(d => `
            <tr>
              <td class="mono">${d.signal_dt || ""}</td>
              <td class="mono">${d.buy_dt || ""}</td>
              <td class="right mono">${fmtNum(d.buy_open || "")}</td>
              <td class="mono">${d.exit_type || ""}</td>
              <td class="mono">${d.trigger_dt || ""}</td>
              <td class="mono">${d.sell_dt || ""}</td>
              <td class="right mono">${fmtNum(d.sell_open || "")}</td>
              <td class="right mono">${fmtPct(d.ret_pct)}</td>
            </tr>
          `).join("");
          const tail = `</tbody></table>`;
          return head + body + tail;
        }

        function toPctText(n){
          if (!Number.isFinite(n)) return "";
          const s = n.toFixed(2);
          return (n > 0 ? "+" : "") + s + "%";
        }

        function renderNameList(items){
          if (!items.length) return "-";
          return items.map(x => `${x.name}(${x.code}) ${toPctText(x.avgRet)}`).join(", ");
        }

        function updateAnalysisSummary(){
          let analyzed = 0;
          let closedTrades = 0;
          let retSum = 0.0;
          let retCount = 0;
          const profitStocks = [];
          const lossStocks = [];

          stockAgg.forEach((s) => {
            analyzed += 1;
            closedTrades += s.closedTrades;
            retSum += s.retSum;
            retCount += s.retCount;
            if (s.avgRet > 0) profitStocks.push({ name: s.name, code: s.code, avgRet: s.avgRet });
            else if (s.avgRet < 0) lossStocks.push({ name: s.name, code: s.code, avgRet: s.avgRet });
          });

          const avgRet = retCount > 0 ? (retSum / retCount) : 0.0;
          anaDoneEl.textContent = fmtNum(analyzed);
          anaClosedTradesEl.textContent = fmtNum(closedTrades);
          anaAvgRetEl.textContent = toPctText(avgRet) || "0.00%";
          anaSumRetEl.textContent = toPctText(retSum) || "0.00%";
          anaProfitCntEl.textContent = fmtNum(profitStocks.length);
          anaLossCntEl.textContent = fmtNum(lossStocks.length);
          anaProfitListEl.textContent = renderNameList(profitStocks);
          anaLossListEl.textContent = renderNameList(lossStocks);
        }

        function addRow(r){
          const jobKey = String(r.key || "");
          const isFail = (r.note && (r.note.includes("오류") || r.note.includes("해석 실패")));
          const name = r.name || r.input || r.display || "";
          const code = r.ticker || "-";
          const sigCnt = Number(r.signals_count || 0);
          const sigDates = Array.isArray(r.signal_dates) ? r.signal_dates : [];
          const details = Array.isArray(r.rows) ? r.rows : [];
          const elapsedMs = Number(r.elapsed_ms || 0);
          const hasSignal = sigCnt > 0;

          const closedRets = details
            .map(d => Number(d.ret_pct))
            .filter(v => Number.isFinite(v));
          const retSum = closedRets.reduce((a, b) => a + b, 0.0);
          const retCount = closedRets.length;
          const avgRet = retCount > 0 ? (retSum / retCount) : 0.0;
          stockAgg.set(jobKey, {
            name,
            code,
            closedTrades: retCount,
            retSum,
            retCount,
            avgRet,
          });
          updateAnalysisSummary();

          if (!hasSignal || isFail) {
            noSignalCount += 1;
            noSignalWrap.classList.remove("hidden");
            refreshNoSignalButtonText();

            const trNs = document.createElement("tr");
            trNs.className = isFail ? "rowFail" : "";
            trNs.innerHTML = `
              <td class="mono">${name}</td>
              <td class="mono">${code}</td>
              <td class="mono">${isFail ? "오류" : "신호없음"}</td>
              <td class="right mono">${fmtNum(elapsedMs || "")}</td>
              <td class="mono small ${isFail ? "err" : ""}">${isFail ? (r.note || "") : "-"}</td>
            `;
            noSignalBody.appendChild(trNs);
            return;
          }

          // 요약행
          const tr = document.createElement("tr");
          tr.className = "rowDone";

          const btnLabel = details.length ? "더보기" : "더보기";
          tr.innerHTML = `
            <td class="mono">${name}</td>
            <td class="mono">${code}</td>
            <td class="right mono">${fmtNum(sigCnt)}</td>
            <td>${renderSignals(sigDates)}</td>
            <td class="right mono">${fmtNum(elapsedMs || "")}</td>
            <td class="mono">
              <button class="btn btnSmall" type="button" data-key="${jobKey}">${btnLabel}</button>
            </td>
          `;
          tbody.appendChild(tr);

          // 상세행(숨김): 요약행 바로 아래에 추가
          const tr2 = document.createElement("tr");
          tr2.className = "detailRow hidden";
          tr2.setAttribute("data-detail", jobKey);
          tr2.innerHTML = `
            <td colspan="6">
              <div class="small" style="margin-bottom:6px"><b>상세 결과</b> (신호별 청산 결과)</div>
              ${renderDetailTable(details)}
            </td>
          `;
          tbody.appendChild(tr2);

          // 버튼 클릭 시 토글
          const btn = tr.querySelector("button[data-key]");
          if (btn) {
            btn.addEventListener("click", () => {
              const key = btn.getAttribute("data-key");
              const row = tbody.querySelector(`tr[data-detail="${key}"]`);
              if (!row) return;
              row.classList.toggle("hidden");
              btn.textContent = row.classList.contains("hidden") ? "더보기" : "닫기";
            });
          }
        }

        function update(){
          prog.textContent = `${done} / ${total}`;
          okCntEl.textContent = String(ok);
          failCntEl.textContent = String(fail);
        }

        if (btnShowNoSignal) {
          btnShowNoSignal.addEventListener("click", () => {
            if (!noSignalPanel) return;
            noSignalPanel.classList.toggle("hidden");
            refreshNoSignalButtonText();
          });
        }

        statusEl.textContent = "연결 중...";
        const es = new EventSource(`/stream/${jobId}`);
        es.onmessage = (ev) => {
          try {
            const msg = JSON.parse(ev.data);
            if (!msg || !msg.type) return;
            if (msg.type === "start") {
              statusEl.textContent = "실행 중...";
              return;
            }
            if (msg.type === "row") {
              done = msg.done || done;
              ok = msg.ok || ok;
              fail = msg.fail || fail;
              addRow(msg.row);
              update();
              return;
            }
            if (msg.type === "error") {
              statusEl.textContent = "오류";
              return;
            }
            if (msg.type === "done") {
              done = msg.done || done;
              ok = msg.ok || ok;
              fail = msg.fail || fail;
              update();
              statusEl.textContent = "완료";
              es.close();
              return;
            }
          } catch (e) {
            // ignore
          }
        };
        es.onerror = () => {
          statusEl.textContent = "연결 끊김(재시도 중...)";
        };
      })();
    </script>
    {% elif auto_job_id %}
    <section class="card">
      <div class="cardTitle">자동분석 진행 상황 (9 CASE)</div>
      <div class="kv" style="margin-bottom:10px">
        <div><span class="k">AUTO JOB</span><span class="v mono">{{ auto_job_id }}</span></div>
        <div><span class="k">현재 CASE</span><span class="v mono" id="autoCurCase">대기</span></div>
        <div><span class="k">사전 데이터수집</span><span class="v mono" id="autoPreloadProg">0 / 0</span></div>
        <div><span class="k">CASE 진행</span><span class="v mono" id="autoCaseProg">0 / 9</span></div>
        <div><span class="k">총 실행종목수</span><span class="v mono" id="autoTotalStocks">0</span></div>
        <div><span class="k">전체 완료</span><span class="v mono" id="autoDoneStocks">0</span></div>
        <div><span class="k">남은 종목</span><span class="v mono" id="autoRemainStocks">0</span></div>
        <div><span class="k">상태</span><span class="v mono" id="autoStatus">대기</span></div>
      </div>
      <div class="tableWrap" style="margin-bottom:10px">
        <table class="table" style="min-width: 1180px">
          <thead>
            <tr>
              <th>CASE</th>
              <th class="right">진행(완료/총)</th>
              <th class="right">남은 종목</th>
              <th class="right">평균 수익률(%)</th>
              <th class="right">누적 수익률(%)</th>
              <th class="right">수익 종목수</th>
              <th class="right">손실 종목수</th>
              <th class="right">오류 종목수</th>
              <th>상태</th>
            </tr>
          </thead>
          <tbody id="autoProgressBody"></tbody>
        </table>
      </div>
      <div id="autoFinalWrap" class="hidden">
        <div class="cardTitle">자동분석 최종 결과 (9 CASE 요약)</div>
        <div class="tableWrap">
          <table class="table" style="min-width: 1260px">
            <thead>
              <tr>
                <th>CASE</th>
                <th class="right">분석 종목수</th>
                <th class="right">청산 완료 거래수</th>
                <th class="right">평균 수익률(%)</th>
                <th class="right">누적 수익률(단순합, %)</th>
                <th class="right">수익 종목수</th>
                <th class="right">손실 종목수</th>
                <th class="right">오류 종목수</th>
                <th>수익 종목</th>
                <th>손실 종목</th>
                <th>상세</th>
              </tr>
            </thead>
            <tbody id="autoFinalBody"></tbody>
          </table>
        </div>
      </div>
      <div class="note">{{ assumption }}</div>
    </section>
    <script>
      (function(){
        const autoJobId = {{ auto_job_id|tojson }};
        const statusEl = document.getElementById("autoStatus");
        const curCaseEl = document.getElementById("autoCurCase");
        const preloadProgEl = document.getElementById("autoPreloadProg");
        const caseProgEl = document.getElementById("autoCaseProg");
        const totalStocksEl = document.getElementById("autoTotalStocks");
        const doneStocksEl = document.getElementById("autoDoneStocks");
        const remainStocksEl = document.getElementById("autoRemainStocks");
        const progressBody = document.getElementById("autoProgressBody");
        const finalWrap = document.getElementById("autoFinalWrap");
        const finalBody = document.getElementById("autoFinalBody");
        const progressMap = new Map();

        function fmtNum(v){
          const n = Number(v);
          if (!Number.isFinite(n)) return String(v || "");
          return n.toLocaleString("ko-KR");
        }
        function fmtPct(v){
          const n = Number(v);
          if (!Number.isFinite(n)) return "";
          const s = n.toFixed(2);
          return (n > 0 ? "+" : "") + s;
        }
        function esc(s){
          return String(s ?? "").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;");
        }

        function upsertProgressRow(caseId, data){
          progressMap.set(caseId, data);
          const rows = Array.from(progressMap.values()).sort((a,b) => (a.case_index||0) - (b.case_index||0));
          progressBody.innerHTML = rows.map((r) => `
            <tr>
              <td class="mono">${esc(r.case_label || "")}</td>
              <td class="right mono">${fmtNum(r.case_done || 0)} / ${fmtNum(r.case_total || 0)}</td>
              <td class="right mono">${fmtNum(r.case_remaining || 0)}</td>
              <td class="right mono">${fmtPct((r.summary||{}).avg_ret_pct || 0)}</td>
              <td class="right mono">${fmtPct((r.summary||{}).sum_ret_pct || 0)}</td>
              <td class="right mono ok">${fmtNum((r.summary||{}).profit_stock_count || 0)}</td>
              <td class="right mono err">${fmtNum((r.summary||{}).loss_stock_count || 0)}</td>
              <td class="right mono warn">${fmtNum((r.summary||{}).error_stock_count || 0)}</td>
              <td class="mono">${esc(r.status || "진행중")}</td>
            </tr>
          `).join("");
        }

        function renderFinal(autoCases){
          if (!Array.isArray(autoCases) || !autoCases.length) return;
          finalBody.innerHTML = autoCases.map((c) => {
            const s = c.summary || {};
            const cid = c.case_id || "";
            const detailRows = (Array.isArray(c.rows) ? c.rows : []).map((r) => `
              <tr>
                <td class="mono">${esc(r.name || "")}</td>
                <td class="mono">${esc(r.ticker || "-")}</td>
                <td class="right mono">${fmtNum(r.signals_count || 0)}</td>
                <td class="right mono">${fmtNum(r.closed_trades || 0)}</td>
                <td class="right mono">${fmtPct(r.avg_ret_pct)}</td>
                <td class="mono small">${esc(r.note || "")}</td>
              </tr>
            `).join("");
            return `
              <tr>
                <td class="mono">${esc(c.case_label || "")}</td>
                <td class="right mono">${fmtNum(s.analyzed_stocks || 0)}</td>
                <td class="right mono">${fmtNum(s.closed_trades || 0)}</td>
                <td class="right mono">${fmtPct(s.avg_ret_pct || 0)}</td>
                <td class="right mono">${fmtPct(s.sum_ret_pct || 0)}</td>
                <td class="right mono ok">${fmtNum(s.profit_stock_count || 0)}</td>
                <td class="right mono err">${fmtNum(s.loss_stock_count || 0)}</td>
                <td class="right mono warn">${fmtNum(s.error_stock_count || 0)}</td>
                <td class="mono small">${esc(s.profit_names || "-")}</td>
                <td class="mono small">${esc(s.loss_names || "-")}</td>
                <td class="mono"><button class="btn btnSmall autoFinalToggleBtn" type="button" data-case="${esc(cid)}">더보기</button></td>
              </tr>
              <tr class="detailRow hidden" data-case-detail="${esc(cid)}">
                <td colspan="11">
                  <div class="small" style="margin-bottom:6px"><b>${esc(c.case_label || "")}</b> 종목별 상세</div>
                  <div class="tableWrap">
                    <table class="table" style="min-width: 980px">
                      <thead>
                        <tr>
                          <th>종목명</th><th>종목코드</th><th class="right">신호 수</th><th class="right">청산 거래수</th><th class="right">평균 수익률(%)</th><th>비고</th>
                        </tr>
                      </thead>
                      <tbody>${detailRows}</tbody>
                    </table>
                  </div>
                </td>
              </tr>
            `;
          }).join("");
          finalWrap.classList.remove("hidden");
          const btns = finalBody.querySelectorAll(".autoFinalToggleBtn");
          btns.forEach((btn) => {
            btn.addEventListener("click", () => {
              const key = btn.getAttribute("data-case");
              const row = finalBody.querySelector(`tr[data-case-detail="${key}"]`);
              if (!row) return;
              row.classList.toggle("hidden");
              btn.textContent = row.classList.contains("hidden") ? "더보기" : "닫기";
            });
          });
        }

        statusEl.textContent = "연결 중...";
        const es = new EventSource(`/stream-auto/${autoJobId}`);
        es.onmessage = (ev) => {
          try {
            const msg = JSON.parse(ev.data);
            if (!msg || !msg.type) return;
            if (msg.type === "auto_start") {
              statusEl.textContent = "실행 중...";
              totalStocksEl.textContent = fmtNum(msg.overall_total || 0);
              doneStocksEl.textContent = "0";
              remainStocksEl.textContent = fmtNum(msg.overall_total || 0);
              preloadProgEl.textContent = `0 / ${fmtNum(msg.total_stocks || 0)}`;
              curCaseEl.textContent = "데이터 수집 중...";
              return;
            }
            if (msg.type === "auto_preload_progress") {
              preloadProgEl.textContent = `${fmtNum(msg.done || 0)} / ${fmtNum(msg.total || 0)}`;
              return;
            }
            if (msg.type === "auto_case_start") {
              curCaseEl.textContent = msg.case_label || "";
              caseProgEl.textContent = `${msg.case_index || 0} / ${msg.total_cases || 0}`;
              upsertProgressRow(msg.case_id, {
                case_label: msg.case_label,
                case_index: msg.case_index,
                case_done: 0,
                case_total: msg.case_total || 0,
                case_remaining: msg.case_total || 0,
                summary: {},
                status: "진행중",
              });
              return;
            }
            if (msg.type === "auto_case_progress") {
              curCaseEl.textContent = msg.case_label || "";
              caseProgEl.textContent = `${msg.case_index || 0} / ${msg.total_cases || 0}`;
              doneStocksEl.textContent = fmtNum(msg.overall_done || 0);
              remainStocksEl.textContent = fmtNum((msg.overall_total || 0) - (msg.overall_done || 0));
              upsertProgressRow(msg.case_id, {
                case_label: msg.case_label,
                case_index: msg.case_index,
                case_done: msg.case_done || 0,
                case_total: msg.case_total || 0,
                case_remaining: msg.case_remaining || 0,
                summary: msg.summary || {},
                status: "진행중",
              });
              return;
            }
            if (msg.type === "auto_case_done") {
              upsertProgressRow(msg.case_id, {
                case_label: msg.case_label,
                case_index: msg.case_index,
                case_done: progressMap.get(msg.case_id)?.case_total || 0,
                case_total: progressMap.get(msg.case_id)?.case_total || 0,
                case_remaining: 0,
                summary: msg.summary || {},
                status: "완료",
              });
              return;
            }
            if (msg.type === "auto_done") {
              statusEl.textContent = "완료";
              renderFinal(msg.auto_cases || []);
              es.close();
              return;
            }
            if (msg.type === "error") {
              statusEl.textContent = "오류";
              return;
            }
          } catch (e) {}
        };
        es.onerror = () => {
          statusEl.textContent = "연결 끊김(재시도 중...)";
        };
      })();
    </script>
    {% elif auto_cases is not none %}
    <section class="card">
      <div class="cardTitle">자동분석 결과 (9 CASE 요약)</div>
      <div class="small" style="margin-bottom:8px">손절률(5/7/10) x 트레일링(10/7/5) 조합을 순차 실행한 결과입니다.</div>
      <div class="tableWrap">
        <table class="table" style="min-width: 1260px">
          <thead>
            <tr>
              <th>CASE</th>
              <th class="right">분석 종목수</th>
              <th class="right">청산 완료 거래수</th>
              <th class="right">평균 수익률(%)</th>
              <th class="right">누적 수익률(단순합, %)</th>
              <th class="right">수익 종목수</th>
              <th class="right">손실 종목수</th>
              <th class="right">오류 종목수</th>
              <th>수익 종목</th>
              <th>손실 종목</th>
              <th>상세</th>
            </tr>
          </thead>
          <tbody>
          {% for c in auto_cases %}
            <tr>
              <td class="mono">{{ c.case_label }}</td>
              <td class="right mono">{{ c.summary.analyzed_stocks }}</td>
              <td class="right mono">{{ c.summary.closed_trades }}</td>
              <td class="right mono">{{ ("+" if c.summary.avg_ret_pct > 0 else "") + ("%.2f"|format(c.summary.avg_ret_pct)) }}</td>
              <td class="right mono">{{ ("+" if c.summary.sum_ret_pct > 0 else "") + ("%.2f"|format(c.summary.sum_ret_pct)) }}</td>
              <td class="right mono ok">{{ c.summary.profit_stock_count }}</td>
              <td class="right mono err">{{ c.summary.loss_stock_count }}</td>
              <td class="right mono warn">{{ c.summary.error_stock_count }}</td>
              <td class="mono small">{{ c.summary.profit_names if c.summary.profit_names else "-" }}</td>
              <td class="mono small">{{ c.summary.loss_names if c.summary.loss_names else "-" }}</td>
              <td class="mono"><button class="btn btnSmall autoToggleBtn" type="button" data-case="{{ c.case_id }}">더보기</button></td>
            </tr>
            <tr class="detailRow hidden" data-case-detail="{{ c.case_id }}">
              <td colspan="11">
                <div class="small" style="margin-bottom:6px"><b>{{ c.case_label }}</b> 종목별 상세</div>
                <div class="tableWrap">
                  <table class="table" style="min-width: 980px">
                    <thead>
                      <tr>
                        <th>종목명</th>
                        <th>종목코드</th>
                        <th class="right">신호 수</th>
                        <th class="right">청산 거래수</th>
                        <th class="right">평균 수익률(%)</th>
                        <th>비고</th>
                      </tr>
                    </thead>
                    <tbody>
                    {% for r in c.rows %}
                      <tr>
                        <td class="mono">{{ r.name }}</td>
                        <td class="mono">{{ r.ticker or "-" }}</td>
                        <td class="right mono">{{ r.signals_count }}</td>
                        <td class="right mono">{{ r.closed_trades }}</td>
                        <td class="right mono">{{ ("+" if r.avg_ret_pct > 0 else "") + ("%.2f"|format(r.avg_ret_pct)) if r.avg_ret_pct is not none else "" }}</td>
                        <td class="mono small">{{ r.note }}</td>
                      </tr>
                    {% endfor %}
                    </tbody>
                  </table>
                </div>
              </td>
            </tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
      <div class="note">{{ assumption }}</div>
    </section>
    <script>
      (function(){
        const btns = document.querySelectorAll(".autoToggleBtn");
        btns.forEach((btn) => {
          btn.addEventListener("click", () => {
            const key = btn.getAttribute("data-case");
            const row = document.querySelector(`tr[data-case-detail="${key}"]`);
            if (!row) return;
            row.classList.toggle("hidden");
            btn.textContent = row.classList.contains("hidden") ? "더보기" : "닫기";
          });
        });
      })();
    </script>
    {% elif results is not none %}
    <section class="card">
      <div class="cardTitle">결과(종목별)</div>
      <div class="tableWrap">
        <table class="table" style="min-width: 1180px">
          <thead>
            <tr>
              <th>입력</th>
              <th>종목코드</th>
              <th class="right">일치 신호 개수</th>
              <th>최근 신호일</th>
              <th>매수일</th>
              <th class="right">매수가(시가)</th>
              <th>청산유형</th>
              <th>청산조건발생일</th>
              <th>청산일</th>
              <th class="right">청산가(시가)</th>
              <th class="right">수익률(%)</th>
              <th>비고</th>
            </tr>
          </thead>
          <tbody>
          {% for r in results %}
            <tr>
              <td class="mono">{{ r.display }}</td>
              <td class="mono">{{ r.ticker or "-" }}</td>
              <td class="right mono">{{ r.signals_count }}</td>
              <td class="mono">{{ r.latest.signal_dt if r.latest else "" }}</td>
              <td class="mono">{{ r.latest.buy_dt if r.latest else "" }}</td>
              <td class="right mono">{{ "{:,}".format(r.latest.buy_open) if r.latest else "" }}</td>
              <td class="mono">{{ r.latest.exit_type if r.latest else "" }}</td>
              <td class="mono">{{ r.latest.trigger_dt if r.latest else "" }}</td>
              <td class="mono">{{ r.latest.sell_dt if r.latest else "" }}</td>
              <td class="right mono">{{ "{:,}".format(r.latest.sell_open) if (r.latest and r.latest.sell_open) else "" }}</td>
              <td class="right mono">{{ ("+" if (r.latest and r.latest.ret_pct is not none and r.latest.ret_pct>0) else "") + ("%.2f"|format(r.latest.ret_pct)) if (r.latest and r.latest.ret_pct is not none) else "" }}</td>
              <td class="mono small">{{ r.note }}</td>
            </tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
      <div class="note">{{ assumption }}</div>
    </section>
    {% endif %}
  </div>
</body>
</html>
"""


@app.get("/")
def home():
	return render_template_string(
		HTML,
		base=request.host_url.rstrip("/"),
		results=None,
		auto_cases=None,
		auto_job_id=None,
		job_id=None,
		total=0,
		days=90,
		stop_loss_pct=DEFAULT_STOP_LOSS_PCT,
		trailing_drop_pct=DEFAULT_TRAILING_DROP_PCT,
		assumption=ASSUMPTION_TEXT,
	)


def _process_one_candidate(
	token: str,
	q: str,
	days: int,
	job_id: str,
	idx: int,
	stop_loss_pct: int = DEFAULT_STOP_LOSS_PCT,
	trailing_drop_pct: int = DEFAULT_TRAILING_DROP_PCT,
) -> tuple[dict, bool]:
	start = time.perf_counter()
	key = f"{job_id}:{idx}"
	ticker, disp = _resolve_to_ticker(q)
	if not ticker:
		elapsed_ms = int((time.perf_counter() - start) * 1000)
		return ({
			"key": key,
			"input": q,
			"name": q,
			"display": disp,
			"ticker": None,
			"signals_count": 0,
			"signal_dates": [],
			"rows": [],
			"note": "종목 해석 실패",
			"elapsed_ms": elapsed_ms,
		}, False)

	try:
		res = simulate_one(
			token,
			ticker,
			days=days,
			stop_loss_pct=int(stop_loss_pct),
			trailing_drop_pct=int(trailing_drop_pct),
		)
		# 입력이 코드만 온 경우 KRX 캐시의 종목명을 우선 사용
		name = q
		if re.fullmatch(r"\d{6}", str(q).strip()):
			try:
				name = (getattr(core, "_krx_cache", {}).get("name_by_code") or {}).get(ticker, q)
			except Exception:
				name = q
		elapsed_ms = int((time.perf_counter() - start) * 1000)
		return ({
			"key": key,
			"input": q,
			"name": name,
			"display": disp,
			"ticker": ticker,
			"signals_count": int(res.get("signals_count") or 0),
			"signal_dates": res.get("matched_signal_dates") or [],
			"rows": res.get("rows") or [],
			"note": "",
			"elapsed_ms": elapsed_ms,
		}, True)
	except Exception as e:
		elapsed_ms = int((time.perf_counter() - start) * 1000)
		return ({
			"key": key,
			"input": q,
			"name": q,
			"display": disp,
			"ticker": ticker,
			"signals_count": 0,
			"signal_dates": [],
			"rows": [],
			"note": f"오류: {e}",
			"elapsed_ms": elapsed_ms,
		}, False)


def _prepare_one_candidate_for_auto(token: str, q: str, days: int) -> dict[str, Any]:
	ticker, disp = _resolve_to_ticker(q)
	base = {
		"input": q,
		"name": q,
		"display": disp,
		"ticker": ticker,
		"prepared": None,
		"note": "",
	}
	if not ticker:
		base["note"] = "종목 해석 실패"
		return base

	name = q
	if re.fullmatch(r"\d{6}", str(q).strip()):
		try:
			name = (getattr(core, "_krx_cache", {}).get("name_by_code") or {}).get(ticker, q)
		except Exception:
			name = q
	base["name"] = name

	try:
		base["prepared"] = _prepare_stock_data(token, ticker, days=days)
	except Exception as e:
		base["note"] = f"오류: {e}"
	return base


def _build_case_calc_row(prepared_row: dict, stop_loss_pct: int, trailing_drop_pct: int) -> dict[str, Any]:
	name = prepared_row.get("name") or prepared_row.get("input") or prepared_row.get("display") or ""
	ticker = prepared_row.get("ticker")
	note = prepared_row.get("note") or ""
	prepared = prepared_row.get("prepared")
	if not prepared or not ticker:
		return {
			"name": name,
			"ticker": ticker,
			"signals_count": 0,
			"closed_trades": 0,
			"ret_sum_pct": 0.0,
			"ret_count": 0,
			"avg_ret_pct": None,
			"note": note or "데이터 없음",
		}

	sim_rows = _simulate_trailing_stop(
		prepared.get("matched_signal_dates") or [],
		prepared.get("ohlc_map") or {},
		stop_loss_pct=int(stop_loss_pct),
		trailing_drop_pct=int(trailing_drop_pct),
	)
	row_like = {
		"name": name,
		"input": prepared_row.get("input"),
		"display": prepared_row.get("display"),
		"ticker": ticker,
		"signals_count": int(prepared.get("signals_count") or 0),
		"rows": [r.__dict__ for r in sim_rows],
		"note": note,
	}
	return _build_case_row_from_result(row_like)


def _build_case_summary(rows: list[dict]) -> dict[str, Any]:
	analyzed_stocks = 0
	closed_trades = 0
	ret_sum = 0.0
	ret_count = 0
	error_stocks = 0
	profit_names: list[str] = []
	loss_names: list[str] = []

	for r in rows:
		if r.get("ticker"):
			analyzed_stocks += 1
		if str(r.get("note") or "").strip():
			error_stocks += 1
		avg_ret = r.get("avg_ret_pct")
		if isinstance(avg_ret, (int, float)):
			if avg_ret > 0:
				profit_names.append(f"{r.get('name')}({r.get('ticker')})")
			elif avg_ret < 0:
				loss_names.append(f"{r.get('name')}({r.get('ticker')})")
		closed_trades += int(r.get("closed_trades") or 0)
		ret_sum += float(r.get("ret_sum_pct") or 0.0)
		ret_count += int(r.get("ret_count") or 0)

	avg_ret_pct = (ret_sum / ret_count) if ret_count > 0 else 0.0
	return {
		"analyzed_stocks": int(analyzed_stocks),
		"closed_trades": int(closed_trades),
		"avg_ret_pct": round(avg_ret_pct, 2),
		"sum_ret_pct": round(ret_sum, 2),
		"profit_stock_count": len(profit_names),
		"loss_stock_count": len(loss_names),
		"error_stock_count": int(error_stocks),
		"profit_names": ", ".join(profit_names),
		"loss_names": ", ".join(loss_names),
	}


def _build_case_row_from_result(row: dict) -> dict[str, Any]:
	details = row.get("rows") or []
	closed_rets = [
		float(d.get("ret_pct"))
		for d in details
		if isinstance(d, dict) and isinstance(d.get("ret_pct"), (int, float))
	]
	ret_sum_pct = sum(closed_rets)
	ret_count = len(closed_rets)
	avg_ret_pct = (ret_sum_pct / ret_count) if ret_count > 0 else None
	return {
		"name": row.get("name") or row.get("input") or row.get("display") or "",
		"ticker": row.get("ticker"),
		"signals_count": int(row.get("signals_count") or 0),
		"closed_trades": int(ret_count),
		"ret_sum_pct": float(ret_sum_pct),
		"ret_count": int(ret_count),
		"avg_ret_pct": round(float(avg_ret_pct), 2) if avg_ret_pct is not None else None,
		"note": row.get("note") or "",
	}


def _run_job(job: _Job):
	done = 0
	ok = 0
	fail = 0
	_emit(job, {"type": "start", "job_id": job.id, "total": job.total, "workers": SIMULDEMAND_WORKERS})
	try:
		token = core.get_token(APP_KEY, APP_SECRET)
	except Exception as e:
		job.error = str(e)
		_emit(job, {"type": "error", "job_id": job.id, "error": str(e)})
		job.done = True
		_emit(job, {"type": "done", "job_id": job.id, "done": done, "ok": ok, "fail": fail, "total": job.total})
		return

	with ThreadPoolExecutor(max_workers=SIMULDEMAND_WORKERS) as ex:
		fut_map = {
			ex.submit(
				_process_one_candidate,
				token,
				q,
				job.days,
				job.id,
				idx,
				job.stop_loss_pct,
				job.trailing_drop_pct,
			): (idx, q)
			for idx, q in enumerate(job.cands, start=1)
		}
		for fut in as_completed(fut_map):
			idx, q = fut_map[fut]
			try:
				row, success = fut.result()
			except Exception as e:
				row = {
					"key": f"{job.id}:{idx}",
					"input": q,
					"name": q,
					"display": q,
					"ticker": None,
					"signals_count": 0,
					"signal_dates": [],
					"rows": [],
					"note": f"오류: {e}",
					"elapsed_ms": 0,
				}
				success = False

			done += 1
			if success:
				ok += 1
			else:
				fail += 1

			_emit(job, {"type": "row", "job_id": job.id, "done": done, "ok": ok, "fail": fail, "total": job.total, "row": row})
			if SIMULDEMAND_ITEM_SLEEP_SEC > 0:
				time.sleep(SIMULDEMAND_ITEM_SLEEP_SEC)

	job.done = True
	_emit(job, {"type": "done", "job_id": job.id, "done": done, "ok": ok, "fail": fail, "total": job.total})


def _run_auto_job(job: _AutoJob):
	total_cases = len(AUTO_STOP_CASES) * len(AUTO_TRAILING_CASES)
	overall_total = total_cases * max(0, int(job.total))
	overall_done = 0
	try:
		token = core.get_token(APP_KEY, APP_SECRET)
	except Exception as e:
		job.error = str(e)
		_emit(job, {"type": "error", "job_id": job.id, "error": str(e)})
		job.done = True
		_emit(job, {"type": "auto_done", "job_id": job.id, "auto_cases": [], "total_cases": total_cases})
		return

	auto_cases: list[dict[str, Any]] = []
	case_idx = 0
	_emit(
		job,
		{
			"type": "auto_start",
			"job_id": job.id,
			"total_cases": total_cases,
			"total_stocks": int(job.total),
			"overall_total": int(overall_total),
		},
	)

	# 1) 종목 데이터는 한 번만 조회하고, 케이스별 계산에서 재사용합니다.
	prepared_rows: list[dict[str, Any]] = []
	preload_done = 0
	for q in job.cands:
		prepared = _prepare_one_candidate_for_auto(token=token, q=q, days=job.days)
		prepared_rows.append(prepared)
		preload_done += 1
		_emit(
			job,
			{
				"type": "auto_preload_progress",
				"job_id": job.id,
				"done": preload_done,
				"total": int(job.total),
			},
		)
		if SIMULDEMAND_API_ITEM_SLEEP_SEC > 0:
			time.sleep(SIMULDEMAND_API_ITEM_SLEEP_SEC)

	for stop_loss_pct in AUTO_STOP_CASES:
		for trailing_drop_pct in AUTO_TRAILING_CASES:
			case_idx += 1
			case_id = f"sl{stop_loss_pct}_ts{trailing_drop_pct}"
			case_label = f"SL {_fmt_pct_int(stop_loss_pct)} / TS {_fmt_pct_int(trailing_drop_pct)}"
			case_rows: list[dict[str, Any]] = []
			case_total = int(job.total)
			case_done = 0
			_emit(
				job,
				{
					"type": "auto_case_start",
					"job_id": job.id,
					"case_id": case_id,
					"case_label": case_label,
					"case_index": case_idx,
					"total_cases": total_cases,
					"case_total": case_total,
				},
			)

			for prepared in prepared_rows:
				case_rows.append(
					_build_case_calc_row(
						prepared_row=prepared,
						stop_loss_pct=stop_loss_pct,
						trailing_drop_pct=trailing_drop_pct,
					)
				)
				case_done += 1
				overall_done += 1
				summary = _build_case_summary(case_rows)
				_emit(
					job,
					{
						"type": "auto_case_progress",
						"job_id": job.id,
						"case_id": case_id,
						"case_label": case_label,
						"case_index": case_idx,
						"total_cases": total_cases,
						"case_done": case_done,
						"case_remaining": max(0, case_total - case_done),
						"case_total": case_total,
						"overall_done": overall_done,
						"overall_total": overall_total,
						"summary": summary,
					},
				)

			final_summary = _build_case_summary(case_rows)
			case_payload = {
				"case_id": case_id,
				"case_label": case_label,
				"stop_loss_pct": stop_loss_pct,
				"trailing_drop_pct": trailing_drop_pct,
				"summary": final_summary,
				"rows": case_rows,
			}
			auto_cases.append(case_payload)
			_emit(
				job,
				{
					"type": "auto_case_done",
					"job_id": job.id,
					"case_id": case_id,
					"case_label": case_label,
					"case_index": case_idx,
					"total_cases": total_cases,
					"summary": final_summary,
				},
			)

	job.done = True
	_emit(
		job,
		{
			"type": "auto_done",
			"job_id": job.id,
			"auto_cases": auto_cases,
			"total_cases": total_cases,
		},
	)


@app.get("/stream/<job_id>")
def stream(job_id: str):
	with _JOBS_LOCK:
		job = _JOBS.get(job_id)
	if not job:
		return Response("not found", status=404)

	@stream_with_context
	def gen():
		# 이미 발생한 이벤트는 먼저 리플레이(새로고침/재접속 대응)
		for ev in job.events:
			yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"

		# 이후는 실시간 큐 소비
		while True:
			if job.done and job.q.empty():
				break
			try:
				ev = job.q.get(timeout=1.0)
				yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
			except queue.Empty:
				# keep-alive (프록시/브라우저 타임아웃 방지)
				yield ": ping\n\n"
				continue

	return Response(gen(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/stream-auto/<job_id>")
def stream_auto(job_id: str):
	with _AUTO_JOBS_LOCK:
		job = _AUTO_JOBS.get(job_id)
	if not job:
		return Response("not found", status=404)

	@stream_with_context
	def gen():
		for ev in job.events:
			yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"

		while True:
			if job.done and job.q.empty():
				break
			try:
				ev = job.q.get(timeout=1.0)
				yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
			except queue.Empty:
				yield ": ping\n\n"
				continue

	return Response(gen(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/simulate")
def simulate():
	stop_loss_pct = DEFAULT_STOP_LOSS_PCT
	trailing_drop_pct = DEFAULT_TRAILING_DROP_PCT
	try:
		stop_loss_pct = _clamp_stop_loss_pct(int((request.form.get("stop_loss_pct") or str(DEFAULT_STOP_LOSS_PCT)).strip()))
	except Exception:
		stop_loss_pct = DEFAULT_STOP_LOSS_PCT
	try:
		trailing_drop_pct = _clamp_trailing_drop_pct(int((request.form.get("trailing_drop_pct") or str(DEFAULT_TRAILING_DROP_PCT)).strip()))
	except Exception:
		trailing_drop_pct = DEFAULT_TRAILING_DROP_PCT

	if not APP_KEY or not APP_SECRET:
		return render_template_string(
			HTML,
			base=request.host_url.rstrip("/"),
			results=[],
			auto_cases=None,
			auto_job_id=None,
			days=90,
			stop_loss_pct=stop_loss_pct,
			trailing_drop_pct=trailing_drop_pct,
			assumption="서버 설정 오류: APP_KEY/APP_SECRET(.env) 설정이 필요합니다.",
		), 500

	file = request.files.get("file")
	if not file:
		return render_template_string(
			HTML,
			base=request.host_url.rstrip("/"),
			results=[],
			auto_cases=None,
			auto_job_id=None,
			days=90,
			stop_loss_pct=stop_loss_pct,
			trailing_drop_pct=trailing_drop_pct,
			assumption="파일이 없습니다.",
		), 400
	try:
		days = int((request.form.get("days") or "90").strip())
	except Exception:
		days = 90
	days = max(10, min(365, days))

	raw = file.read()
	try:
		text = raw.decode("utf-8-sig")
	except Exception:
		text = raw.decode("utf-8", errors="ignore")

	cands = _extract_candidates_from_md(text)
	cands = cands[:MAX_CANDIDATES]  # 과도한 호출 방지
	job_id = uuid.uuid4().hex[:12]
	job = _Job(
		id=job_id,
		created_at=time.time(),
		days=days,
		stop_loss_pct=stop_loss_pct,
		trailing_drop_pct=trailing_drop_pct,
		cands=cands,
		total=len(cands),
		events=[],
		q=queue.Queue(),
	)
	with _JOBS_LOCK:
		_JOBS[job_id] = job

	th = threading.Thread(target=_run_job, args=(job,), daemon=True)
	th.start()

	# 즉시 화면 반환(이후 SSE로 한 줄씩 갱신)
	return render_template_string(
		HTML,
		base=request.host_url.rstrip("/"),
		results=None,
		auto_cases=None,
		auto_job_id=None,
		job_id=job_id,
		total=len(cands),
		days=days,
		stop_loss_pct=stop_loss_pct,
		trailing_drop_pct=trailing_drop_pct,
		assumption=_build_assumption_text(stop_loss_pct, trailing_drop_pct),
	)


@app.post("/simulate-auto")
def simulate_auto():
	if not APP_KEY or not APP_SECRET:
		return render_template_string(
			HTML,
			base=request.host_url.rstrip("/"),
			results=[],
			auto_cases=None,
			auto_job_id=None,
			days=90,
			stop_loss_pct=DEFAULT_STOP_LOSS_PCT,
			trailing_drop_pct=DEFAULT_TRAILING_DROP_PCT,
			assumption="서버 설정 오류: APP_KEY/APP_SECRET(.env) 설정이 필요합니다.",
		), 500

	file = request.files.get("file")
	if not file:
		return render_template_string(
			HTML,
			base=request.host_url.rstrip("/"),
			results=[],
			auto_cases=None,
			auto_job_id=None,
			days=90,
			stop_loss_pct=DEFAULT_STOP_LOSS_PCT,
			trailing_drop_pct=DEFAULT_TRAILING_DROP_PCT,
			assumption="파일이 없습니다.",
		), 400

	try:
		days = int((request.form.get("days") or "90").strip())
	except Exception:
		days = 90
	days = max(10, min(365, days))

	raw = file.read()
	try:
		text = raw.decode("utf-8-sig")
	except Exception:
		text = raw.decode("utf-8", errors="ignore")

	cands = _extract_candidates_from_md(text)
	cands = cands[:MAX_CANDIDATES]

	job_id = uuid.uuid4().hex[:12]
	job = _AutoJob(
		id=job_id,
		created_at=time.time(),
		days=days,
		cands=cands,
		total=len(cands),
		events=[],
		q=queue.Queue(),
	)
	with _AUTO_JOBS_LOCK:
		_AUTO_JOBS[job_id] = job

	th = threading.Thread(target=_run_auto_job, args=(job,), daemon=True)
	th.start()

	return render_template_string(
		HTML,
		base=request.host_url.rstrip("/"),
		results=None,
		auto_cases=None,
		auto_job_id=job_id,
		job_id=None,
		total=0,
		days=days,
		stop_loss_pct=DEFAULT_STOP_LOSS_PCT,
		trailing_drop_pct=DEFAULT_TRAILING_DROP_PCT,
		assumption="자동분석 고정 케이스: 손절률 5/7/10, 트레일링 10/7/5 조합 (실시간 진행상황 표시)",
	)


@app.post("/api/simulate")
def api_simulate():
	"""
	프론트 없이도 쓸 수 있게 JSON API 제공.
	"""
	if not APP_KEY or not APP_SECRET:
		return jsonify({"ok": False, "error": "ServerMisconfigured", "detail": "Set APP_KEY/APP_SECRET in .env"}), 500
	payload = request.get_json(force=True) or {}
	items = payload.get("items") or []
	try:
		days = int(payload.get("days") or 90)
	except Exception:
		days = 90
	days = max(10, min(365, days))
	try:
		stop_loss_pct = _clamp_stop_loss_pct(int(payload.get("stop_loss_pct") or DEFAULT_STOP_LOSS_PCT))
	except Exception:
		stop_loss_pct = DEFAULT_STOP_LOSS_PCT
	try:
		trailing_drop_pct = _clamp_trailing_drop_pct(int(payload.get("trailing_drop_pct") or DEFAULT_TRAILING_DROP_PCT))
	except Exception:
		trailing_drop_pct = DEFAULT_TRAILING_DROP_PCT

	if not isinstance(items, list):
		return jsonify({"ok": False, "error": "BadRequest", "detail": "items must be list"}), 400

	token = core.get_token(APP_KEY, APP_SECRET)
	out = []
	for q in items[:MAX_CANDIDATES]:
		start = time.perf_counter()
		ticker, _ = _resolve_to_ticker(str(q))
		if not ticker:
			out.append({"input": q, "ticker": None, "error": "ResolveFailed", "elapsed_ms": int((time.perf_counter() - start) * 1000)})
			continue
		try:
			out.append(
				{
					"input": q,
					"ticker": ticker,
					"result": simulate_one(
						token,
						ticker,
						days=days,
						stop_loss_pct=stop_loss_pct,
						trailing_drop_pct=trailing_drop_pct,
					),
					"elapsed_ms": int((time.perf_counter() - start) * 1000),
				}
			)
		except Exception as e:
			out.append({"input": q, "ticker": ticker, "error": str(e), "elapsed_ms": int((time.perf_counter() - start) * 1000)})
		if SIMULDEMAND_API_ITEM_SLEEP_SEC > 0:
			time.sleep(SIMULDEMAND_API_ITEM_SLEEP_SEC)
	return jsonify(
		{
			"ok": True,
			"days": days,
			"stop_loss_pct": stop_loss_pct,
			"trailing_drop_pct": trailing_drop_pct,
			"count": len(out),
			"results": out,
		}
	)


if __name__ == "__main__":
	# 기본 포트(기존 앱과 충돌 방지)
	app.run(host="0.0.0.0", port=7790, debug=False)

