from __future__ import annotations

import hashlib
import os
import re
import sys
import time
from datetime import datetime
from typing import Any

import bot_store

# buysell.py의 검증된 키움 REST 호출/유틸을 재사용합니다.
# (Flask app 객체도 함께 로드되지만 __main__이 아니므로 서버는 뜨지 않습니다.)
import buysell as api


TZ = api.TZ


def _parse_token_error(s: str) -> dict[str, Any]:
	"""
	예외 문자열에서 (가능하면) 토큰 오류코드/return_code를 추출합니다.
	예: "Token error: ... [8050:지정단말기 ...] (return_code=3) ..."
	"""
	s = str(s or "")
	code = None
	rc = None
	m = re.search(r"\[(\d{4,5})\s*:", s)
	if m:
		code = m.group(1)
	m2 = re.search(r"return_code\s*=\s*(\d+)", s)
	if m2:
		rc = m2.group(1)
	hint = ""
	if code == "8050" or ("지정단말기" in s):
		hint = "키움 API센터/HTS에서 지정단말기 인증이 필요합니다."
	if code == "8005" or ("Token이 유효하지" in s) or ("Token이" in s and "유효" in s):
		hint = "토큰이 만료/무효입니다. 자동으로 재발급을 시도합니다."
	return {"code": code, "return_code": rc, "detail": s, "hint": hint}


def _is_token_invalid(e: Exception | str) -> bool:
	s = str(e)
	return ("[8005:" in s) or ("8005" in s and "Token" in s) or ("Token이 유효하지" in s)


def _krx_order_allowed(now: datetime) -> tuple[bool, datetime | None, str]:
	"""
	KRX 주문 가능 여부(간단 게이트).
	- 장전 동시호가/접수 시작 시각은 환경변수(KIWOOM_KRX_PREMARKET_START / KIWOOM_LIVE_ORDER_START) 기준
	- 종료는 15:30 기본(필요시 env로 조정)
	"""
	end_hhmm = (os.getenv("KIWOOM_KRX_MARKET_END") or "15:30").strip()
	# KRX 시작 시각은 "장전접수 시작"과 "LIVE 주문 게이트" 중 더 늦은 시각을 사용
	h1, m1 = api._parse_hhmm(api.KRX_PREMARKET_START_HHMM)
	h2, m2 = api._parse_hhmm(api.LIVE_ORDER_START_HHMM)
	start_h, start_m = (h1, m1) if (h1, m1) >= (h2, m2) else (h2, m2)
	start = now.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
	end = api._hhmm_today(now, end_hhmm)
	if now < start:
		return (False, start, f"미개장({start.strftime('%H:%M')} 이전)")
	if now >= end:
		return (False, start + api.timedelta(days=1), f"장종료({end_hhmm} 이후)")
	return (True, None, "ok")


def _pick_active_exchange(plan_exchange: str, now: datetime) -> tuple[str, str, datetime | None]:
	"""
	AUTO면 현재 시각 기준으로 NXT/KRX 중 '주문 가능한 시장'을 선택합니다.
	- 둘 다 가능하면 선호도(env: KIWOOM_AUTO_EXCHANGE_PREFERENCE)를 따르되,
	  미설정 시에는 KRX 정규장 시간(기본 09:00~15:30)에는 KRX를 우선합니다.
	"""
	pe = (plan_exchange or "AUTO").strip().upper()
	if pe in ("KRX", "NXT", "SOR"):
		return (pe, "explicit", None)

	# AUTO
	nxt_ok, nxt_next, nxt_reason = api._nxt_new_order_allowed(now)
	krx_ok, krx_next, krx_reason = _krx_order_allowed(now)

	pref_env = (os.getenv("KIWOOM_AUTO_EXCHANGE_PREFERENCE") or "").strip().upper()
	# 정규장 시간대에는 기본 KRX 우선(사용자가 env로 명시하면 그 값을 우선)
	krx_regular_start = api._hhmm_today(now, "09:00")
	krx_regular_end = api._hhmm_today(now, os.getenv("KIWOOM_KRX_MARKET_END") or "15:30")
	default_pref = "KRX" if (krx_regular_start <= now < krx_regular_end) else "NXT"
	pref = pref_env if pref_env in ("KRX", "NXT") else default_pref
	if nxt_ok and krx_ok:
		chosen = "KRX" if pref == "KRX" else "NXT"
		return (chosen, f"both-open(pref={pref}{',env' if pref_env else ''})", None)
	if nxt_ok:
		return ("NXT", f"nxt({nxt_reason})", None)
	if krx_ok:
		return ("KRX", f"krx({krx_reason})", None)

	# 둘 다 불가: 다음 가능 시각 안내(더 이른 쪽)
	next_at = None
	reason = "closed"
	cands = [t for t in [nxt_next, krx_next] if t is not None]
	if cands:
		next_at = sorted(cands)[0]
		reason = f"next={next_at.strftime('%H:%M')}"
	return ("NXT" if pref != "KRX" else "KRX", reason, next_at)


def _is_market_closed(e: Exception | str) -> bool:
	s = str(e)
	return ("return_code=20" in s) or ("장종료" in s) or ("장종료되었습니다" in s) or ("장 종료" in s)


def _is_symbol_not_found(e: Exception | str) -> bool:
	"""
	키움 TR이 종목코드를 인식하지 못하거나(거래소 불일치 포함) 발생하는 대표 에러를 감지합니다.
	예: "1902: 종목 정보가 없습니다 ... (return_code=7)"
	"""
	s = str(e)
	return ("1902" in s and ("종목" in s or "종목코드" in s)) or ("종목 정보가 없습니다" in s)


def _next_open_for_exchange(exchange: str, now: datetime) -> datetime:
	ex = (exchange or "KRX").strip().upper()
	if ex == "NXT":
		ok, next_at, _ = api._nxt_new_order_allowed(now)
		return next_at if next_at is not None else (now + api.timedelta(minutes=5))
	# KRX
	ok, next_at, _ = _krx_order_allowed(now)
	return next_at if next_at is not None else (now + api.timedelta(minutes=5))


def _plan_key(plan: dict[str, Any]) -> str:
	core = {
		"ticker": str(plan.get("ticker") or "").strip(),
		"exchange": str(plan.get("exchange") or "").strip().upper(),
		"qty": int(plan.get("qty") or 0),
		"buy_price": int(plan.get("buy_price") or 0),
		"stop_loss": int(plan.get("stop_loss") or 0),
		"take_profit": int(plan.get("take_profit") or 0),
	}
	s = repr(sorted(core.items())).encode("utf-8")
	return hashlib.sha1(s).hexdigest()[:12]


