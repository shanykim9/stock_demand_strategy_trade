from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Any

import buysell as bs
import stgdemand as stg
from flask import request


app = stg.app
TZ = stg.TZ

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
BS_CONFIG_PATH = os.path.join(DATA_DIR, "bsdemand_config.json")
BS_PLAN_PATH = os.path.join(DATA_DIR, "bsdemand_buy_plan.json")
BS_LOG_PATH = os.path.join(DATA_DIR, "bsdemand_buy_log.jsonl")
os.makedirs(DATA_DIR, exist_ok=True)

_BS_CFG_LOCK = threading.Lock()
_BS_PLAN_LOCK = threading.Lock()
_BS_LOG_LOCK = threading.Lock()
_BS_RT_LOCK = threading.Lock()
_BS_RUNTIME: dict[str, Any] = {
	"thread_started_at": "",
	"last_loop_at": "",
	"last_ok_at": "",
	"last_result": "",
	"last_error": "",
	"last_error_at": "",
	"loop_count": 0,
}


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


BS_DEFAULT_BUDGET = _env_int("BSDEMAND_BUDGET_PER_STOCK", 500000, min_value=1, max_value=2000000000)
BS_ENABLED_DEFAULT = (os.getenv("BSDEMAND_AUTO_BUY_ENABLED") or "1").strip() not in ("0", "false", "False")
BS_BUY_START_HHMM = (os.getenv("BSDEMAND_BUY_START_HHMM") or "09:00").strip()
BS_RETRY_COUNT = _env_int("BSDEMAND_RETRY_COUNT", 2, min_value=0, max_value=10)  # 실패 시 재시도 횟수
BS_LOOP_SLEEP_SEC = float(os.getenv("BSDEMAND_LOOP_SLEEP_SEC") or "7")
BS_TRAILING_DROP_PCT = 10.0
BS_STOP_LOSS_PCT = 7.0

# 토큰 캐시: 잦은 /buy-status 호출 시 매번 신규 발급 방지 (5분 유효)
_TOKEN_CACHE: dict[str, Any] = {"token": "", "fetched_at": 0.0}
_TOKEN_CACHE_TTL = 270.0  # 270초(4.5분) 마다 갱신


def _get_cached_token() -> str:
	now_ts = time.time()
	if _TOKEN_CACHE["token"] and (now_ts - _TOKEN_CACHE["fetched_at"]) < _TOKEN_CACHE_TTL:
		return str(_TOKEN_CACHE["token"])
	token = bs.get_token(bs.APP_KEY, bs.APP_SECRET)
	_TOKEN_CACHE["token"] = token
	_TOKEN_CACHE["fetched_at"] = now_ts
	return token


def _parse_hhmm(v: str) -> tuple[int, int]:
	s = (v or "").strip()
	if not re.fullmatch(r"\d{2}:\d{2}", s):
		return (9, 0)
	h, m = s.split(":")
	return (max(0, min(23, int(h))), max(0, min(59, int(m))))


def _now_str() -> str:
	return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")


def _read_json(path: str) -> dict[str, Any]:
	try:
		with open(path, "r", encoding="utf-8") as f:
			obj = json.load(f)
		return obj if isinstance(obj, dict) else {}
	except Exception:
		return {}


def _write_json(path: str, obj: dict[str, Any]):
	tmp = path + ".tmp"
	with open(tmp, "w", encoding="utf-8") as f:
		json.dump(obj, f, ensure_ascii=False, indent=2)
		f.write("\n")
	os.replace(tmp, path)


def _load_bs_config() -> dict[str, Any]:
	with _BS_CFG_LOCK:
		obj = _read_json(BS_CONFIG_PATH)
		cfg = {
			"enabled": bool(obj.get("enabled", BS_ENABLED_DEFAULT)),
			"budget_per_stock": _env_int("BSDEMAND_BUDGET_PER_STOCK", int(obj.get("budget_per_stock", BS_DEFAULT_BUDGET)), 1, 2000000000),
			"buy_start_hhmm": (obj.get("buy_start_hhmm") or BS_BUY_START_HHMM),
			"updated_at": str(obj.get("updated_at") or ""),
		}
		h, m = _parse_hhmm(cfg["buy_start_hhmm"])
		cfg["buy_start_hhmm"] = f"{h:02d}:{m:02d}"
		return cfg


def _save_bs_config(cfg: dict[str, Any]):
	with _BS_CFG_LOCK:
		h, m = _parse_hhmm(str(cfg.get("buy_start_hhmm") or BS_BUY_START_HHMM))
		obj = {
			"enabled": bool(cfg.get("enabled", True)),
			"budget_per_stock": _env_int("BSDEMAND_BUDGET_PER_STOCK", int(cfg.get("budget_per_stock") or BS_DEFAULT_BUDGET), 1, 2000000000),
			"buy_start_hhmm": f"{h:02d}:{m:02d}",
			"updated_at": _now_str(),
		}
		_write_json(BS_CONFIG_PATH, obj)


def _load_buy_plan() -> dict[str, Any]:
	with _BS_PLAN_LOCK:
		obj = _read_json(BS_PLAN_PATH)
		plans = obj.get("plans")
		if not isinstance(plans, list):
			plans = []
		return {"plans": [p for p in plans if isinstance(p, dict)]}


def _save_buy_plan(plan_obj: dict[str, Any]):
	with _BS_PLAN_LOCK:
		out = {"plans": [p for p in (plan_obj.get("plans") or []) if isinstance(p, dict)]}
		_write_json(BS_PLAN_PATH, out)


def _append_buy_log(ev: dict[str, Any]):
	with _BS_LOG_LOCK:
		ev2 = dict(ev)
		ev2.setdefault("ts", _now_str())
		with open(BS_LOG_PATH, "a", encoding="utf-8") as f:
			f.write(json.dumps(ev2, ensure_ascii=False) + "\n")


def _latest_buy_logs(n=20) -> list[dict[str, Any]]:
	try:
		with open(BS_LOG_PATH, "r", encoding="utf-8") as f:
			lines = [ln.strip() for ln in f.readlines() if ln.strip()]
		out: list[dict[str, Any]] = []
		for ln in lines[-n:]:
			try:
				obj = json.loads(ln)
				if isinstance(obj, dict):
					out.append(obj)
			except Exception:
				continue
		return out
	except Exception:
		return []


def _market_wait_message() -> str:
	now = datetime.now(TZ)
	h, m = _parse_hhmm(_load_bs_config().get("buy_start_hhmm", "09:00"))
	start_t = now.replace(hour=h, minute=m, second=0, microsecond=0)
	if now.weekday() >= 5:
		return "휴장일(주말): 장 시작 대기"
	if now < start_t:
		return f"장 시작 대기중 ({start_t.strftime('%H:%M')} 이후 매수 시도)"
	return f"매수 가능 시간대 ({start_t.strftime('%H:%M')} 이후)"


def _next_weekday(dt_s: str) -> str:
	d = datetime.strptime(dt_s, "%Y-%m-%d")
	nd = d + timedelta(days=1)
	while nd.weekday() >= 5:
		nd += timedelta(days=1)
	return nd.strftime("%Y-%m-%d")


def _prev_weekday(dt_s: str) -> str:
	d = datetime.strptime(dt_s, "%Y-%m-%d")
	pd = d - timedelta(days=1)
	while pd.weekday() >= 5:
		pd -= timedelta(days=1)
	return pd.strftime("%Y-%m-%d")


