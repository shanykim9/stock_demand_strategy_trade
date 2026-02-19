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

	token = bs.get_token(bs.APP_KEY, bs.APP_SECRET)
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

		if not trigger_reason and today_ohlc:
			lo = int(today_ohlc.get("low") or 0)
			cl = int(today_ohlc.get("close") or 0)
			trailing_line = int(round(float(p["peak_high"]) * (1.0 - BS_TRAILING_DROP_PCT / 100.0)))
			trailing_hit = lo > 0 and trailing_line > 0 and lo <= trailing_line
			stop_hit = _is_after_close(now) and cl > 0 and cl <= int(round(float(buy_price) * (1.0 - BS_STOP_LOSS_PCT / 100.0)))
			if trailing_hit or stop_hit:
				# 동시 발생 시 트레일링 우선
				if trailing_hit:
					trigger_reason = "TRAILING_STOP"
					p["trailing_trigger_dt"] = today_dt
					if stop_hit and not str(p.get("stop_trigger_dt") or ""):
						p["stop_trigger_dt"] = today_dt
				else:
					trigger_reason = "STOP_LOSS"
					p["stop_trigger_dt"] = today_dt
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
        <label class="field"><span class="label">종목당 매수금액(원)</span><input id="bsBudget" class="input mono" type="number" min="1" step="1000" value="500000" /></label>
        <label class="field"><span class="label">매수 시작시각</span><input id="bsStartTime" class="input mono" type="time" value="09:00" /></label>
        <button id="btnSaveBsCfg" class="btn" type="button">매수설정 저장</button>
      </div>
      <div style="margin-top:8px; background:#f3f4f6; border:1px solid #e5e7eb; border-radius:10px; padding:10px 12px;">
        <div class="mono" style="font-size:13px; line-height:1.7; color:#111827; margin-bottom:4px;">
          자동매도 규칙: 익절(트레일링)=매수 후 최고가 대비 10% 하락(저가 기준) 시 트리거, 손절=매수가 대비 7% 하락(종가 기준) 시 트리거, 체결은 모두 다음 거래일 시가, 동시 발생 시 익절(트레일링) 우선
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
      function fmtNum(v, fallback){
        const n = Number(v || 0);
        if (Number.isFinite(n) && n >= 1) return String(Math.floor(n));
        const f = Number(fallback || 500000);
        return Number.isFinite(f) && f >= 1 ? String(Math.floor(f)) : "500000";
      }
      function fmtReason(v){
        const s = String(v || "").trim().toUpperCase();
        if (!s) return "";
        if (s === "TRAILING_STOP") return "익절(트레일링)";
        if (s === "STOP_LOSS") return "손절";
        return String(v || "");
      }
      function renderPendingRows(rows){
        if (!pendingBody) return;
        pendingBody.innerHTML = "";
        const xs = Array.isArray(rows) ? rows : [];
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
            <td class="mono">${r.status || ""}</td>
          `;
          pendingBody.appendChild(trMain);

          const trCfg = document.createElement("tr");
          trCfg.dataset.ticker = String(r.ticker || "");
          trCfg.dataset.buyDt = String(r.buy_dt || "");
          trCfg.innerHTML = `
            <td colspan="11" style="background:#fafafa; border-top:none; padding:10px 12px;">
              <div style="display:grid; grid-template-columns: repeat(6, minmax(120px, 1fr)); gap:8px; align-items:end;">
                <label class="field" style="margin:0"><span class="label">매수금액(원)</span><input class="input mono bsBuyBudget" type="number" min="1" step="1000" value="${fmtNum(r.buy_budget, bdEl.value)}" /></label>
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
      async function loadCfg(){
        const r = await fetch("/buy-config");
        const d = await r.json();
        if (!d.ok) return;
        enEl.checked = !!d.config.enabled;
        bdEl.value = String(d.config.budget_per_stock || 500000);
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
      async function loadBuyStatus(){
        const r = await fetch("/buy-status");
        const d = await r.json();
        if (!d.ok) return;
        if (marketEl) marketEl.textContent = d.market_message || "-";
        if (planEl) planEl.textContent = `오늘 매수예정 ${d.pending_today_count || 0}건 · 내일 매수예정 ${d.pending_next_count || 0}건 · 보유 ${d.bought_count || 0}건`;
        renderPendingRows(d.watch_items || d.pending_today || []);
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
        const payload = {
          enabled: !!enEl.checked,
          budget_per_stock: Number(bdEl.value || 500000),
          buy_start_hhmm: stEl.value || "09:00",
        };
        const r = await fetch("/buy-config", { method:"POST", headers:{ "Content-Type":"application/json" }, body: JSON.stringify(payload) });
        const d = await r.json();
        alert(d.ok ? "매수설정 저장 완료" : `저장 실패: ${d.error || "unknown"}`);
        await loadCfg();
        await loadBuyStatus();
      });
      pendingBody.addEventListener("click", async (ev) => {
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
        const payload = {
          ticker,
          buy_dt: buyDt,
          buy_budget: Number(buyBudgetEl ? buyBudgetEl.value : 1),
          tp_mode: (tpModeEl ? tpModeEl.value : ""),
          tp_value: Number(tpValueEl ? tpValueEl.value : 0),
          sl_mode: (slModeEl ? slModeEl.value : ""),
          sl_value: Number(slValueEl ? slValueEl.value : 0),
        };
        const r = await fetch("/buy-plan-item", { method:"POST", headers:{ "Content-Type":"application/json" }, body: JSON.stringify(payload) });
        const d = await r.json();
        alert(d.ok ? `${ticker} 개별 설정 저장 완료` : `저장 실패: ${d.error || "unknown"}`);
        if (d.ok) await loadBuyStatus();
      });
      loadCfg().catch(()=>{});
      loadBuyStatus().catch(()=>{});
      loadAutoStatus().catch(()=>{});
      setInterval(() => { loadCfg().catch(()=>{}); loadBuyStatus().catch(()=>{}); loadAutoStatus().catch(()=>{}); }, 5000);
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
	# 대기 종목 + 보유(bought) 상태를 같이 노출해 매도 감시 상태를 확인할 수 있게 함
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
	watch_items = pending_today + pending_next + pos_bought
	watch_items = sorted(watch_items, key=lambda x: (str(x.get("buy_dt") or ""), str(x.get("status") or ""), str(x.get("ticker") or "")))[:180]
	return {
		"ok": True,
		"market_message": _market_wait_message(),
		"pending_today_count": len(pending_today),
		"pending_next_count": len(pending_next),
		"bought_count": len(pos_bought),
		"pending_today": watch_items,
		"watch_items": watch_items,
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