def _fmt_ts(ts: float | None) -> str:
	if not ts:
		return ""
	try:
		return datetime.fromtimestamp(float(ts), tz=TZ).isoformat(timespec="seconds")
	except Exception:
		return ""


def _safe_int(v, default=0) -> int:
	try:
		return int(v)
	except Exception:
		return default


def _extract_order_no(resp: dict) -> str:
	return str(api._first_non_empty(resp, ["ord_no", "ordNo", "order_no", "ordNo"]) or "")


def _match_order(rows: list[dict] | None, ord_no: str) -> bool:
	if not ord_no:
		return False
	for x in (rows or []):
		on = str(api._first_non_empty(x, ["ord_no", "ordNo", "order_no"]) or "")
		if on == ord_no:
			return True
	return False


def _latest_buy_fill(fills: list[dict] | None, ticker: str | None = None) -> dict[str, Any] | None:
	"""
	체결 목록에서 '매수'로 보이는 가장 최근 체결 1건을 뽑아옵니다.
	필드명이 환경에 따라 달라질 수 있어 후보 키를 폭넓게 봅니다.
	"""
	best = None
	best_tm = ""
	for x in (fills or []):
		if not isinstance(x, dict):
			continue
		if ticker:
			sc = str(x.get("stk_cd") or x.get("stkCd") or "")
			if sc and re.sub(r"\\D", "", sc)[:6] != re.sub(r"\\D", "", ticker)[:6]:
				continue
		side = str(x.get("io_tp_nm") or x.get("trde_tp") or x.get("side") or "").strip()
		# "+매수" / "매수" 등
		if ("매수" not in side) and (side not in ("2", "B", "BUY")):
			continue
		tm = str(x.get("ord_tm") or x.get("cntr_tm") or x.get("time") or x.get("tm") or "").strip()
		if tm and tm >= best_tm:
			best_tm = tm
			best = x
	return best


def _load_or_init_runtime(plan_id: str, plan_key: str) -> dict[str, Any]:
	rt = bot_store.load_runtime() or {}
	if rt.get("plan_id") != plan_id or rt.get("plan_key") != plan_key:
		rt = {
			"plan_id": plan_id,
			"plan_key": plan_key,
			"position_qty": 0,
			"buy_submitted": False,
			"sell_submitted": False,
			"last_buy_order_no": "",
			"last_sell_order_no": "",
			"last_order_no": "",
			"last_buy_order_price": 0,
			"last_buy_fill_price": 0,
			"last_buy_fill_time": "",
			"last_buy_order_at": "",
			"started_at": bot_store.now_ts(),
		}
		bot_store.save_runtime(rt)
	return rt


def _update_state(state: dict[str, Any]):
	state = dict(state)
	state["heartbeat_ts"] = bot_store.now_ts()
	bot_store.save_state(state)


def _log(msg: str, **extra):
	print(msg, flush=True)
	try:
		bot_store.log_event({"msg": msg, **extra})
	except Exception:
		pass