def _upsert_buy_plan_from_job(job: Any):
	# job.events의 row 이벤트에서 "오늘 신호" 또는 "직전 거래일 신호(장 시작 전 분석 시)" 종목을 추출
	# - 종목당 가장 최근 신호 1개 기준
	# - 오늘 신호: 다음 거래일 시가 매수
	# - 직전 거래일 신호 + 장 시작 전: 오늘 시가 매수
	today = datetime.now(TZ).strftime("%Y-%m-%d")
	prev_trade_day = _prev_weekday(today)
	now = datetime.now(TZ)
	h, m = _parse_hhmm(_load_bs_config().get("buy_start_hhmm", "09:00"))
	buy_start_today = now.replace(hour=h, minute=m, second=0, microsecond=0)
	before_buy_start = (now < buy_start_today)
	next_buy_dt = _next_weekday(today)
	by_ticker: dict[str, dict[str, Any]] = {}
	for ev in (getattr(job, "events", []) or []):
		if not isinstance(ev, dict) or ev.get("type") != "row":
			continue
		row = ev.get("row") or {}
		if not isinstance(row, dict):
			continue
		ticker = str(row.get("ticker") or "").strip()
		if not re.fullmatch(r"\d{6}", ticker):
			continue
		sig_cnt = int(row.get("signals_count") or 0)
		if sig_cnt <= 0:
			continue
		sig_dates = [str(x).strip() for x in (row.get("signal_dates") or []) if str(x).strip()]
		if not sig_dates:
			continue
		latest_sig = sorted(sig_dates)[-1]
		if latest_sig == today:
			buy_dt = next_buy_dt
		elif latest_sig == prev_trade_day and before_buy_start and now.weekday() < 5:
			buy_dt = today
		else:
			continue
		prev = by_ticker.get(ticker)
		if (prev is None) or (latest_sig > str(prev.get("signal_dt") or "")):
			by_ticker[ticker] = {
				"ticker": ticker,
				"name": str(row.get("name") or row.get("input") or ticker),
				"signal_dt": latest_sig,
				"buy_dt": buy_dt,
				"source_job_id": str(getattr(job, "id", "")),
			}

	plan = _load_buy_plan()
	plans = [dict(p) for p in (plan.get("plans") or []) if isinstance(p, dict)]
	# 오늘/직전거래일 신호 기반 pending plan은 최신 분석 결과로 교체
	plans = [
		p for p in plans
		if not (
			str(p.get("status") or "") == "pending"
			and str(p.get("signal_dt") or "") in (today, prev_trade_day)
		)
	]

	if not by_ticker:
		plan["plans"] = plans
		_save_buy_plan(plan)
		_append_buy_log({"type": "plan_none_today", "message": "일치하는 종목이 없어서 매수를 하지 않겠습니다", "job_id": str(getattr(job, "id", "")), "date": today})
		return

	idx: dict[tuple[str, str], int] = {}
	for i, p in enumerate(plans):
		key = (str(p.get("ticker") or ""), str(p.get("buy_dt") or ""))
		idx[key] = i

	for _, v in by_ticker.items():
		key = (v["ticker"], v["buy_dt"])
		row = {
			"ticker": v["ticker"],
			"name": v["name"],
			"signal_dt": v["signal_dt"],
			"buy_dt": v["buy_dt"],
			"status": "pending",
			"buy_budget": 0,
			"attempts": 0,
			"last_error": "",
			"order_no": "",
			"buy_price": 0,
			"buy_qty": 0,
			"pnl_pct": None,
			"tp_mode": "",
			"tp_price": 0,
			"tp_rate": None,
			"sl_mode": "",
			"sl_price": 0,
			"sl_rate": None,
			"sell_price": 0,
			"sell_qty": 0,
			"sell_reason": "",
			"sell_order_no": "",
			"peak_high": 0,
			"trailing_trigger_dt": "",
			"stop_trigger_dt": "",
			"planned_sell_dt": "",
			"trigger_reason": "",
			"updated_at": _now_str(),
			"source_job_id": v["source_job_id"],
		}
		if key in idx:
			i = idx[key]
			old = dict(plans[i] or {})
			# 이미 매수 완료건은 유지, 미완료건은 최신 신호로 갱신
			if str(old.get("status") or "") in ("bought",):
				continue
			for key in ("buy_budget", "tp_mode", "tp_rate", "tp_price", "sl_mode", "sl_rate", "sl_price"):
				if key in old:
					row[key] = old.get(key)
			old.update(row)
			plans[i] = old
		else:
			plans.append(row)

	# 최근 데이터 위주 유지
	plans = sorted(plans, key=lambda x: (str(x.get("buy_dt") or ""), str(x.get("ticker") or "")), reverse=True)[:5000]
	plan["plans"] = plans
	_save_buy_plan(plan)
	_append_buy_log(
		{
			"type": "plan_upsert_today",
			"message": f"오늘/직전거래일 신호 {len(by_ticker)}개 종목을 매수 계획으로 저장했습니다",
			"added_or_updated": len(by_ticker),
			"buy_dt": sorted(list({str(v.get('buy_dt') or '') for v in by_ticker.values()})),
			"date": today,
		}
	)


_ACCT_DEBUG: dict[str, Any] = {
	"last_raw": None, "last_error": "", "last_tried_bodies": [],
	"last_at": "", "api_call_ok": False, "tried_api_ids": [],
}


def _safe_fetch_account_holdings(token: str) -> tuple[list[dict], bool]:
	"""
	키움 REST kt00018(계좌평가잔고내역요청)으로 실제 계좌 보유 종목을 조회합니다.
	- TR: kt00018, endpoint: /api/dostk/acnt
	- Body: qry_tp(필수), dmst_stex_tp(필수) - acnt_no 불필요(토큰에서 자동 결정)
	- 응답 list 키: acnt_evlt_remn_indv_tot
	반환: (holdings_list, api_call_succeeded)
	/acct-debug 엔드포인트로 raw 응답 및 오류 확인 가능
	"""
	_ACCT_DEBUG["last_at"] = _now_str()
	_ACCT_DEBUG["api_call_ok"] = False
	_ACCT_DEBUG["tried_api_ids"] = ["kt00018"]

	# kt00018 body 후보: qry_tp(1:합산/2:개별), dmst_stex_tp(KRX/NXT/%)
	bodies = [
		{"qry_tp": "1", "dmst_stex_tp": "KRX"},
		{"qry_tp": "2", "dmst_stex_tp": "KRX"},
		{"qry_tp": "1", "dmst_stex_tp": "%"},
		{"qry_tp": "1", "dmst_stex_tp": "KRX", "acnt_no": re.sub(r"\D", "", bs.ACCOUNT_NO_RAW or "")},
	]
	_ACCT_DEBUG["last_tried_bodies"] = [dict(b) for b in bodies]

	last_err_msgs: list[str] = []
	raw_rows: list[dict] = []
	call_succeeded = False

	for body in bodies:
		try:
			r = bs.call_tr(token, api_id="kt00018", body=body, endpoint="/api/dostk/acnt", timeout=20)
			j = r.json()
			_ACCT_DEBUG["last_raw"] = j
			# kt00018 응답 list는 acnt_evlt_remn_indv_tot 키에 있음
			rows: Any = None
			if isinstance(j, dict):
				rows = j.get("acnt_evlt_remn_indv_tot")
			if not isinstance(rows, list):
				rows = bs._pick_list_rows(j)  # 폴백: 일반 파싱
			call_succeeded = True
			if isinstance(rows, list) and rows:
				raw_rows = rows
				_ACCT_DEBUG["last_error"] = ""
				_ACCT_DEBUG["api_call_ok"] = True
				break
			else:
				raw_keys = list(j.keys()) if isinstance(j, dict) else type(j).__name__
				_ACCT_DEBUG["last_error"] = f"API 성공 - 빈 잔고 또는 파싱 불일치. raw_keys={raw_keys}"
				_ACCT_DEBUG["api_call_ok"] = True
				break
		except Exception as e:
			msg = f"[body={list(body.keys())}] {str(e)}"
			last_err_msgs.append(msg)
			_ACCT_DEBUG["last_error"] = " | ".join(last_err_msgs[-3:])

	if not call_succeeded:
		_ACCT_DEBUG["last_error"] = "kt00018 모든 body 시도 실패: " + " | ".join(last_err_msgs)
		return [], False

	if not raw_rows:
		return [], True  # API 성공이지만 보유 종목 없음

	# 응답 row 필드 정규화 (kt00018 공식 필드명 기준, 폴백 후보 포함)
	results: list[dict] = []
	for row in raw_rows:
		if not isinstance(row, dict):
			continue
		# 종목코드: stk_cd (kt00018 공식)
		ticker_raw = str(
			bs._first_non_empty(row, ["stk_cd", "stkCd", "isin_cd", "iscd"]) or ""
		).strip()
		ticker = re.sub(r"\D", "", ticker_raw)[:6]
		if not re.fullmatch(r"\d{6}", ticker):
			continue
		# 종목명: stk_nm (kt00018 공식)
		name = str(
			bs._first_non_empty(row, ["stk_nm", "stkNm", "isu_nm", "isuNm", "stk_name"]) or ticker
		).strip()
		# 보유수량: rmnd_qty (kt00018 공식)
		qty = int(float(
			bs._first_non_empty(row, ["rmnd_qty", "rmndQty", "hld_qty", "hdng_qty", "hldg_qty", "bal_qty"]) or 0
		))
		if qty <= 0:
			continue
		# 매입가: pur_pric (kt00018 공식) / 폴백: avg_prc 계열
		avg_price = int(float(
			bs._first_non_empty(row, ["pur_pric", "purPric", "avg_prc", "avgPrc", "pchs_avg_prc", "pchs_pric"]) or 0
		))
		# 현재가: cur_prc (kt00018 공식)
		cur_price = int(float(
			bs._first_non_empty(row, ["cur_prc", "curPrc", "prst_prc", "prsntPrc", "last_prc"]) or 0
		))
		# 수익률: prft_rt (kt00018 공식)
		pnl_pct: float | None = None
		prft_rt_raw = bs._first_non_empty(row, ["prft_rt", "prftRt", "evlt_prft_rt", "evltPrftRt", "pnl_rt"])
		if prft_rt_raw is not None:
			try:
				pnl_pct = round(float(prft_rt_raw), 2)
			except Exception:
				pass
		if pnl_pct is None and avg_price > 0 and cur_price > 0:
			pnl_pct = round((cur_price - avg_price) * 100.0 / avg_price, 2)
		results.append({
			"ticker": ticker,
			"name": name,
			"qty": qty,
			"avg_price": avg_price,
			"cur_price": cur_price,
			"pnl_pct": pnl_pct,
		})
	return results, True


def _safe_get_open_price(token: str, ticker: str) -> tuple[int | None, str]:
	# 1) 당일 일봉 시가 우선
	try:
		ohlc_map = stg.base._fetch_ohlc_map(token, ticker, pages=1)
		today = datetime.now(TZ).strftime("%Y-%m-%d")
		op = int((ohlc_map.get(today) or {}).get("open") or 0)
		if op > 0:
			return op, "ohlc_open"
	except Exception:
		pass
	# 2) 없으면 최우선 매도호가/매수호가 대체
	try:
		stk_cd = bs._format_stk_cd(ticker, "KRX")
		q = bs.fetch_best_bid_ask(token, stk_cd, stex_tp="KRX")
		ask = int(q.get("best_ask") or 0)
		bid = int(q.get("best_bid") or 0)
		p = ask if ask > 0 else bid
		if p > 0:
			return p, "best_ask_bid"
	except Exception:
		pass
	return None, "none"


def _safe_get_current_price(token: str, ticker: str) -> tuple[int | None, str]:
	try:
		stk_cd = bs._format_stk_cd(ticker, "KRX")
		q = bs.fetch_best_bid_ask(token, stk_cd, stex_tp="KRX")
		bid = int(q.get("best_bid") or 0)
		ask = int(q.get("best_ask") or 0)
		p = bid if bid > 0 else ask
		if p > 0:
			return p, "best_bid_ask"
	except Exception:
		pass
	return None, "none"


def _safe_get_today_ohlc(token: str, ticker: str) -> tuple[dict[str, int] | None, str]:
	try:
		ohlc_map = stg.base._fetch_ohlc_map(token, ticker, pages=2)
		today = datetime.now(TZ).strftime("%Y-%m-%d")
		row = ohlc_map.get(today) or {}
		o = int(row.get("open") or 0)
		hi = int(row.get("high") or 0)
		lo = int(row.get("low") or 0)
		cl = int(row.get("close") or 0)
		if min(o, hi, lo, cl) > 0:
			return {"open": o, "high": hi, "low": lo, "close": cl}, "ohlc_today"
	except Exception:
		pass
	return None, "none"


def _is_after_close(now: datetime) -> bool:
	# 종가 기반 손절 판단은 장 종료 이후에만 수행
	return (now.hour > 15) or (now.hour == 15 and now.minute >= 30)


def _init_peak_high_from_history(token: str, ticker: str, buy_dt: str, buy_price: int) -> int:
	peak = int(buy_price or 0)
	try:
		ohlc_map = stg.base._fetch_ohlc_map(token, ticker, pages=18)
		today = datetime.now(TZ).strftime("%Y-%m-%d")
		for dt, row in (ohlc_map or {}).items():
			if not isinstance(dt, str) or dt < str(buy_dt) or dt > today:
				continue
			hi = int((row or {}).get("high") or 0)
			if hi > peak:
				peak = hi
	except Exception:
		pass
	return max(peak, int(buy_price or 0))


def _calc_sell_targets(buy_price: int, plan_row: dict[str, Any]) -> dict[str, float | int | None]:
	if buy_price <= 0:
		return {"tp_price": None, "tp_rate": None, "sl_price": None, "sl_rate": None}
	tp_mode_raw = str(plan_row.get("tp_mode") or "").strip().lower()
	sl_mode_raw = str(plan_row.get("sl_mode") or "").strip().lower()
	tp_mode = tp_mode_raw if tp_mode_raw in ("price", "rate") else ""
	sl_mode = sl_mode_raw if sl_mode_raw in ("price", "rate") else ""
	tp_price: int | None = None
	tp_rate: float | None = None
	sl_price: int | None = None
	sl_rate: float | None = None

	if tp_mode == "price":
		p = int(plan_row.get("tp_price") or 0)
		if p > 0:
			tp_price = p
			tp_rate = (p - buy_price) * 100.0 / buy_price
	elif tp_mode == "rate":
		r = float(plan_row.get("tp_rate") or 0)
		if r > 0:
			tp_rate = r
			tp_price = max(1, int(round(buy_price * (1.0 + r / 100.0))))

	if sl_mode == "price":
		p = int(plan_row.get("sl_price") or 0)
		if p > 0:
			sl_price = p
			sl_rate = (buy_price - p) * 100.0 / buy_price
	elif sl_mode == "rate":
		r = float(plan_row.get("sl_rate") or 0)
		if r > 0:
			sl_rate = r
			sl_price = max(1, int(round(buy_price * (1.0 - r / 100.0))))

	return {"tp_price": tp_price, "tp_rate": tp_rate, "sl_price": sl_price, "sl_rate": sl_rate}


def _place_buy_market(token: str, ticker: str, qty: int, exchange="KRX") -> dict[str, Any]:
	# 실거래 기본: 시장가 코드(trde_tp=03) 사용
	bs._validate_account_no()
	stk_cd = bs._format_stk_cd(ticker, exchange)
	body = {
		"dmst_stex_tp": str(exchange or "KRX"),
		"stk_cd": str(stk_cd),
		"ord_qty": str(int(qty)),
		"ord_uv": "0",
		"trde_tp": "03",  # 시장가
	}
	resp = bs.call_tr(token, api_id="kt10000", body=body, endpoint="/api/dostk/ordr", timeout=20)
	j = resp.json()
	try:
		rc = int(j.get("return_code")) if isinstance(j, dict) and ("return_code" in j) else 0
	except Exception:
		rc = 0
	if rc not in (0, None):
		raise RuntimeError(str(j.get("return_msg") or j))
	return j if isinstance(j, dict) else {"raw": j}


def _place_sell_limit_auto(token: str, ticker: str, qty: int, price: int, exchange="KRX") -> dict[str, Any]:
	bs._validate_account_no()
	stk_cd = bs._format_stk_cd(ticker, exchange)
	resp = bs._place_sell_limit(token, stk_cd=stk_cd, exchange=exchange, qty=int(qty), price=int(price), trde_tp="00")
	return resp if isinstance(resp, dict) else {"raw": resp}


def _place_sell_market_auto(token: str, ticker: str, qty: int, exchange="KRX") -> dict[str, Any]:
	bs._validate_account_no()
	stk_cd = bs._format_stk_cd(ticker, exchange)
	resp = bs._place_sell_limit(token, stk_cd=stk_cd, exchange=exchange, qty=int(qty), price=0, trde_tp="03")
	return resp if isinstance(resp, dict) else {"raw": resp}


def _is_market_day_now() -> bool:
	now = datetime.now(TZ)
	return now.weekday() < 5