def run_forever():
	bot_store.ensure_data_dir()
	_log("bot_runner 시작됨", pid=os.getpid())

	token: str | None = None
	token_ok_at: float | None = None

	last_status_msg = ""
	last_token_err_msg = ""
	last_token_err_at = 0.0
	while True:
		plan_wrap = bot_store.load_plan() or {}
		enabled = bool(plan_wrap.get("enabled"))
		plan_id = str(plan_wrap.get("plan_id") or "")
		p = plan_wrap.get("plan") if isinstance(plan_wrap, dict) else None
		p = p if isinstance(p, dict) else {}
		# ✅ 중요: plan_key는 wrapper가 아니라 "plan 본문"으로 계산해야 런타임이 올바르게 초기화됩니다.
		plan_key = _plan_key(p) if p else ""

		# 기본 state 틀
		state: dict[str, Any] = {
			"ok": True,
			"runner": {
				"pid": os.getpid(),
				"started_at": plan_wrap.get("runner_started_at") or "",
				"heartbeat_ts": bot_store.now_ts(),
				"token_ok_at": _fmt_ts(token_ok_at),
			},
			"mode": "LIVE" if (api.ENABLE_LIVE and (not api.DRY_RUN) and api.ACCOUNT_NO) else "DRY_RUN",
			"message": "",
			"token_error": None,
			"last_action_at": datetime.now(TZ).isoformat(timespec="seconds"),
			"last_price": 0,
			"last_bid": 0,
			"last_ask": 0,
			"position_qty": 0,
			"buy_submitted": False,
			"sell_submitted": False,
			"last_order_no": "",
			"fills": [],
			"unfilled": [],
			"plan": p or None,
			"enabled": enabled,
		}
		# 직전 상태가 있으면 시세표시(0 깜빡임) 최소화
		try:
			prev = bot_store.load_state() or {}
			if not state.get("last_price"):
				state["last_price"] = _safe_int(prev.get("last_price"), 0)
			if not state.get("last_bid"):
				state["last_bid"] = _safe_int(prev.get("last_bid"), 0)
			if not state.get("last_ask"):
				state["last_ask"] = _safe_int(prev.get("last_ask"), 0)
		except Exception:
			pass

		# 런타임은 enabled 여부와 상관없이 로드해서 UI가 "중지" 시에도 포지션이 0으로 튀지 않게 합니다.
		rt = _load_or_init_runtime(plan_id=plan_id, plan_key=plan_key) if p else (bot_store.load_runtime() or {})
		# 런타임 상태 반영(항상)
		state["position_qty"] = int(rt.get("position_qty") or 0)
		state["buy_submitted"] = bool(rt.get("buy_submitted"))
		state["sell_submitted"] = bool(rt.get("sell_submitted"))
		state["last_order_no"] = str(rt.get("last_order_no") or "")
		state["last_buy_order_no"] = str(rt.get("last_buy_order_no") or "")
		state["last_buy_order_price"] = int(rt.get("last_buy_order_price") or 0)
		state["last_buy_fill_price"] = int(rt.get("last_buy_fill_price") or 0)
		state["last_buy_fill_time"] = str(rt.get("last_buy_fill_time") or "")
		state["last_buy_order_at"] = str(rt.get("last_buy_order_at") or "")

		if not p:
			state["message"] = "대기 중… (전략이 없습니다)"
			_update_state(state)
			time.sleep(2.0)
			continue

		if not enabled:
			state["message"] = "대기 중… (전략이 비활성화되어 있습니다)"
			_update_state(state)
			time.sleep(2.0)
			continue

		# ============================================================
		# Multi-entry mode (분할매수/엔트리 리스트)
		# - buysell.py의 "전략 저장"은 엔트리 1건을 append 합니다.
		# - 각 엔트리는 고유 TP/SL을 보관하며, 체결되면 해당 TP/SL로 청산됩니다.
		# ============================================================
		try:
			entries = bot_store.load_entries()
		except Exception:
			entries = []
		if entries:
			live = (state["mode"] == "LIVE")
			now = datetime.now(TZ)

			# 토큰 확보(실패해도 enabled 유지)
			if live:
				if not token:
					try:
						token = api.get_token(api.APP_KEY, api.APP_SECRET)
						token_ok_at = bot_store.now_ts()
						_log("토큰 발급 성공")
					except Exception as e:
						token = None
						state["message"] = f"토큰 발급 실패(재시도 중): {e}"
						state["token_error"] = _parse_token_error(str(e))
						_update_state(state)
						time.sleep(3.0)
						continue

			# 처리 대상: 아직 닫히지 않은 엔트리 위주(최근 200)
			keep_n = 200
			active_entries = [e for e in entries if isinstance(e, dict)]
			if len(active_entries) > keep_n:
				active_entries = active_entries[-keep_n:]

			# (ticker, exchange)별로 unfilled/fills 캐시
			book_cache: dict[tuple[str, str], dict[str, Any]] = {}

			def _dedupe_rows(rows: list[dict], key_fields: list[str]) -> list[dict]:
				"""
				키움 조회가 환경에 따라 동일 row가 중복되는 케이스가 있어, key_fields 조합으로 중복 제거합니다.
				- 마지막 값을 우선(동일 키가 여러 번 나오면 최신 row로 overwrite)
				"""
				out: dict[str, dict] = {}
				for x in (rows or []):
					if not isinstance(x, dict):
						continue
					parts = []
					for k in key_fields:
						parts.append(str(api._first_non_empty(x, [k]) if k in x else x.get(k) or ""))
					key = "|".join(parts).strip()
					if not key:
						# 키가 비면 ord_no만이라도 사용
						key = str(api._first_non_empty(x, ["ord_no", "ordNo", "order_no"]) or "")
					if not key:
						continue
					out[key] = x
				return list(out.values())

			def _get_books(ticker6: str, ex: str) -> tuple[list[dict], list[dict]]:
				key = (ticker6, ex)
				if key in book_cache:
					b = book_cache[key]
					return (b.get("unfilled") or []), (b.get("fills") or [])
				unfilled: list[dict] = []
				fills: list[dict] = []
				if live and token:
					unfilled = api.fetch_unfilled_orders(token, api.ACCOUNT_NO, stk_cd=ticker6, stex_tp=ex) or []
					fills = api.fetch_fills(token, api.ACCOUNT_NO, stk_cd=ticker6, stex_tp=ex) or []
				# ✅ 중복 제거(동일 주문번호/시간/가격/수량이 반복되는 케이스 방지)
				unfilled = _dedupe_rows(
					unfilled,
					key_fields=["ord_no", "ordNo", "order_no", "tm", "time", "ord_tm", "ord_pric", "ord_uv", "ord_qty", "oso_qty"],
				)
				fills = _dedupe_rows(
					fills,
					key_fields=["ord_no", "ordNo", "order_no", "cntr_no", "cntr_tm", "ord_tm", "cntr_pric", "cntr_qty"],
				)
				book_cache[key] = {"unfilled": unfilled[-80:], "fills": fills[-80:]}
				return book_cache[key]["unfilled"], book_cache[key]["fills"]

			# 가격 캐시(최우선호가 대용)
			px_cache: dict[tuple[str, str], int] = {}

			def _get_cur_px(ticker6: str, ex: str, anchor: int) -> int:
				key = (ticker6, ex)
				if key in px_cache:
					return px_cache[key]
				if live and token:
					q = api.fetch_best_bid_ask(token, ticker6, stex_tp=ex)
					cur = int(q.get("best_ask") or q.get("best_bid") or 0)
					# state 표시용으로 마지막 값 갱신(가장 최근 호출값으로)
					state["last_bid"] = int(q.get("best_bid") or 0)
					state["last_ask"] = int(q.get("best_ask") or 0)
					state["last_price"] = int(cur or 0)
				else:
					prev_p = int(state.get("last_price") or 0)
					q = api.mock_quote_step(ticker6, prev_p, anchor_price=anchor)
					cur = int(q.get("best_ask") or q.get("best_bid") or 0)
					state["last_bid"] = int(q.get("best_bid") or 0)
					state["last_ask"] = int(q.get("best_ask") or 0)
					state["last_price"] = int(cur or 0)
				px_cache[key] = int(cur or 0)
				return px_cache[key]

			changed = False
			pos_total = 0
			open_cnt = 0
			pending_cnt = 0

			# 엔트리 순회(오래된 것부터 처리)
			for e in active_entries:
				st = str(e.get("status") or "PENDING").upper()
				if st in ("CLOSED", "CANCELLED"):
					continue

				ticker6 = str(e.get("ticker") or "").strip()
				if not ticker6:
					continue
				plan_exchange = str(e.get("exchange") or "AUTO").strip().upper()
				qty_e = _safe_int(e.get("qty"), 1)
				buy_px = _safe_int(e.get("buy_price"), 0)
				tp = _safe_int(e.get("take_profit"), 0)
				sl = _safe_int(e.get("stop_loss"), 0)

				# AUTO면 현재 시각 기준 거래소 선택.
				# 단, 이미 주문을 낸 엔트리는 해당 주문이 제출된 거래소로 "고정"해서 조회/표시 일관성을 보장합니다.
				ex_locked = ""
				if st in ("BUY_SUBMITTED", "FILLED", "SELL_SUBMITTED"):
					ex_locked = str(e.get("buy_exchange") or e.get("protect_exchange") or e.get("active_exchange") or "").strip().upper()
				if ex_locked in ("KRX", "NXT", "SOR"):
					ex, ex_reason, _ = (ex_locked, "locked", None)
				else:
					ex, ex_reason, _ = _pick_active_exchange(plan_exchange, now)
				e["active_exchange"] = ex
				e["exchange_reason"] = ex_reason

				cur = _get_cur_px(ticker6, ex, anchor=(buy_px or api._mock_base_price(ticker6)))

				if st in ("PENDING",):
					pending_cnt += 1
					if live:
						# 주문 시간 게이트(거래소별)
						live_order_time, _ = api._next_trading_window_start(ex, now)
						if now < live_order_time:
							continue
						if ex == "NXT":
							allow, next_at, _reason = api._nxt_new_order_allowed(now)
							if not allow:
								continue
						# 매수 1회 제출
						try:
							trde_tp = api._pick_live_trde_tp(ex, now)
							resp = api._place_buy_limit(token, ticker6, ex, qty_e, buy_px, trde_tp=trde_tp)
							ord_no = _extract_order_no(resp)
							e["status"] = "BUY_SUBMITTED"
							e["buy_ord_no"] = ord_no
							e["buy_exchange"] = ex
							e["buy_ord_at"] = datetime.now(TZ).isoformat(timespec="seconds")
							_log("엔트리 매수 주문 제출", exchange=ex, stk_cd=ticker6, ord_no=ord_no, px=buy_px)
							changed = True
						except Exception as err:
							# 장종료/미개장 등은 메시지만
							state["message"] = f"[LIVE] 엔트리 매수 주문 실패: {err}"
							_log(state["message"])
							continue
					else:
						# DRY_RUN: 가격 도달 시 체결
						if buy_px > 0 and cur > 0 and cur <= buy_px:
							e["status"] = "FILLED"
							e["buy_fill_price"] = int(buy_px)
							e["buy_fill_time"] = datetime.now(TZ).strftime("%H%M%S")
							changed = True

				st2 = str(e.get("status") or "").upper()
				if st2 in ("BUY_SUBMITTED",):
					open_cnt += 1
					ord_no = str(e.get("buy_ord_no") or "").strip()
					unfilled, fills = _get_books(ticker6, ex)
					u_has = _match_order(unfilled, ord_no)
					f_has = _match_order(fills, ord_no)
					if ord_no and (not u_has) and f_has:
						# 체결가/시간 추출
						fp = 0
						ft = ""
						for fx in (fills or []):
							if str(api._first_non_empty(fx, ["ord_no", "ordNo", "order_no"]) or "") == ord_no:
								fp = _safe_int(fx.get("cntr_pric") or fx.get("cntr_prc") or fx.get("cntr_uv") or 0, 0)
								ft = str(fx.get("ord_tm") or fx.get("cntr_tm") or fx.get("time") or fx.get("tm") or "")
								if fp:
									break
						e["status"] = "FILLED"
						e["buy_fill_price"] = int(fp or buy_px or 0)
						e["buy_fill_time"] = ft
						changed = True

				st3 = str(e.get("status") or "").upper()
				if st3 in ("FILLED", "SELL_SUBMITTED"):
					pos_total += int(qty_e or 0)

				if st3 == "FILLED":
					open_cnt += 1
					# (A) 자동감시 유사: 체결 직후 TP/SL을 "주문으로" 거래소에 등록(서버가 꺼져도 동작)
					# - 익절: 지정가 매도
					# - 손절: 스톱지정가(trde_tp=28, cond_uv 사용)
					# 서버가 살아있을 때는 한쪽 체결 시 다른 쪽을 취소해 OCO처럼 동작시킵니다.
					tp_ord = str(e.get("tp_ord_no") or "").strip()
					sl_ord = str(e.get("sl_ord_no") or "").strip()
					if live and token and (tp or sl) and (not tp_ord and not sl_ord):
						try:
							# 보호주문을 "현재 active exchange"로 등록
							e["protect_exchange"] = ex
							e["protect_set_at"] = datetime.now(TZ).isoformat(timespec="seconds")
							if tp and tp > 0:
								trde_tp_sell = api._pick_live_trde_tp(ex, now)
								r1 = api._place_sell_limit(token, ticker6, ex, qty_e, int(tp), trde_tp=trde_tp_sell)
								e["tp_ord_no"] = _extract_order_no(r1)
							if sl and sl > 0:
								r2 = api._place_sell_stop_limit(token, ticker6, ex, qty_e, int(sl))
								e["sl_ord_no"] = _extract_order_no(r2)
							changed = True
							_log("보호주문 등록", exchange=ex, stk_cd=ticker6, ord_no=f"tp={e.get('tp_ord_no')},sl={e.get('sl_ord_no')}")
						except Exception as err:
							# 보호주문 실패 시에는 아래 "소프트 감시"로 fallback
							state["message"] = f"[LIVE] 보호주문 등록 실패(소프트감시로 대체): {err}"
							_log(state["message"])

					# (B) 보호주문 체결 감시 + OCO 취소
					tp_ord = str(e.get("tp_ord_no") or "").strip()
					sl_ord = str(e.get("sl_ord_no") or "").strip()
					if live and token and (tp_ord or sl_ord):
						unfilled, fills = _get_books(ticker6, ex)
						tp_filled = bool(tp_ord and _match_order(fills, tp_ord))
						sl_filled = bool(sl_ord and _match_order(fills, sl_ord))
						if tp_filled or sl_filled:
							e["status"] = "CLOSED"
							e["closed_at"] = datetime.now(TZ).isoformat(timespec="seconds")
							e["close_reason"] = "익절(보호주문)" if tp_filled else "손절(보호주문)"
							# 반대편 주문 취소(OCO)
							try:
								if tp_filled and sl_ord and _match_order(unfilled, sl_ord):
									api._cancel_order(token, ord_no=sl_ord, stk_cd=ticker6, exchange=ex, cncl_qty=0)
								if sl_filled and tp_ord and _match_order(unfilled, tp_ord):
									api._cancel_order(token, ord_no=tp_ord, stk_cd=ticker6, exchange=ex, cncl_qty=0)
							except Exception:
								pass
							changed = True
							continue

					# (C) 소프트 감시(서버가 켜져 있을 때도 즉시 대응)
					# 보호주문이 없거나 등록 실패한 경우에만 사용합니다.
					should_sell = False
					sell_px = 0
					reason = ""
					if tp and cur >= tp:
						should_sell = True
						sell_px = tp
						reason = "익절"
					elif sl and cur <= sl:
						should_sell = True
						sell_px = sl
						reason = "손절"
					# 보호주문이 이미 등록되어 있으면 소프트 매도주문을 중복 제출하지 않습니다.
					if should_sell and (not (tp_ord or sl_ord)):
						if live:
							try:
								trde_tp = api._pick_live_trde_tp(ex, now)
								resp = api._place_sell_limit(token, ticker6, ex, qty_e, sell_px, trde_tp=trde_tp)
								sord = _extract_order_no(resp)
								e["status"] = "SELL_SUBMITTED"
								e["sell_ord_no"] = sord
								e["sell_ord_at"] = datetime.now(TZ).isoformat(timespec="seconds")
								_log("엔트리 매도 주문 제출", exchange=ex, stk_cd=ticker6, ord_no=sord, px=sell_px, reason=reason)
								changed = True
							except Exception as err:
								state["message"] = f"[LIVE] 엔트리 매도 주문 실패: {err}"
								_log(state["message"])
						else:
							e["status"] = "CLOSED"
							e["closed_at"] = datetime.now(TZ).isoformat(timespec="seconds")
							changed = True

				if str(e.get("status") or "").upper() == "SELL_SUBMITTED":
					open_cnt += 1
					ord_no = str(e.get("sell_ord_no") or "").strip()
					unfilled, fills = _get_books(ticker6, ex)
					u_has = _match_order(unfilled, ord_no)
					f_has = _match_order(fills, ord_no)
					if ord_no and (not u_has) and f_has:
						e["status"] = "CLOSED"
						e["closed_at"] = datetime.now(TZ).isoformat(timespec="seconds")
						changed = True

			# entries 저장
			if changed:
				try:
					bot_store.save_entries(entries)
				except Exception:
					pass

			# state에 엔트리/요약 포함
			state["entries"] = entries[-200:]
			state["position_qty"] = pos_total
			state["entries_summary"] = {
				"total": len(entries),
				"pending": pending_cnt,
				"open": open_cnt,
				"position_qty": pos_total,
			}

			# ✅ 종목별 미체결/체결 book을 함께 내려 UI가 필터/그룹핑 할 수 있도록 함
			# ✅ 거래소 간 중복 제거:
			# 동일 주문번호가 KRX/NXT 조회에 모두 섞여 내려오는 케이스가 있어,
			# '주문번호 → 올바른 거래소'를 결정한 뒤 나머지 book에서 제거합니다.
			def _ord_no(row: dict) -> str:
				return str(api._first_non_empty(row, ["ord_no", "ordNo", "order_no"]) or "").strip()

			def _norm_ex(v) -> str:
				s = str(v or "").strip().upper()
				if s in ("1", "KRX"):
					return "KRX"
				if s in ("2", "NXT"):
					return "NXT"
				if s in ("3", "SOR"):
					return "SOR"
				return s

			# 엔트리에서 주문번호→거래소 매핑(가장 신뢰)
			ord_to_ex: dict[str, str] = {}
			for e in active_entries:
				if not isinstance(e, dict):
					continue
				ex0 = _norm_ex(e.get("buy_exchange") or e.get("protect_exchange") or e.get("active_exchange") or "")
				for k in ("buy_ord_no", "tp_ord_no", "sl_ord_no", "sell_ord_no"):
					on = str(e.get(k) or "").strip()
					if on and ex0 in ("KRX", "NXT", "SOR"):
						ord_to_ex.setdefault(on, ex0)

			# row 자체에 stex_tp_txt/stex_tp가 있으면 그 값을 우선
			for (t6, ex), b in book_cache.items():
				ex = _norm_ex(ex)
				for row in (b.get("unfilled") or []) + (b.get("fills") or []):
					if not isinstance(row, dict):
						continue
					on = _ord_no(row)
					if not on:
						continue
					row_ex = _norm_ex(row.get("stex_tp_txt") or row.get("stex_tp") or "")
					if row_ex in ("KRX", "NXT", "SOR"):
						ord_to_ex[on] = row_ex
					else:
						ord_to_ex.setdefault(on, ex)

			# 제거 적용
			for (t6, ex), b in book_cache.items():
				ex = _norm_ex(ex)
				b["unfilled"] = [r for r in (b.get("unfilled") or []) if (_ord_no(r) == "" or ord_to_ex.get(_ord_no(r), ex) == ex)]
				b["fills"] = [r for r in (b.get("fills") or []) if (_ord_no(r) == "" or ord_to_ex.get(_ord_no(r), ex) == ex)]

			books = []
			tickers_set = set()
			for (t6, ex), b in book_cache.items():
				tickers_set.add(str(t6))
				books.append({
					"ticker": str(t6),
					"exchange": str(_norm_ex(ex)),
					"unfilled": (b.get("unfilled") or [])[-50:],
					"fills": (b.get("fills") or [])[-50:],
				})
			# 정렬: ticker -> exchange
			books.sort(key=lambda x: (x.get("ticker") or "", x.get("exchange") or ""))
			state["books"] = books
			state["tickers"] = sorted(list(tickers_set))

			# 표시용 unfilled/fills는 마지막으로 호출된 책(있다면)으로
			if book_cache:
				_last_key = list(book_cache.keys())[-1]
				state["active_exchange"] = _last_key[1]
				state["active_stk_cd"] = _last_key[0]
				state["unfilled"] = (book_cache[_last_key].get("unfilled") or [])[-50:]
				state["fills"] = (book_cache[_last_key].get("fills") or [])[-50:]

			if not state.get("message"):
				state["message"] = f"실행 중… (엔트리 {open_cnt}개 감시, 보유수량 {pos_total}주)"

			_update_state(state)
			time.sleep(float(os.getenv("KIWOOM_POLL_SEC") or "2.0"))
			continue

		ticker = str(p.get("ticker") or "").strip()
		plan_exchange = str(p.get("exchange") or "AUTO").strip().upper()
		qty = _safe_int(p.get("qty"), 1)
		buy_price = _safe_int(p.get("buy_price"), 0)
		stop_loss = _safe_int(p.get("stop_loss"), 0)
		take_profit = _safe_int(p.get("take_profit"), 0)

		# ✅ AUTO 거래소 선택(1차)
		exchange, ex_reason, ex_next = _pick_active_exchange(plan_exchange, datetime.now(TZ))
		state["active_exchange"] = exchange
		state["exchange_reason"] = ex_reason
		if ex_next is not None:
			state["next_exchange_check_at"] = ex_next.isoformat(timespec="seconds")

		stk_cd = api._format_stk_cd(ticker, exchange)
		state["active_stk_cd"] = stk_cd

		live = (state["mode"] == "LIVE")
		now = datetime.now(TZ)

		# ✅ 핵심: 장종료/미개장일 때는 "주문/시세" API를 호출하기 전에 먼저 게이트로 차단합니다.
		# return_code=20(장종료) 스팸을 근본적으로 방지합니다.
		if live:
			try:
				if exchange == "NXT":
					ok_ex, next_at, reason = api._nxt_new_order_allowed(now)
				else:
					ok_ex, next_at, reason = _krx_order_allowed(now)
			except Exception:
				ok_ex, next_at, reason = (True, None, "ok")

			if not ok_ex:
				# AUTO였는데 둘 다 불가한 경우는 pick_active_exchange가 next_at를 줍니다.
				# explicit(KRX/NXT)면 해당 거래소 다음 오픈까지 대기.
				if next_at is None:
					next_at = _next_open_for_exchange(exchange, now)
				state["message"] = f"[LIVE] 대기 중… (주문 불가: {reason}) → {next_at.strftime('%H:%M')} 재시도"
				_update_state(state)
				time.sleep(30.0)
				continue

		if live:
			# 토큰 확보(실패해도 enabled 상태 유지 → 다음 루프에서 재시도)
			if not token:
				try:
					token = api.get_token(api.APP_KEY, api.APP_SECRET)
					token_ok_at = bot_store.now_ts()
					_log("토큰 발급 성공")
				except Exception as e:
					token = None
					state["message"] = f"토큰 발급 실패(재시도 중): {e}"
					state["token_error"] = _parse_token_error(str(e))
					# 로그는 너무 자주 쌓이지 않도록(동일 메시지는 60초에 한 번만)
					now_ts = bot_store.now_ts()
					msg = str(state["message"])
					if (msg != last_token_err_msg) or (now_ts - last_token_err_at >= 60.0):
						_log(msg)
						last_token_err_msg = msg
						last_token_err_at = now_ts
					_update_state(state)
					time.sleep(3.0)
					continue

		# 현재가/호가
		try:
			if live and token:
				q = api.fetch_best_bid_ask(token, stk_cd, stex_tp=exchange)
				cur = int(q.get("best_ask") or q.get("best_bid") or 0)
				state["last_bid"] = int(q.get("best_bid") or 0)
				state["last_ask"] = int(q.get("best_ask") or 0)
				state["last_price"] = int(cur or 0)
			else:
				# DRY_RUN은 mock 시세
				prev_p = int(state.get("last_price") or 0)
				anchor = int(buy_price or api._mock_base_price(ticker))
				q = api.mock_quote_step(ticker, prev_p, anchor_price=anchor)
				cur = int(q.get("best_ask") or q.get("best_bid") or 0)
				state["last_bid"] = int(q.get("best_bid") or 0)
				state["last_ask"] = int(q.get("best_ask") or 0)
				state["last_price"] = int(cur or 0)
		except Exception as e:
			# 장종료/미개장(return_code=20)면 짧은 재시도 스팸 대신 다음 오픈까지 대기
			if live and _is_market_closed(e):
				next_at = _next_open_for_exchange(exchange, now)
				state["message"] = f"[LIVE] 시세/호가 조회 불가(장종료/미개장) → {next_at.strftime('%H:%M')} 재시도: {e}"
				_log(state["message"])
				_update_state(state)
				time.sleep(30.0)
				continue
			# 8005: 토큰 무효/만료 → 토큰 초기화 후 재발급 루프로
			if live and _is_token_invalid(e):
				token = None
				token_ok_at = None
				state["token_error"] = _parse_token_error(str(e))
				state["message"] = f"시세 조회 실패(토큰 무효/만료) → 토큰 재발급 시도: {e}"
				_log(state["message"])
				_update_state(state)
				time.sleep(1.0)
				continue
			state["message"] = f"시세 조회 실패(재시도): {e}"
			_log(state["message"])
			_update_state(state)
			time.sleep(2.0)
			continue

		# LIVE 주문 시간 게이트(거래소별)
		if live:
			live_order_time, _ = api._next_trading_window_start(exchange, now)
			if now < live_order_time:
				state["message"] = f"[LIVE] 대기 중… (주문 시작 {live_order_time.strftime('%H:%M')})"
				_update_state(state)
				time.sleep(2.0)
				continue

			# NXT 신규주문 보류 구간(예: 08:50~09:00)
			if exchange == "NXT":
				allow, next_at, reason = api._nxt_new_order_allowed(now)
				if not allow and next_at is not None:
					state["message"] = f"[LIVE] NXT 신규주문 불가({reason}) → {next_at.strftime('%H:%M')} 재시도"
					_update_state(state)
					time.sleep(2.0)
					continue

		# 미체결/체결 조회 (표시/상태 업데이트)
		try:
			if live and token:
				unfilled = api.fetch_unfilled_orders(token, api.ACCOUNT_NO, stk_cd=ticker, stex_tp=exchange)
				fills = api.fetch_fills(token, api.ACCOUNT_NO, stk_cd=ticker, stex_tp=exchange)
				state["unfilled"] = (unfilled or [])[-50:]
				state["fills"] = (fills or [])[-50:]
				# 체결이 이미 있는 경우(재시작 후 등) 마지막 매수 체결가/시간을 최대한 복원(항상 최신으로 갱신)
				lf = _latest_buy_fill(state["fills"], ticker=ticker)
				if lf:
					fp = _safe_int(lf.get("cntr_pric") or lf.get("cntr_prc") or lf.get("cntr_uv") or 0, 0)
					ft = str(lf.get("ord_tm") or lf.get("cntr_tm") or lf.get("time") or lf.get("tm") or "")
					if fp > 0 and fp != int(rt.get("last_buy_fill_price") or 0):
						rt["last_buy_fill_price"] = int(fp)
						rt["last_buy_fill_time"] = ft
						bot_store.save_runtime(rt)

				# ✅ 보조: "매수 주문 제출되었고 아직 포지션이 0"인데 ka10075가 비어있으면,
				# 러너가 알고 있는 마지막 매수 주문 정보를 미체결 목록에 합성해서 UI에 표시(UX 개선)
				if int(rt.get("position_qty") or 0) == 0 and bool(rt.get("buy_submitted")):
					ord_no = str(rt.get("last_buy_order_no") or "")
					if ord_no and not _match_order(state.get("unfilled"), ord_no):
						px = int(rt.get("last_buy_order_price") or 0)
						qt = int(qty or 0)
						if px > 0 and qt > 0:
							state["unfilled"] = [
								{
									"ord_no": ord_no,
									"io_tp_nm": "+매수",
									"ord_pric": str(px),
									"ord_qty": str(qt),
									"oso_qty": str(qt),
									"ord_tm": "",
									"stk_cd": str(ticker),
									"stk_nm": str(p.get("name") or ""),
									"stex_tp_txt": exchange,
									"note": "runner-local pending (ka10075 empty)",
								},
								*list(state.get("unfilled") or []),
							][:50]
		except Exception as e:
			if live and _is_token_invalid(e):
				token = None
				token_ok_at = None
				state["token_error"] = _parse_token_error(str(e))
				state["message"] = f"[LIVE] 체결/미체결 조회 실패(토큰 무효/만료) → 토큰 재발급 시도: {e}"
				_log(state["message"])
			else:
				# 조회 실패는 치명적이지 않게 표시만
				state["message"] = f"[LIVE] 체결/미체결 조회 실패(재시도): {e}"
				_log(state["message"])

		pos_qty = int(rt.get("position_qty") or 0)
		buy_sub = bool(rt.get("buy_submitted"))
		sell_sub = bool(rt.get("sell_submitted"))

		# 포지션이 이미 있으면 "새 매수가는 표시만 되고, 신규 매수는 진행하지 않음"을 메시지로 명확히
		if pos_qty > 0 and not state.get("message"):
			state["message"] = f"실행 중… (포지션 보유 {pos_qty}주 · 신규매수는 미실행, 익절/손절만 감시)"

		# 1) 매수 주문 제출(한 번만)
		if pos_qty == 0:
			if live:
				if not buy_sub:
					try:
						trde_tp = api._pick_live_trde_tp(exchange, now)
						resp = api._place_buy_limit(token, stk_cd, exchange, qty, buy_price, trde_tp=trde_tp)
						ord_no = _extract_order_no(resp)
						rt["buy_submitted"] = True
						rt["last_buy_order_no"] = ord_no
						rt["last_order_no"] = ord_no
						rt["last_buy_order_price"] = int(buy_price or 0)
						rt["last_buy_fill_price"] = int(rt.get("last_buy_fill_price") or 0)
						rt["last_buy_order_at"] = datetime.now(TZ).isoformat(timespec="seconds")
						bot_store.save_runtime(rt)
						state["message"] = f"[LIVE] 매수 주문 제출: {buy_price} x {qty} (exchange={exchange}, stk_cd={stk_cd}, ord_no={ord_no})"
						_log("매수 주문 제출", exchange=exchange, stk_cd=stk_cd, ord_no=ord_no)
					except Exception as e:
						# 장종료/미개장(return_code=20)면 스팸 재시도 대신 다음 오픈까지 대기.
						if _is_market_closed(e):
							# AUTO면 반대 거래소도 1회 시도
							fallback_used = False
							if plan_exchange == "AUTO":
								alt = "KRX" if exchange == "NXT" else "NXT"
								alt_stk = api._format_stk_cd(ticker, alt)
								try:
									trde_tp2 = api._pick_live_trde_tp(alt, now)
									resp2 = api._place_buy_limit(token, alt_stk, alt, qty, buy_price, trde_tp=trde_tp2)
									ord_no2 = _extract_order_no(resp2)
									rt["buy_submitted"] = True
									rt["last_buy_order_no"] = ord_no2
									rt["last_order_no"] = ord_no2
									rt["last_buy_order_price"] = int(buy_price or 0)
									rt["last_buy_order_at"] = datetime.now(TZ).isoformat(timespec="seconds")
									bot_store.save_runtime(rt)
									# 상태도 alt로 갱신
									exchange = alt
									stk_cd = alt_stk
									state["active_exchange"] = alt
									state["active_stk_cd"] = alt_stk
									state["exchange_reason"] = f"fallback-from-{('NXT' if alt=='KRX' else 'KRX')}"
									state["message"] = f"[LIVE] 매수 주문 제출: {buy_price} x {qty} (exchange={alt}, stk_cd={alt_stk}, ord_no={ord_no2})"
									_log("매수 주문 제출(fallback)", exchange=alt, stk_cd=alt_stk, ord_no=ord_no2)
									fallback_used = True
								except Exception as e2:
									# alt도 실패하면 대기
									pass
							if not fallback_used:
								next_at = _next_open_for_exchange(exchange, now)
								state["message"] = f"[LIVE] 매수 주문 불가(장종료/미개장) → {next_at.strftime('%H:%M')} 재시도: {e}"
								_log(state["message"])
								_update_state(state)
								# 대기 간격을 크게 (스팸 방지)
								time.sleep(30.0)
								continue

						# 종목코드/거래소 불일치(1902 등)면 AUTO일 때 반대 거래소로 1회 폴백
						if _is_symbol_not_found(e):
							fallback_used = False
							if plan_exchange == "AUTO":
								alt = "KRX" if exchange == "NXT" else "NXT"
								alt_stk = api._format_stk_cd(ticker, alt)
								try:
									trde_tp2 = api._pick_live_trde_tp(alt, now)
									resp2 = api._place_buy_limit(token, alt_stk, alt, qty, buy_price, trde_tp=trde_tp2)
									ord_no2 = _extract_order_no(resp2)
									rt["buy_submitted"] = True
									rt["last_buy_order_no"] = ord_no2
									rt["last_order_no"] = ord_no2
									rt["last_buy_order_price"] = int(buy_price or 0)
									rt["last_buy_order_at"] = datetime.now(TZ).isoformat(timespec="seconds")
									bot_store.save_runtime(rt)
									exchange = alt
									stk_cd = alt_stk
									state["active_exchange"] = alt
									state["active_stk_cd"] = alt_stk
									state["exchange_reason"] = "fallback-symbol-not-found"
									state["message"] = f"[LIVE] 매수 주문 제출(fallback): {buy_price} x {qty} (exchange={alt}, stk_cd={alt_stk}, ord_no={ord_no2})"
									_log("매수 주문 제출(fallback-1902)", exchange=alt, stk_cd=alt_stk, ord_no=ord_no2)
									fallback_used = True
								except Exception:
									pass
							if not fallback_used:
								state["message"] = f"[LIVE] 매수 주문 불가(종목/거래소 확인 필요) : {e}"
								_log(state["message"])
								_update_state(state)
								time.sleep(5.0)
								continue

						if _is_token_invalid(e):
							token = None
							token_ok_at = None
							state["token_error"] = _parse_token_error(str(e))
							state["message"] = f"[LIVE] 매수 주문 실패(토큰 무효/만료) → 토큰 재발급 시도: {e}"
							_log(state["message"])
						else:
							state["message"] = f"[LIVE] 매수 주문 실패(재시도): {e}"
							_log(state["message"])
						_update_state(state)
						time.sleep(2.0)
						continue

				# 1-2) 체결 확인
				ord_no = str(rt.get("last_buy_order_no") or "")
				u_has = _match_order(state.get("unfilled"), ord_no)
				f_has = _match_order(state.get("fills"), ord_no)
				if ord_no and (not u_has) and f_has:
					rt["position_qty"] = qty
					# 체결가 기록(가능하면 fills에서 ord_no 매칭)
					try:
						fp = 0
						ft = ""
						for fx in (state.get("fills") or []):
							if str(fx.get("ord_no") or fx.get("ordNo") or "") == ord_no:
								fp = _safe_int(fx.get("cntr_pric") or fx.get("cntr_prc") or fx.get("cntr_uv") or 0, 0)
								ft = str(fx.get("ord_tm") or fx.get("cntr_tm") or fx.get("time") or fx.get("tm") or "")
								if fp:
									break
						if fp > 0:
							rt["last_buy_fill_price"] = int(fp)
							rt["last_buy_fill_time"] = ft
					except Exception:
						pass
					bot_store.save_runtime(rt)
					state["message"] = f"[LIVE] 매수 체결 확인(ord_no={ord_no})"
					_log("매수 체결 확인", ord_no=ord_no)
			else:
				# DRY_RUN: 가격 도달 시 즉시 체결 처리
				cur = int(state.get("last_price") or 0)
				if cur and cur <= buy_price:
					rt["position_qty"] = qty
					bot_store.save_runtime(rt)
					state["message"] = f"[DRY_RUN] 매수 체결(현재가 {cur} <= 매수가 {buy_price})"

		# 2) 매수 후 익절/손절 감시 및 매도
		pos_qty = int(rt.get("position_qty") or 0)
		if pos_qty > 0:
			cur = int(state.get("last_price") or 0)
			should_sell = False
			sell_px = 0
			sell_reason = ""
			if take_profit and cur >= take_profit:
				should_sell = True
				sell_px = take_profit
				sell_reason = "익절"
			elif stop_loss and cur <= stop_loss:
				should_sell = True
				sell_px = stop_loss
				sell_reason = "손절"

			if live:
				if should_sell and (not sell_sub):
					try:
						trde_tp = api._pick_live_trde_tp(exchange, now)
						resp = api._place_sell_limit(token, stk_cd, exchange, qty, sell_px, trde_tp=trde_tp)
						ord_no = _extract_order_no(resp)
						rt["sell_submitted"] = True
						rt["last_sell_order_no"] = ord_no
						rt["last_order_no"] = ord_no
						bot_store.save_runtime(rt)
						state["message"] = f"[LIVE] 매도 주문 제출({sell_reason}): {sell_px} x {qty} (ord_no={ord_no})"
						_log("매도 주문 제출", reason=sell_reason, ord_no=ord_no, px=sell_px)
					except Exception as e:
						if _is_token_invalid(e):
							token = None
							token_ok_at = None
							state["token_error"] = _parse_token_error(str(e))
							state["message"] = f"[LIVE] 매도 주문 실패(토큰 무효/만료) → 토큰 재발급 시도: {e}"
							_log(state["message"])
						else:
							state["message"] = f"[LIVE] 매도 주문 실패(재시도): {e}"
							_log(state["message"])
						_update_state(state)
						time.sleep(2.0)
						continue

				# 매도 체결 확인
				ord_no = str(rt.get("last_sell_order_no") or "")
				u_has = _match_order(state.get("unfilled"), ord_no)
				f_has = _match_order(state.get("fills"), ord_no)
				if ord_no and (not u_has) and f_has:
					# 거래 종결: position 0, 플랜 비활성화
					rt["position_qty"] = 0
					bot_store.save_runtime(rt)
					plan_wrap["enabled"] = False
					bot_store.save_plan(plan_wrap)
					state["message"] = f"[LIVE] 매도 체결 확인(ord_no={ord_no}) → 거래 종결"
					_log("거래 종결", ord_no=ord_no)
			else:
				if should_sell:
					rt["position_qty"] = 0
					bot_store.save_runtime(rt)
					plan_wrap["enabled"] = False
					bot_store.save_plan(plan_wrap)
					state["message"] = f"[DRY_RUN] 매도 체결({sell_reason}) → 거래 종결"

			# 상태 저장
			state["position_qty"] = int(rt.get("position_qty") or 0)
			state["buy_submitted"] = bool(rt.get("buy_submitted"))
			state["sell_submitted"] = bool(rt.get("sell_submitted"))
			state["last_order_no"] = str(rt.get("last_order_no") or "")
			state["last_buy_order_no"] = str(rt.get("last_buy_order_no") or "")
			state["last_buy_order_price"] = int(rt.get("last_buy_order_price") or 0)
			state["last_buy_fill_price"] = int(rt.get("last_buy_fill_price") or 0)
			state["last_buy_fill_time"] = str(rt.get("last_buy_fill_time") or "")
			state["last_buy_order_at"] = str(rt.get("last_buy_order_at") or "")

			if not state.get("message"):
				state["message"] = f"실행 중… (exchange={exchange}, stk_cd={stk_cd})"

			# 상태 메시지 변화가 있으면 로그
			msg = str(state.get("message") or "")
			if msg and msg != last_status_msg:
				_log(msg)
				last_status_msg = msg

		_update_state(state)
		time.sleep(float(os.getenv("KIWOOM_POLL_SEC") or "2.0"))


if __name__ == "__main__":
	while True:
		try:
			run_forever()
		except KeyboardInterrupt:
			print("bot_runner 종료됨", flush=True)
			sys.exit(0)
		except Exception as e:
			# 치명적 예외로 프로세스가 죽는 걸 방지: 로그 남기고 재시작
			try:
				print(f"bot_runner 치명적 예외(재시작): {e}", flush=True)
			except Exception:
				pass
			try:
				bot_store.log_event({"msg": f"bot_runner 치명적 예외(재시작): {e}"})
			except Exception:
				pass
			time.sleep(3.0)