def _execute_auto_trading_once():
	cfg = _load_bs_config()
	if not bool(cfg.get("enabled", True)):
		return
	if not _is_market_day_now():
		return
	now = datetime.now(TZ)
	h, m = _parse_hhmm(str(cfg.get("buy_start_hhmm") or "09:00"))
	start_t = now.replace(hour=h, minute=m, second=0, microsecond=0)
	if now < start_t:
		return

	plan_obj = _load_buy_plan()
	plans = plan_obj.get("plans") or []
	if not plans:
		return
	today = now.strftime("%Y-%m-%d")
	targets = [p for p in plans if str(p.get("buy_dt") or "") == today and str(p.get("status") or "") == "pending"]
	sell_targets = [p for p in plans if str(p.get("status") or "") == "bought" and int(p.get("buy_qty") or 0) > 0]
	if not targets and not sell_targets:
		return

	token = _get_cached_token()
	budget = int(cfg.get("budget_per_stock") or BS_DEFAULT_BUDGET)
	for p in targets:
		ticker = str(p.get("ticker") or "").strip()
		name = str(p.get("name") or ticker)
		if not re.fullmatch(r"\d{6}", ticker):
			p["status"] = "failed"
			p["last_error"] = "bad_ticker"
			p["updated_at"] = _now_str()
			_append_buy_log({"type": "buy_skip", "ticker": ticker, "name": name, "reason": "bad_ticker"})
			continue

		open_price, price_src = _safe_get_open_price(token, ticker)
		if not open_price or open_price <= 0:
			p["status"] = "failed"
			p["last_error"] = "open_price_unavailable"
			p["updated_at"] = _now_str()
			_append_buy_log({"type": "buy_fail", "ticker": ticker, "name": name, "reason": "open_price_unavailable"})
			continue

		item_budget = _env_int("BSDEMAND_BUDGET_PER_STOCK", int(p.get("buy_budget") or budget), 1, 2000000000)
		qty = int(item_budget // int(open_price))
		if qty < 1:
			# 1주 가격이 설정 금액을 넘으면 "초과매수 금지" 원칙상 주문하지 않음
			p["status"] = "failed"
			p["last_error"] = f"budget_too_small(price={open_price}, budget={item_budget})"
			p["updated_at"] = _now_str()
			_append_buy_log({"type": "buy_fail", "ticker": ticker, "name": name, "reason": "budget_too_small", "price": open_price, "budget": item_budget})
			continue

		last_err = ""
		success = False
		resp_j: dict[str, Any] = {}
		for attempt in range(BS_RETRY_COUNT + 1):
			try:
				resp_j = _place_buy_market(token, ticker, qty=qty, exchange="KRX")
				success = True
				break
			except Exception as e:
				last_err = str(e)
				_append_buy_log({"type": "buy_retry", "ticker": ticker, "name": name, "attempt": attempt + 1, "error": last_err})
				time.sleep(1.0)

		p["attempts"] = int((p.get("attempts") or 0)) + (BS_RETRY_COUNT + 1)
		p["buy_price"] = int(open_price)
		p["buy_qty"] = int(qty)
		p["peak_high"] = max(int(p.get("peak_high") or 0), int(open_price))
		p["trailing_trigger_dt"] = ""
		p["stop_trigger_dt"] = ""
		p["planned_sell_dt"] = ""
		p["trigger_reason"] = ""
		p["sell_reason"] = ""
		p["updated_at"] = _now_str()
		p["price_source"] = price_src
		if success:
			p["status"] = "bought"
			p["order_no"] = str(resp_j.get("ord_no") or resp_j.get("ordNo") or resp_j.get("order_no") or "")
			p["last_error"] = ""
			_append_buy_log(
				{
					"type": "buy_done",
					"ticker": ticker,
					"name": name,
					"qty": qty,
					"open_price": open_price,
					"budget": item_budget,
					"price_source": price_src,
					"order_no": p["order_no"],
				}
			)
		else:
			p["status"] = "failed"
			p["last_error"] = last_err or "unknown"
			_append_buy_log({"type": "buy_fail", "ticker": ticker, "name": name, "error": p["last_error"]})

	# 자동매도(신규 우선 로직):
	# - 트레일링: 매수일 고가부터 최고가 추적, 최고가 대비 10% 하락 시 트리거
	# - 손절: 당일 종가가 매수가 대비 7% 하락 시 트리거
	# - 체결: 트리거 다음 거래일 시가
	# - 동시 발생: 장중 저가 기반 트레일링 우선
	for p in sell_targets:
		ticker = str(p.get("ticker") or "").strip()
		name = str(p.get("name") or ticker)
		qty = int(p.get("buy_qty") or 0)
		buy_price = int(p.get("buy_price") or 0)
		buy_dt = str(p.get("buy_dt") or "")
		if qty <= 0 or buy_price <= 0 or not re.fullmatch(r"\d{6}", ticker) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", buy_dt):
			continue

		cur_price, cur_src = _safe_get_current_price(token, ticker)
		if not cur_price or cur_price <= 0:
			cur_price = 0
			cur_src = "none"
		if cur_price > 0:
			cur_ret = (cur_price - buy_price) * 100.0 / buy_price
			p["last_price"] = int(cur_price)
			p["last_price_source"] = cur_src
			p["pnl_pct"] = round(cur_ret, 2)

		today_ohlc, _ohlc_src = _safe_get_today_ohlc(token, ticker)
		today_dt = now.strftime("%Y-%m-%d")
		peak_high = int(p.get("peak_high") or 0)
		if peak_high <= 0:
			peak_high = _init_peak_high_from_history(token=token, ticker=ticker, buy_dt=buy_dt, buy_price=buy_price)

		if today_ohlc:
			peak_high = max(peak_high, int(today_ohlc.get("high") or 0))
		p["peak_high"] = int(max(peak_high, buy_price))

		trigger_reason = str(p.get("trigger_reason") or "").strip()
		planned_sell_dt = str(p.get("planned_sell_dt") or "").strip()

		# ── 손절: 현재가가 매수가 대비 7% 이하로 내려가면 트리거 없이 즉시 시장가 매도 ──
		if not trigger_reason:
			stop_line = int(round(float(buy_price) * (1.0 - BS_STOP_LOSS_PCT / 100.0)))
			stop_hit = cur_price > 0 and cur_price <= stop_line
			if stop_hit:
				p["stop_trigger_dt"] = today_dt
				p["trigger_reason"] = "STOP_LOSS"
				p["planned_sell_dt"] = today_dt
				_append_buy_log(
					{
						"type": "sell_trigger",
						"ticker": ticker,
						"name": name,
						"reason": "STOP_LOSS",
						"trigger_dt": today_dt,
						"planned_sell_dt": today_dt,
						"cur_price": int(cur_price),
						"stop_line": int(stop_line),
						"buy_price": int(buy_price),
					}
				)
				last_err = ""
				success = False
				resp_j: dict[str, Any] = {}
				for attempt in range(BS_RETRY_COUNT + 1):
					try:
						resp_j = _place_sell_market_auto(token, ticker=ticker, qty=qty, exchange="KRX")
						success = True
						break
					except Exception as e:
						last_err = str(e)
						_append_buy_log({"type": "sell_retry", "ticker": ticker, "name": name, "attempt": attempt + 1, "mode": "market", "reason": "STOP_LOSS", "error": last_err})
						time.sleep(1.0)
				# 시장가 실패 시 현재가 지정가로 재시도
				if not success and cur_price > 0:
					for attempt in range(BS_RETRY_COUNT + 1):
						try:
							resp_j = _place_sell_limit_auto(token, ticker=ticker, qty=qty, price=int(cur_price), exchange="KRX")
							success = True
							break
						except Exception as e:
							last_err = str(e)
							_append_buy_log({"type": "sell_retry", "ticker": ticker, "name": name, "attempt": attempt + 1, "mode": "limit_cur", "reason": "STOP_LOSS", "error": last_err})
							time.sleep(1.0)
				if success:
					sell_price = int(cur_price) if cur_price > 0 else buy_price
					sell_ret = (sell_price - buy_price) * 100.0 / buy_price
					p["status"] = "sold"
					p["sell_reason"] = "STOP_LOSS"
					p["sell_price"] = sell_price
					p["sell_qty"] = int(qty)
					p["sell_order_no"] = str(resp_j.get("ord_no") or resp_j.get("ordNo") or resp_j.get("order_no") or "")
					p["sell_at"] = _now_str()
					p["sell_price_source"] = cur_src
					p["pnl_pct"] = round(sell_ret, 2)
					p["last_error"] = ""
					_append_buy_log(
						{
							"type": "sell_done",
							"ticker": ticker,
							"name": name,
							"qty": qty,
							"sell_price": sell_price,
							"buy_price": buy_price,
							"pnl_pct": round(sell_ret, 2),
							"reason": "STOP_LOSS",
							"order_no": p["sell_order_no"],
							"planned_sell_dt": today_dt,
						}
					)
				else:
					p["last_error"] = last_err or "sell_unknown"
					_append_buy_log({"type": "sell_fail", "ticker": ticker, "name": name, "reason": "STOP_LOSS", "error": p["last_error"]})
				p["updated_at"] = _now_str()
				continue  # 손절 처리 완료 → 트레일링 로직 건너뜀

		# ── 트레일링 익절: 장중 저가가 최고가 대비 10% 하락 시 트리거 → 다음 거래일 시가 매도 ──
		if not trigger_reason and today_ohlc:
			lo = int(today_ohlc.get("low") or 0)
			trailing_line = int(round(float(p["peak_high"]) * (1.0 - BS_TRAILING_DROP_PCT / 100.0)))
			trailing_hit = lo > 0 and trailing_line > 0 and lo <= trailing_line
			if trailing_hit:
				trigger_reason = "TRAILING_STOP"
				p["trailing_trigger_dt"] = today_dt
				planned_sell_dt = _next_weekday(today_dt)
				p["trigger_reason"] = trigger_reason
				p["planned_sell_dt"] = planned_sell_dt
				_append_buy_log(
					{
						"type": "sell_trigger",
						"ticker": ticker,
						"name": name,
						"reason": trigger_reason,
						"trigger_dt": today_dt,
						"planned_sell_dt": planned_sell_dt,
						"peak_high": int(p["peak_high"]),
						"buy_price": int(buy_price),
					}
				)

		if trigger_reason and planned_sell_dt and planned_sell_dt <= today_dt:
			open_price, open_src = _safe_get_open_price(token, ticker)
			if not open_price or open_price <= 0:
				p["last_error"] = "sell_open_price_unavailable"
				p["updated_at"] = _now_str()
				continue

			last_err = ""
			success = False
			resp_j: dict[str, Any] = {}
			# 우선 시장가 매도 시도
			for attempt in range(BS_RETRY_COUNT + 1):
				try:
					resp_j = _place_sell_market_auto(token, ticker=ticker, qty=qty, exchange="KRX")
					success = True
					break
				except Exception as e:
					last_err = str(e)
					_append_buy_log({"type": "sell_retry", "ticker": ticker, "name": name, "attempt": attempt + 1, "mode": "market", "error": last_err})
					time.sleep(1.0)
			# 시장가 실패 시 시가 지정가로 한 번 더 시도
			if not success:
				for attempt in range(BS_RETRY_COUNT + 1):
					try:
						resp_j = _place_sell_limit_auto(token, ticker=ticker, qty=qty, price=int(open_price), exchange="KRX")
						success = True
						break
					except Exception as e:
						last_err = str(e)
						_append_buy_log({"type": "sell_retry", "ticker": ticker, "name": name, "attempt": attempt + 1, "mode": "limit_open", "error": last_err})
						time.sleep(1.0)

			if success:
				sell_price = int(open_price)
				sell_ret = (sell_price - buy_price) * 100.0 / buy_price
				p["status"] = "sold"
				p["sell_reason"] = trigger_reason
				p["sell_price"] = sell_price
				p["sell_qty"] = int(qty)
				p["sell_order_no"] = str(resp_j.get("ord_no") or resp_j.get("ordNo") or resp_j.get("order_no") or "")
				p["sell_at"] = _now_str()
				p["sell_price_source"] = open_src
				p["pnl_pct"] = round(sell_ret, 2)
				p["last_error"] = ""
				_append_buy_log(
					{
						"type": "sell_done",
						"ticker": ticker,
						"name": name,
						"qty": qty,
						"sell_price": sell_price,
						"buy_price": buy_price,
						"pnl_pct": round(sell_ret, 2),
						"reason": trigger_reason,
						"order_no": p["sell_order_no"],
						"planned_sell_dt": planned_sell_dt,
					}
				)
			else:
				p["last_error"] = last_err or "sell_unknown"
				_append_buy_log({"type": "sell_fail", "ticker": ticker, "name": name, "reason": trigger_reason, "error": p["last_error"]})

		p["updated_at"] = _now_str()

	plan_obj["plans"] = plans
	_save_buy_plan(plan_obj)


def _buy_scheduler_loop():
	while True:
		with _BS_RT_LOCK:
			_BS_RUNTIME["loop_count"] = int(_BS_RUNTIME.get("loop_count") or 0) + 1
			_BS_RUNTIME["last_loop_at"] = _now_str()
		try:
			_execute_auto_trading_once()
			need_recovered_log = False
			with _BS_RT_LOCK:
				need_recovered_log = bool(_BS_RUNTIME.get("last_error"))
				_BS_RUNTIME["last_ok_at"] = _now_str()
				_BS_RUNTIME["last_result"] = "ok"
				_BS_RUNTIME["last_error"] = ""
				_BS_RUNTIME["last_error_at"] = ""
			if need_recovered_log:
				_append_buy_log({"type": "scheduler_recovered", "message": "자동매수 스케줄러가 정상 상태로 복구되었습니다."})
		except Exception as e:
			err = str(e)
			should_log = False
			with _BS_RT_LOCK:
				prev_err = str(_BS_RUNTIME.get("last_error") or "")
				prev_err_at = str(_BS_RUNTIME.get("last_error_at") or "")
				now_s = _now_str()
				# 동일 오류는 로그 폭증 방지(마지막 오류 시각 기준 60초 이후에만 재기록)
				if err != prev_err:
					should_log = True
				elif prev_err_at:
					try:
						t_now = datetime.strptime(now_s, "%Y-%m-%d %H:%M:%S")
						t_prev = datetime.strptime(prev_err_at, "%Y-%m-%d %H:%M:%S")
						should_log = (t_now - t_prev).total_seconds() >= 60
					except Exception:
						should_log = True
				else:
					should_log = True
				_BS_RUNTIME["last_result"] = "error"
				_BS_RUNTIME["last_error"] = err
				_BS_RUNTIME["last_error_at"] = now_s
			if should_log:
				_append_buy_log({"type": "scheduler_error", "error": err})
		time.sleep(max(3.0, BS_LOOP_SLEEP_SEC))


def _patch_stgdemand_html():
	# stgdemand UI에 "자동매수 설정" 카드 추가
	if "자동매수 설정" in stg.HTML:
		return
	section = """
    <section class="card">
      <div class="cardTitle">자동매수 설정 (BSDemand)</div>
      <div class="controls">
        <label class="field"><span class="label">자동매수</span><input id="bsEnabled" type="checkbox" /></label>
        <label class="field"><span class="label">종목당 매수금액(원)</span><input id="bsBudget" class="input mono" type="number" min="10000" step="10000" inputmode="numeric" value="500000" /></label>
        <label class="field"><span class="label">매수 시작시각</span><input id="bsStartTime" class="input mono" type="time" value="09:00" /></label>
        <button id="btnSaveBsCfg" class="btn" type="button">매수설정 저장</button>
      </div>
      <div class="small mono" style="margin-top:6px;">※ 매수금액은 스피너 클릭 시 1만원 단위로 변경되며, 직접 숫자 입력도 가능합니다.</div>
      <div style="margin-top:8px; background:#f3f4f6; border:1px solid #e5e7eb; border-radius:10px; padding:10px 12px;">
        <div class="mono" style="font-size:13px; line-height:1.7; color:#111827; margin-bottom:4px;">
          자동매도 규칙: 익절(트레일링)=매수 후 최고가 대비 10% 하락(저가 기준) 시 트리거 → 다음 거래일 시가 매도, 손절=현재가가 매수가 대비 7% 이하 시 즉시 시장가 매도
        </div>
        <div class="mono" id="bsInfo" style="font-size:14px; line-height:1.6">자동매수 상태를 불러오는 중...</div>
        <div class="mono" id="bsMarketState" style="font-size:14px; line-height:1.6">장 상태 확인 중...</div>
        <div class="mono" id="bsPlanSummary" style="font-size:14px; line-height:1.6">매수 계획 확인 중...</div>
      </div>
      <div class="tableWrap" style="margin-top:8px">
        <table class="table" style="min-width: 0; width:100%">
          <thead>
            <tr>
              <th>종목명</th>
              <th>종목코드</th>
              <th>신호일</th>
              <th>매수예정일</th>
              <th class="right">최고가</th>
              <th>익절(트레일링) 트리거일</th>
              <th>손절 트리거일</th>
              <th>예정 매도일</th>
              <th>매도사유</th>
              <th class="right">현재수익률(%)</th>
              <th>상태</th>
            </tr>
          </thead>
          <tbody id="bsPendingBody"></tbody>
        </table>
      </div>
      <div style="margin-top:14px;">
        <div id="bsAcctTitle" class="mono" style="font-size:13px; font-weight:600; margin-bottom:6px; color:#374151;">
          계좌 보유 종목 (키움 실계좌 기준)
        </div>
        <div id="bsAcctError" class="small mono" style="color:#ef4444; display:none;"></div>
        <div class="tableWrap">
          <table class="table" style="min-width: 0; width:100%">
            <thead>
              <tr>
                <th>종목명</th>
                <th>종목코드</th>
                <th class="right">보유수량</th>
                <th class="right">평균단가(원)</th>
                <th class="right">현재가(원)</th>
                <th class="right">수익률(%)</th>
              </tr>
            </thead>
            <tbody id="bsAcctBody"></tbody>
          </table>
        </div>
      </div>
    </section>
"""
	# 자동 실행 설정 카드 뒤에 삽입
	marker = '    <section class="card">\n      <div class="cardTitle">최근 자동 실행 결과</div>'
	if marker in stg.HTML:
		stg.HTML = stg.HTML.replace(marker, section + marker)
	else:
		stg.HTML = stg.HTML.replace("</body>", section + "</body>")

	script = """
  <script>
    (function(){
      const enEl = document.getElementById("bsEnabled");
      const bdEl = document.getElementById("bsBudget");
      const stEl = document.getElementById("bsStartTime");
      const infoEl = document.getElementById("bsInfo");
      const marketEl = document.getElementById("bsMarketState");
      const planEl = document.getElementById("bsPlanSummary");
      const pendingBody = document.getElementById("bsPendingBody");
      const autoRunInfoEl = document.getElementById("autoRunInfo");
      const btnEl = document.getElementById("btnSaveBsCfg");
      if (!enEl || !bdEl || !stEl || !infoEl || !btnEl) return;
      function fmtPct(v){
        if (v === null || v === undefined || v === "") return "-";
        const n = Number(v);
        if (!Number.isFinite(n)) return "-";
        const s = n.toFixed(2);
        return (n > 0 ? "+" : "") + s;
      }
      function normalizeBudget(v, fallback){
        const s = String(v ?? "").replace(/[^0-9.-]/g, "");
        let n = Number(s);
        if (!Number.isFinite(n) || n <= 0) n = Number(fallback || 500000);
        if (!Number.isFinite(n) || n < 10000) n = 10000;
        // UI/입력 단위를 1만원으로 고정
        n = Math.round(n / 10000) * 10000;
        return Math.max(10000, Math.floor(n));
      }
      function fmtNum(v, fallback){
        return String(normalizeBudget(v, fallback));
      }
      function fmtReason(v){
        const s = String(v || "").trim().toUpperCase();
        if (!s) return "";
        if (s === "TRAILING_STOP") return "익절(트레일링)";
        if (s === "STOP_LOSS") return "손절";
        return String(v || "");
      }
      function todayKST(){
        return new Date().toLocaleDateString("en-CA", {timeZone: "Asia/Seoul"});
      }
      function renderPendingRows(rows){
        if (!pendingBody) return;
        pendingBody.innerHTML = "";
        const today = todayKST();
        // 예정매도일이 오늘보다 이전인 항목은 표시하지 않음 (하단 계좌 보유 섹션에서 표시)
        const xs = (Array.isArray(rows) ? rows : []).filter(r => {
          const psd = String(r.planned_sell_dt || "").trim();
          return !psd || psd >= today;
        });
        if (!xs.length) {
          const tr = document.createElement("tr");
          tr.innerHTML = `<td colspan="11" class="small mono">설정 가능한 종목(오늘/내일 대기 + 보유)이 없습니다.</td>`;
          pendingBody.appendChild(tr);
          return;
        }
        for (const r of xs) {
          const tpMode = (r.tp_mode === "price" || r.tp_mode === "rate") ? r.tp_mode : "";
          const slMode = (r.sl_mode === "price" || r.sl_mode === "rate") ? r.sl_mode : "";
          const tpValue = (tpMode === "price") ? Number(r.tp_price || 0) : Number(r.tp_rate || 0);
          const slValue = (slMode === "price") ? Number(r.sl_price || 0) : Number(r.sl_rate || 0);
          const cfgId = `cfg_${String(r.ticker || "").replace(/[^0-9A-Za-z_]/g, "_")}_${String(r.buy_dt || "").replace(/[^0-9A-Za-z_]/g, "_")}`;
          const trMain = document.createElement("tr");
          trMain.innerHTML = `
            <td class="mono">${r.name || ""}</td>
            <td class="mono">${r.ticker || ""}</td>
            <td class="mono">${r.signal_dt || ""}</td>
            <td class="mono">${r.buy_dt || ""}</td>
            <td class="right mono">${(Number(r.peak_high || 0) > 0) ? Number(r.peak_high).toLocaleString("ko-KR") : "-"}</td>
            <td class="mono">${r.trailing_trigger_dt || ""}</td>
            <td class="mono">${r.stop_trigger_dt || ""}</td>
            <td class="mono">${r.planned_sell_dt || ""}</td>
            <td class="mono">${fmtReason(r.sell_reason || r.trigger_reason || "")}</td>
            <td class="right mono">${fmtPct(r.pnl_pct)}</td>
            <td class="mono" style="white-space:nowrap;">
              <span>${r.status || ""}</span>
              <button class="btn bsCfgToggle" type="button" data-target="${cfgId}" style="margin-left:8px; padding:2px 8px; font-size:12px;">보조설정 펼치기</button>
            </td>
          `;
          pendingBody.appendChild(trMain);

          const trCfg = document.createElement("tr");
          trCfg.id = cfgId;
          trCfg.dataset.ticker = String(r.ticker || "");
          trCfg.dataset.buyDt = String(r.buy_dt || "");
          trCfg.style.display = "none";
          trCfg.innerHTML = `
            <td colspan="11" style="background:#fafafa; border-top:none; padding:10px 12px;">
              <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:6px;">
                <div class="small mono" style="color:#6b7280;">종목별 보조설정(선택)</div>
                <button class="btn bsCfgClose" type="button" data-target="${cfgId}" style="padding:2px 10px; font-size:12px;">닫기</button>
              </div>
              <div style="display:grid; grid-template-columns: repeat(6, minmax(120px, 1fr)); gap:8px; align-items:end;">
                <label class="field" style="margin:0"><span class="label">매수금액(원)</span><input class="input mono bsBuyBudget" type="number" min="10000" step="10000" inputmode="numeric" value="${fmtNum(r.buy_budget, bdEl.value)}" /></label>
                <label class="field" style="margin:0"><span class="label">수익매도 기준</span>
                  <select class="input mono bsTpMode">
                    <option value="" ${tpMode === "" ? "selected" : ""}>미설정</option>
                    <option value="rate" ${tpMode === "rate" ? "selected" : ""}>수익률(%)</option>
                    <option value="price" ${tpMode === "price" ? "selected" : ""}>가격(원)</option>
                  </select>
                </label>
                <label class="field" style="margin:0"><span class="label">수익매도 값</span><input class="input mono bsTpValue" type="number" min="0" step="0.1" value="${String(tpValue || 0)}" /></label>
                <label class="field" style="margin:0"><span class="label">손절매도 기준</span>
                  <select class="input mono bsSlMode">
                    <option value="" ${slMode === "" ? "selected" : ""}>미설정</option>
                    <option value="rate" ${slMode === "rate" ? "selected" : ""}>손절률(%)</option>
                    <option value="price" ${slMode === "price" ? "selected" : ""}>가격(원)</option>
                  </select>
                </label>
                <label class="field" style="margin:0"><span class="label">손절매도 값</span><input class="input mono bsSlValue" type="number" min="0" step="0.1" value="${String(slValue || 0)}" /></label>
                <div style="display:flex; justify-content:flex-end;">
                  <button class="btn bsRowSave" type="button">저장</button>
                </div>
              </div>
              <div class="small mono" style="margin-top:8px; color:#6b7280;">
                ※ 아래 값은 종목별 보조 설정 저장용입니다. 현재 자동매도는 상단에 표시된 트레일링/손절 규칙을 우선 적용합니다.
              </div>
            </td>
          `;
          pendingBody.appendChild(trCfg);
        }
      }
      let openPanelId = null;
      let bsBudgetDirty = false;
      bdEl.addEventListener("input", () => { bsBudgetDirty = true; });
      bdEl.addEventListener("blur", () => {
        bdEl.value = String(normalizeBudget(bdEl.value, 500000));
      });
      async function loadCfg(){
        const r = await fetch("/buy-config");
        const d = await r.json();
        if (!d.ok) return;
        enEl.checked = !!d.config.enabled;
        const budgetFromServer = normalizeBudget(d.config.budget_per_stock, 500000);
        if (document.activeElement !== bdEl && !bsBudgetDirty) {
          bdEl.value = String(budgetFromServer);
        }
        stEl.value = d.config.buy_start_hhmm || "09:00";
        const latest = d.latest_log || null;
        const latestMsg = latest ? (latest.message || latest.error || (latest.type || "-")) : "-";
        const staleHint = (Number(d.pending_total_count || 0) === 0 && Number(d.bought_count || 0) <= 0 && latest) ? " (과거 로그: 현재 대기/보유 종목 없음)" : "";
        const rt = d.runtime || null;
        const rtState = rt ? (rt.last_result === "error" ? "오류" : (rt.last_result === "ok" ? "정상" : "대기")) : "-";
        const rtTs = rt ? (rt.last_loop_at || "-") : "-";
        const rtExtra = (rt && rt.last_result === "error" && rt.last_error) ? ` / ${String(rt.last_error).slice(0, 140)}` : "";
        infoEl.textContent = `대기 ${d.pending_today_count || 0}건 · 보류전체 ${d.pending_total_count || 0}건 · 최근로그 ${latest ? (latest.ts + " / " + latestMsg) : "-"}${staleHint} · 스케줄러 ${rtState} (${rtTs})${rtExtra}`;
      }
      function renderAcctHoldings(rows, errMsg, fromApi){
        const acctBody = document.getElementById("bsAcctBody");
        const acctErrEl = document.getElementById("bsAcctError");
        const acctTitleEl = document.getElementById("bsAcctTitle");
        if (acctTitleEl) {
          acctTitleEl.textContent = fromApi
            ? "계좌 보유 종목 (키움 실계좌 기준)"
            : "계좌 보유 종목 (시스템 플랜 기준 - API 조회 실패)";
        }
        if (acctErrEl) {
          if (errMsg) {
            acctErrEl.textContent = errMsg;
            acctErrEl.style.display = "";
          } else {
            acctErrEl.style.display = "none";
          }
        }
        if (!acctBody) return;
        acctBody.innerHTML = "";
        const xs = Array.isArray(rows) ? rows : [];
        if (!xs.length) {
          const tr = document.createElement("tr");
          tr.innerHTML = `<td colspan="6" class="small mono" style="color:#9ca3af;">보유 종목이 없습니다.</td>`;
          acctBody.appendChild(tr);
          return;
        }
        for (const h of xs) {
          const pctVal = (h.pnl_pct !== null && h.pnl_pct !== undefined) ? Number(h.pnl_pct) : null;
          const pctStr = (pctVal !== null && Number.isFinite(pctVal)) ? ((pctVal >= 0 ? "+" : "") + pctVal.toFixed(2)) : "-";
          const pctColor = (pctVal === null || !Number.isFinite(pctVal)) ? "" : (pctVal >= 0 ? "color:#16a34a" : "color:#dc2626");
          // sell_pending: 오늘 이후 날짜의 익절/손절 트리거가 있는 경우만 뱃지 표시
          const sellBadge = h.sell_pending
            ? `<span style="margin-left:4px; font-size:11px; background:#fef9c3; color:#92400e; border:1px solid #fde68a; border-radius:4px; padding:1px 5px;">${h.trigger_reason === "TRAILING_STOP" ? "익절대기" : h.trigger_reason === "STOP_LOSS" ? "손절대기" : "매도대기"} ${h.planned_sell_dt || ""}</span>`
            : "";
          const tr = document.createElement("tr");
          tr.innerHTML = `
            <td class="mono">${h.name || ""}${sellBadge}</td>
            <td class="mono">${h.ticker || ""}</td>
            <td class="right mono">${Number(h.qty || 0).toLocaleString("ko-KR")}</td>
            <td class="right mono">${(Number(h.avg_price) > 0) ? Number(h.avg_price).toLocaleString("ko-KR") : "-"}</td>
            <td class="right mono">${(Number(h.cur_price) > 0) ? Number(h.cur_price).toLocaleString("ko-KR") : "-"}</td>
            <td class="right mono" style="${pctColor}; font-weight:600;">${pctStr}</td>
          `;
          acctBody.appendChild(tr);
        }
      }
      async function loadBuyStatus(){
        const r = await fetch("/buy-status");
        const d = await r.json();
        if (!d.ok) return;
        if (marketEl) marketEl.textContent = d.market_message || "-";
        if (planEl) planEl.textContent = `오늘 매수예정 ${d.pending_today_count || 0}건 · 내일 매수예정 ${d.pending_next_count || 0}건 · 보유 ${d.bought_count || 0}건`;
        renderPendingRows(d.watch_items || d.pending_today || []);
        renderAcctHoldings(d.acct_holdings || [], d.acct_error || "", !!d.acct_from_api);
      }
      async function loadAutoStatus(){
        if (!autoRunInfoEl) return;
        const r = await fetch("/schedule");
        const d = await r.json();
        if (!d.ok) return;
        const a = d.latest_auto || null;
        if (!a) return;
        const statusTxt = (a.status === "done") ? "완료" : (a.status === "running" ? "실행중" : "실패");
        const detail = a.job_id ? ` · 상세 /?job_id=${a.job_id}` : "";
        autoRunInfoEl.textContent = `상태 ${statusTxt} · 시작 ${a.started_at || "-"} · 종료 ${a.finished_at || "-"} · 진행 ${a.done || 0}/${a.total || 0} · 성공 ${a.ok || 0} · 실패 ${a.fail || 0}${a.error ? ` · 오류 ${a.error}` : ""}${detail}`;
      }
      btnEl.addEventListener("click", async () => {
        const budget = normalizeBudget(bdEl.value, 500000);
        bdEl.value = String(budget);
        const payload = {
          enabled: !!enEl.checked,
          budget_per_stock: budget,
          buy_start_hhmm: stEl.value || "09:00",
        };
        const r = await fetch("/buy-config", { method:"POST", headers:{ "Content-Type":"application/json" }, body: JSON.stringify(payload) });
        const d = await r.json();
        alert(d.ok ? "매수설정 저장 완료" : `저장 실패: ${d.error || "unknown"}`);
        if (d.ok) bsBudgetDirty = false;
        await loadCfg();
        await loadBuyStatus();
      });
      function closeCfgPanel(targetId){
        const trCfg = targetId ? document.getElementById(targetId) : null;
        if (!trCfg) return;
        trCfg.style.display = "none";
        // 헤더 행의 토글 버튼 텍스트 복원
        const prev = trCfg.previousElementSibling;
        if (prev) {
          const tgBtn = prev.querySelector(".bsCfgToggle");
          if (tgBtn) tgBtn.textContent = "보조설정 펼치기";
        }
        openPanelId = null;
      }
      pendingBody.addEventListener("click", async (ev) => {
        // 닫기 버튼
        const closeBtn = ev.target && ev.target.closest ? ev.target.closest(".bsCfgClose") : null;
        if (closeBtn) {
          const targetId = closeBtn.getAttribute("data-target") || "";
          closeCfgPanel(targetId);
          loadBuyStatus().catch(()=>{});
          return;
        }
        // 펼치기/접기 버튼
        const tgBtn = ev.target && ev.target.closest ? ev.target.closest(".bsCfgToggle") : null;
        if (tgBtn) {
          const targetId = tgBtn.getAttribute("data-target") || "";
          const trCfg = targetId ? document.getElementById(targetId) : null;
          if (trCfg) {
            const isOpen = trCfg.style.display !== "none";
            if (isOpen) {
              trCfg.style.display = "none";
              tgBtn.textContent = "보조설정 펼치기";
              openPanelId = null;
            } else {
              trCfg.style.display = "";
              tgBtn.textContent = "보조설정 접기";
              openPanelId = targetId;
            }
          }
          return;
        }
        // 저장 버튼
        const btn = ev.target && ev.target.closest ? ev.target.closest(".bsRowSave") : null;
        if (!btn) return;
        const tr = btn.closest("tr");
        if (!tr) return;
        const ticker = tr.dataset.ticker || "";
        const buyDt = tr.dataset.buyDt || "";
        const buyBudgetEl = tr.querySelector(".bsBuyBudget");
        const tpModeEl = tr.querySelector(".bsTpMode");
        const tpValueEl = tr.querySelector(".bsTpValue");
        const slModeEl = tr.querySelector(".bsSlMode");
        const slValueEl = tr.querySelector(".bsSlValue");
        const buyBudget = normalizeBudget(buyBudgetEl ? buyBudgetEl.value : 10000, bdEl.value || 500000);
        if (buyBudgetEl) buyBudgetEl.value = String(buyBudget);
        const payload = {
          ticker,
          buy_dt: buyDt,
          buy_budget: buyBudget,
          tp_mode: (tpModeEl ? tpModeEl.value : ""),
          tp_value: Number(tpValueEl ? tpValueEl.value : 0),
          sl_mode: (slModeEl ? slModeEl.value : ""),
          sl_value: Number(slValueEl ? slValueEl.value : 0),
        };
        const r = await fetch("/buy-plan-item", { method:"POST", headers:{ "Content-Type":"application/json" }, body: JSON.stringify(payload) });
        const d = await r.json();
        alert(d.ok ? `${ticker} 개별 설정 저장 완료` : `저장 실패: ${d.error || "unknown"}`);
        if (d.ok) {
          // 저장 후 패널 닫고 새로고침
          const cfgRow = btn.closest("tr[id]");
          if (cfgRow) closeCfgPanel(cfgRow.id);
          await loadBuyStatus();
        }
      });
      loadCfg().catch(()=>{});
      loadBuyStatus().catch(()=>{});
      loadAutoStatus().catch(()=>{});
      setInterval(() => {
        loadCfg().catch(()=>{});
        // 보조설정 패널이 열려있는 동안은 테이블 새로고침 중단 (입력 보호)
        if (!openPanelId) loadBuyStatus().catch(()=>{});
        loadAutoStatus().catch(()=>{});
      }, 5000);
    })();
  </script>
"""
	stg.HTML = stg.HTML.replace("</body>", script + "\n</body>")


def _install_job_hook():
	if getattr(_install_job_hook, "_installed", False):
		return
	setattr(_install_job_hook, "_installed", True)
	orig_run_job = stg._run_job

	def _wrapped_run_job(job):
		orig_run_job(job)
		try:
			# auto/manual 상관없이 신호일 결과를 매수플랜으로 누적
			_upsert_buy_plan_from_job(job)
		except Exception as e:
			_append_buy_log({"type": "plan_hook_error", "error": str(e), "job_id": str(getattr(job, "id", ""))})

	stg._run_job = _wrapped_run_job


@app.get("/buy-config")
def get_buy_config():
	cfg = _load_bs_config()
	plan = _load_buy_plan()
	plans = plan.get("plans") or []
	today = datetime.now(TZ).strftime("%Y-%m-%d")
	pending_today = sum(1 for p in plans if str(p.get("buy_dt") or "") == today and str(p.get("status") or "") == "pending")
	pending_total = sum(1 for p in plans if str(p.get("status") or "") == "pending")
	bought_total = sum(1 for p in plans if str(p.get("status") or "") == "bought")
	logs = _latest_buy_logs(1)
	with _BS_RT_LOCK:
		rt = dict(_BS_RUNTIME)
	return {
		"ok": True,
		"config": cfg,
		"pending_today_count": int(pending_today),
		"pending_total_count": int(pending_total),
		"bought_count": int(bought_total),
		"latest_log": logs[-1] if logs else None,
		"runtime": rt,
	}


@app.get("/buy-status")
def get_buy_status():
	plan = _load_buy_plan()
	plans = [p for p in (plan.get("plans") or []) if isinstance(p, dict)]
	today = datetime.now(TZ).strftime("%Y-%m-%d")
	next_day = _next_weekday(today)
	pending_today = [p for p in plans if str(p.get("buy_dt") or "") == today and str(p.get("status") or "") == "pending"]
	pending_next = [p for p in plans if str(p.get("buy_dt") or "") == next_day and str(p.get("status") or "") == "pending"]
	pos_bought = [p for p in plans if str(p.get("status") or "") == "bought"]
	for p in pos_bought:
		buy_price = int(p.get("buy_price") or 0)
		if buy_price > 0:
			tg = _calc_sell_targets(buy_price, p)
			p["tp_rate"] = round(float(tg["tp_rate"]), 2) if tg["tp_rate"] is not None else p.get("tp_rate")
			p["sl_rate"] = round(float(tg["sl_rate"]), 2) if tg["sl_rate"] is not None else p.get("sl_rate")
		p["peak_high"] = int(p.get("peak_high") or 0)
		p["trailing_trigger_dt"] = str(p.get("trailing_trigger_dt") or "")
		p["stop_trigger_dt"] = str(p.get("stop_trigger_dt") or "")
		p["planned_sell_dt"] = str(p.get("planned_sell_dt") or "")
		p["trigger_reason"] = str(p.get("trigger_reason") or "")

	# 상단 테이블: 매수 대기 + planned_sell_dt가 없거나 오늘 이후인 보유 종목만
	bought_active = [
		p for p in pos_bought
		if not str(p.get("planned_sell_dt") or "").strip()
		or str(p.get("planned_sell_dt") or "").strip() >= today
	]
	watch_items = pending_today + pending_next + bought_active
	watch_items = sorted(watch_items, key=lambda x: (str(x.get("buy_dt") or ""), str(x.get("status") or ""), str(x.get("ticker") or "")))[:180]

	# 시스템 플랜에서 bought 종목의 트리거 정보 인덱스 생성 (ticker → plan entry)
	plan_by_ticker: dict[str, dict] = {}
	for p in pos_bought:
		t = str(p.get("ticker") or "").strip()
		if t:
			plan_by_ticker[t] = p

	# 계좌 보유 종목: 키움 실계좌 API를 1차 소스로 사용
	acct_holdings: list[dict] = []
	acct_error: str = ""
	api_succeeded = False

	try:
		token = _get_cached_token()
		raw_holdings, api_call_ok = _safe_fetch_account_holdings(token)
		api_succeeded = api_call_ok
		if not api_call_ok:
			# API 호출 자체 실패 → 오류 메시지 표시
			acct_error = str(_ACCT_DEBUG.get("last_error") or "키움 API 호출 실패 - /acct-debug 확인")
		for h in raw_holdings:
			ticker = h["ticker"]
			plan_entry = plan_by_ticker.get(ticker)
			sell_pending = False
			sell_trigger_reason = ""
			sell_planned_dt = ""
			if plan_entry:
				psd = str(plan_entry.get("planned_sell_dt") or "").strip()
				tr = str(plan_entry.get("trigger_reason") or "").strip()
				# 오늘 또는 미래 날짜의 트리거만 '대기' 뱃지 표시
				# 과거 날짜 트리거는 매도 미체결 상태이므로 뱃지 없이 보유 종목으로 표시
				if psd and psd >= today and tr:
					sell_pending = True
					sell_trigger_reason = tr
					sell_planned_dt = psd
			acct_holdings.append({
				"ticker": ticker,
				"name": h.get("name") or ticker,
				"qty": h.get("qty") or 0,
				"avg_price": h.get("avg_price") or 0,
				"cur_price": h.get("cur_price") or 0,
				"pnl_pct": h.get("pnl_pct"),
				"sell_pending": sell_pending,
				"trigger_reason": sell_trigger_reason,
				"planned_sell_dt": sell_planned_dt,
				"from_api": True,
			})
	except Exception as e:
		acct_error = str(e)[:300]

	# API 호출 실패 시: 시스템 플랜의 bought 항목으로 폴백
	if not api_succeeded:
		for p in pos_bought:
			ticker = str(p.get("ticker") or "").strip()
			psd = str(p.get("planned_sell_dt") or "").strip()
			tr = str(p.get("trigger_reason") or "").strip()
			sell_pending = bool(psd and psd >= today and tr)
			acct_holdings.append({
				"ticker": ticker,
				"name": str(p.get("name") or ticker),
				"qty": int(p.get("buy_qty") or 0),
				"avg_price": int(p.get("buy_price") or 0),
				"cur_price": int(p.get("last_price") or 0),
				"pnl_pct": p.get("pnl_pct"),
				"sell_pending": sell_pending,
				"trigger_reason": tr if sell_pending else "",
				"planned_sell_dt": psd if sell_pending else "",
				"from_api": False,
			})

	return {
		"ok": True,
		"market_message": _market_wait_message(),
		"pending_today_count": len(pending_today),
		"pending_next_count": len(pending_next),
		"bought_count": len(pos_bought),
		"pending_today": watch_items,
		"watch_items": watch_items,
		"acct_holdings": acct_holdings,
		"acct_error": acct_error,
		"acct_from_api": api_succeeded,
	}


@app.post("/buy-config")
def set_buy_config():
	payload = request.get_json(force=True) or {}
	cfg = _load_bs_config()
	cfg["enabled"] = bool(payload.get("enabled", cfg.get("enabled", True)))
	cfg["budget_per_stock"] = _env_int("BSDEMAND_BUDGET_PER_STOCK", int(payload.get("budget_per_stock") or cfg.get("budget_per_stock") or BS_DEFAULT_BUDGET), 1, 2000000000)
	cfg["buy_start_hhmm"] = str(payload.get("buy_start_hhmm") or cfg.get("buy_start_hhmm") or "09:00")
	_save_bs_config(cfg)
	return {"ok": True, "config": _load_bs_config()}


@app.post("/buy-plan-item")
def set_buy_plan_item():
	payload = request.get_json(force=True) or {}
	ticker = str(payload.get("ticker") or "").strip()
	buy_dt = str(payload.get("buy_dt") or "").strip()
	if not re.fullmatch(r"\d{6}", ticker):
		return {"ok": False, "error": "bad_ticker"}, 400
	if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", buy_dt):
		return {"ok": False, "error": "bad_buy_dt"}, 400

	plan = _load_buy_plan()
	plans = [dict(p) for p in (plan.get("plans") or []) if isinstance(p, dict)]
	target = None
	for p in plans:
		if str(p.get("ticker") or "") == ticker and str(p.get("buy_dt") or "") == buy_dt and str(p.get("status") or "") in ("pending", "bought"):
			target = p
			break
	if target is None:
		return {"ok": False, "error": "plan_not_found"}, 404

	target["buy_budget"] = _env_int("BSDEMAND_BUDGET_PER_STOCK", int(payload.get("buy_budget") or 1), 1, 2000000000)
	tp_mode_raw = str(payload.get("tp_mode") or "").strip().lower()
	sl_mode_raw = str(payload.get("sl_mode") or "").strip().lower()
	tp_value = max(0.0, float(payload.get("tp_value") or 0))
	sl_value = max(0.0, float(payload.get("sl_value") or 0))

	if tp_mode_raw in ("price", "rate"):
		target["tp_mode"] = tp_mode_raw
		if tp_mode_raw == "price":
			target["tp_price"] = _env_int("BSDEMAND_TP_PRICE", int(tp_value), 0, 2000000000)
			target["tp_rate"] = None
		else:
			target["tp_rate"] = float(tp_value)
			target["tp_price"] = 0
	else:
		target["tp_mode"] = ""
		target["tp_rate"] = None
		target["tp_price"] = 0

	if sl_mode_raw in ("price", "rate"):
		target["sl_mode"] = sl_mode_raw
		if sl_mode_raw == "price":
			target["sl_price"] = _env_int("BSDEMAND_SL_PRICE", int(sl_value), 0, 2000000000)
			target["sl_rate"] = None
		else:
			target["sl_rate"] = float(sl_value)
			target["sl_price"] = 0
	else:
		target["sl_mode"] = ""
		target["sl_rate"] = None
		target["sl_price"] = 0

	target["updated_at"] = _now_str()
	plan["plans"] = plans
	_save_buy_plan(plan)
	return {"ok": True, "item": target}


@app.get("/buy-plan")
def get_buy_plan():
	plan = _load_buy_plan()
	return {"ok": True, "plans": plan.get("plans") or []}


@app.get("/buy-log")
def get_buy_log():
	return {"ok": True, "logs": _latest_buy_logs(200)}


@app.get("/acct-debug")
def get_acct_debug():
	"""
	계좌 조회 디버그 엔드포인트.
	브라우저에서 http://127.0.0.1:7791/acct-debug 로 접속하면
	키움 API 응답 원본과 오류 원인을 JSON으로 확인할 수 있습니다.
	"""
	ac = re.sub(r"\D", "", bs.ACCOUNT_NO_RAW or "")
	token_ok = False
	token_err = ""
	holdings: list[dict] = []
	api_call_ok = False
	try:
		token = _get_cached_token()
		token_ok = True
		holdings, api_call_ok = _safe_fetch_account_holdings(token)
	except Exception as e:
		token_err = str(e)
	return {
		"ok": True,
		"diagnosis": {
			"account_no_raw": bs.ACCOUNT_NO_RAW or "(미설정)",
			"account_no_digits": ac or "(비어있음)",
			"account_no_length": len(ac),
			"account_no_ok": len(ac) >= 10,
			"token_ok": token_ok,
			"token_err": token_err,
			"api_call_ok": api_call_ok,
			"holdings_count": len(holdings),
			"tried_api_ids": _ACCT_DEBUG.get("tried_api_ids", []),
		},
		"last_error": _ACCT_DEBUG.get("last_error", ""),
		"last_raw_response": _ACCT_DEBUG.get("last_raw"),
		"parsed_holdings": holdings,
		"last_checked_at": _ACCT_DEBUG.get("last_at", ""),
		"hint": "last_error 내용을 확인하고, last_raw_response 의 키 이름을 개발자에게 알려주세요.",
	}


def _start_buy_scheduler_once():
	if getattr(_start_buy_scheduler_once, "_started", False):
		return
	setattr(_start_buy_scheduler_once, "_started", True)
	with _BS_RT_LOCK:
		_BS_RUNTIME["thread_started_at"] = _now_str()
	# 재시작 직후 화면에서 과거 오류/현재 상태를 구분할 수 있게 마지막 오류를 runtime에 초기 반영
	try:
		last = _latest_buy_logs(1)
		if last and str((last[-1] or {}).get("type") or "") == "scheduler_error":
			with _BS_RT_LOCK:
				_BS_RUNTIME["last_error"] = str((last[-1] or {}).get("error") or "")
				_BS_RUNTIME["last_error_at"] = str((last[-1] or {}).get("ts") or "")
				_BS_RUNTIME["last_result"] = "error"
	except Exception:
		pass
	th = threading.Thread(target=_buy_scheduler_loop, daemon=True)
	th.start()


_patch_stgdemand_html()
_install_job_hook()
_start_buy_scheduler_once()


if __name__ == "__main__":
	# stgdemand와 동일 포트 사용 (bsdemand만 단독 실행 가정)
	app.run(host="0.0.0.0", port=7791, debug=False)

