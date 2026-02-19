from __future__ import annotations

from flask import Flask, jsonify, render_template, request
import ast
import csv
import io
import json
import os
import re
import random
import threading
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests

import bot_store

try:
	# python-dotenv (선택 의존성): .env 파일에서 환경변수 로드
	from dotenv import load_dotenv  # type: ignore
	load_dotenv()
except Exception:
	pass


HOST = os.getenv("KIWOOM_HOST") or "https://api.kiwoom.com"
TZ = ZoneInfo(os.getenv("KIWOOM_TZ") or "Asia/Seoul")

APP_KEY = os.getenv("KIWOOM_APP_KEY") or os.getenv("APP_KEY") or ""
APP_SECRET = os.getenv("KIWOOM_APP_SECRET") or os.getenv("APP_SECRET") or ""

# 주문(실거래) 관련 환경변수
# - 실거래는 안전장치(2중)로 기본 비활성화
ACCOUNT_NO_RAW = os.getenv("KIWOOM_ACCOUNT_NO") or os.getenv("ACCOUNT_NO") or ""
ACCOUNT_NO = re.sub(r"\D", "", ACCOUNT_NO_RAW)
DEFAULT_EXCHANGE = (os.getenv("KIWOOM_DMST_STEX_TP") or "KRX").strip().upper()  # KRX,NXT,SOR
DRY_RUN = (os.getenv("KIWOOM_DRY_RUN") or "1").strip() not in ("0", "false", "False")
ENABLE_LIVE = (os.getenv("KIWOOM_ENABLE_LIVE_TRADING") or "").strip().upper() == "YES"

# 시세/차트(마켓데이터) 기본 동작
# - DRY_RUN에서는 키움 토큰/지정단말기 인증(8050) 없이도 "실제에 가까운" OHLCV가 보이도록
#   기본값을 PUBLIC(네이버/스투크)로 둡니다.
# - 강제로 키움 마켓데이터를 쓰려면: KIWOOM_MARKETDATA_PROVIDER=KIWOOM
# - 강제로 mock을 쓰려면: KIWOOM_MARKETDATA_PROVIDER=MOCK
USE_KIWOOM_MARKETDATA = (os.getenv("KIWOOM_USE_KIWOOM_MARKETDATA") or ("1" if not DRY_RUN else "0")).strip() not in ("0", "false", "False")  # (구버전 호환)
MARKETDATA_PROVIDER = (os.getenv("KIWOOM_MARKETDATA_PROVIDER") or ("PUBLIC" if DRY_RUN else "KIWOOM")).strip().upper()
ALLOW_MOCK_FALLBACK = (os.getenv("KIWOOM_ALLOW_MOCK_FALLBACK") or "1").strip() not in ("0", "false", "False")

# “오전 8시부터” 요청 반영 (기본값 08:00). 필요시 .env에서 변경
MARKET_OPEN_HHMM = (os.getenv("KIWOOM_MARKET_OPEN") or "08:00").strip()
# LIVE 주문 제출 시작 시각(기본: 장 시작 시각과 동일).
# - 장 전부터 모니터링은 가능하지만, "주문 제출"은 이 시각부터 하도록 게이트를 둡니다.
# - 필요 시 .env에서 KIWOOM_LIVE_ORDER_START=09:00 처럼 변경하세요.
LIVE_ORDER_START_HHMM = (os.getenv("KIWOOM_LIVE_ORDER_START") or MARKET_OPEN_HHMM).strip()
# KRX 장전(시간외) 주문 구간: 이 구간엔 장전용 주문구분을 자동 사용
KRX_PREMARKET_START_HHMM = (os.getenv("KIWOOM_KRX_PREMARKET_START") or "08:30").strip()
KRX_PREMARKET_END_HHMM = (os.getenv("KIWOOM_KRX_PREMARKET_END") or "09:00").strip()
# NXT(대체거래소) 기본 거래시간(참고용 게이트)
# - 일반적으로 08:00~08:50 프리마켓, 08:50~09:00 시가결정 구간은 '신규주문 불가(취소만 가능)'인 경우가 있어
#   이 시간대엔 주문 제출을 보류하고 09:00에 재시도합니다.
NXT_MARKET_START_HHMM = (os.getenv("KIWOOM_NXT_MARKET_START") or "08:00").strip()
NXT_NEW_ORDER_PAUSE_START_HHMM = (os.getenv("KIWOOM_NXT_NEW_ORDER_PAUSE_START") or "08:50").strip()
NXT_NEW_ORDER_PAUSE_END_HHMM = (os.getenv("KIWOOM_NXT_NEW_ORDER_PAUSE_END") or "09:00").strip()
# NXT 추가 보류 구간(기본: 장중 종가결정/전환 구간을 넓게 회피)
NXT_NEW_ORDER_PAUSE2_START_HHMM = (os.getenv("KIWOOM_NXT_NEW_ORDER_PAUSE2_START") or "15:20").strip()
NXT_NEW_ORDER_PAUSE2_END_HHMM = (os.getenv("KIWOOM_NXT_NEW_ORDER_PAUSE2_END") or "15:40").strip()
NXT_MARKET_END_HHMM = (os.getenv("KIWOOM_NXT_MARKET_END") or "20:00").strip()
POLL_INTERVAL_SEC = float(os.getenv("KIWOOM_POLL_SEC") or "2.0")


app = Flask(__name__)

# 키움 API 호출은 시스템/환경 프록시를 타지 않도록 별도 세션 사용
_KIWOOM_SESSION = requests.Session()
_KIWOOM_SESSION.trust_env = False


def _is_kiwoom_url(url: str) -> bool:
	try:
		host = (urlparse(url).hostname or "").lower()
	except Exception:
		host = ""
	return host.endswith("kiwoom.com")


def _http_get(url: str, **kwargs):
	if _is_kiwoom_url(url):
		return _KIWOOM_SESSION.get(url, **kwargs)
	return requests.get(url, **kwargs)


def _http_post(url: str, **kwargs):
	if _is_kiwoom_url(url):
		return _KIWOOM_SESSION.post(url, **kwargs)
	return requests.post(url, **kwargs)


def _format_stk_cd(base_6: str, exchange: str) -> str:
	"""
	키움 REST에서 종목코드는 기본적으로 6자리 숫자입니다.
	과거에는 NXT/SOR에 대해 "005930_NX" 같은 suffix를 붙여 시도했지만,
	실제 주문(TR=kt10000)에서 `1902: 종목 정보가 없습니다`가 발생하는 케이스가 확인되어
	기본 동작은 항상 6자리 코드로 정규화합니다.

	거래소는 별도 입력값(dmst_stex_tp / stex_tp)으로 전달합니다.
	"""
	# 입력이 "005930_NX" 같은 형태여도 6자리로 정규화
	digits6 = re.sub(r"\D", "", str(base_6 or ""))[:6]
	return digits6 or str(base_6 or "").strip()


# ============================================================
# 종목명 -> 종목코드(6자리) 변환 (KRX KIND 다운로드 HTML 테이블 파싱)
# ============================================================
_krx_cache = {
	"loaded_at": None,   # datetime
	"by_name": None,     # dict[str, str] normalized_name -> code
	"name_by_code": None # dict[str, str] code -> original_name
}


def _norm_name(s: str) -> str:
	s = (s or "").strip().lower()
	s = re.sub(r"\s+", "", s)
	s = re.sub(r"[·\.\,\(\)\[\]\-_/]", "", s)
	return s


def _load_krx_name_map(force=False):
	now = datetime.now(TZ)
	if not force and _krx_cache["loaded_at"] and _krx_cache["by_name"] and _krx_cache["name_by_code"]:
		if (now - _krx_cache["loaded_at"]).total_seconds() < 24 * 3600:
			return

	url = "https://kind.krx.co.kr/corpgeneral/corpList.do?method=download"
	r = _http_get(url, timeout=25, headers={"User-Agent": "Mozilla/5.0"})
	r.raise_for_status()

	raw = r.content
	text = None
	for enc in ("utf-8-sig", "cp949", "euc-kr", "utf-8"):
		try:
			text = raw.decode(enc)
			break
		except Exception:
			continue
	if text is None:
		text = raw.decode("utf-8", errors="ignore")

	class _KRXTableParser(HTMLParser):
		def __init__(self):
			super().__init__()
			self.rows: list[list[str]] = []
			self._in_tr = False
			self._in_cell = False
			self._cell_buf: list[str] = []
			self._cur_row: list[str] = []

		def handle_starttag(self, tag, attrs):
			t = tag.lower()
			if t == "tr":
				self._in_tr = True
				self._cur_row = []
			elif self._in_tr and t in ("td", "th"):
				self._in_cell = True
				self._cell_buf = []

		def handle_data(self, data):
			if self._in_cell:
				self._cell_buf.append(data)

		def handle_endtag(self, tag):
			t = tag.lower()
			if t in ("td", "th") and self._in_cell:
				txt = unescape("".join(self._cell_buf))
				txt = re.sub(r"\s+", " ", txt).strip()
				self._cur_row.append(txt)
				self._in_cell = False
				self._cell_buf = []
			elif t == "tr" and self._in_tr:
				if any(c.strip() for c in self._cur_row):
					self.rows.append(self._cur_row)
				self._in_tr = False
				self._cur_row = []

	parser = _KRXTableParser()
	parser.feed(text)
	rows = parser.rows
	if not rows:
		raise RuntimeError("KRX 종목 목록을 가져왔지만 파싱된 행이 없습니다.")

	header = rows[0]

	def _col_idx(candidates):
		for cand in candidates:
			for i, h in enumerate(header):
				if h.replace(" ", "") == cand.replace(" ", ""):
					return i
		return None

	i_name = _col_idx(["회사명", "종목명"])
	i_code = _col_idx(["종목코드"])
	if i_name is None or i_code is None:
		raise RuntimeError(f"KRX 목록 헤더 파싱 실패: {header[:12]}")

	by_name: dict[str, str] = {}
	name_by_code: dict[str, str] = {}
	for r0 in rows[1:]:
		if len(r0) <= max(i_name, i_code):
			continue
		name = (r0[i_name] or "").strip()
		code = (r0[i_code] or "").strip()
		if not name or not code:
			continue
		code = re.sub(r"\D", "", code).zfill(6)
		if not re.fullmatch(r"\d{6}", code):
			continue
		by_name[_norm_name(name)] = code
		name_by_code[code] = name

	_krx_cache["loaded_at"] = now
	_krx_cache["by_name"] = by_name
	_krx_cache["name_by_code"] = name_by_code


def resolve_ticker(query: str) -> tuple[str | None, str | None, str | None]:
	"""
	return: (ticker, name, err)
	"""
	q = (query or "").strip()
	if re.fullmatch(r"\d{6}", q):
		_load_krx_name_map()
		name = (_krx_cache.get("name_by_code") or {}).get(q, "")
		return (q, name, None)

	_load_krx_name_map()
	by_name = _krx_cache["by_name"] or {}
	nq = _norm_name(q)
	if not nq:
		return (None, None, "종목명을 다시확인해 주세요")

	if nq in by_name:
		code = by_name[nq]
		name = (_krx_cache.get("name_by_code") or {}).get(code, "")
		return (code, name, None)

	cands = [(name_norm, code) for (name_norm, code) in by_name.items() if nq in name_norm]
	if len(cands) == 1:
		code = cands[0][1]
		name = (_krx_cache.get("name_by_code") or {}).get(code, "")
		return (code, name, None)

	return (None, None, "종목명을 다시확인해 주세요")


@app.get("/api/resolve-ticker")
def api_resolve_ticker():
	q = (request.args.get("q") or "").strip()
	try:
		ticker, name, err = resolve_ticker(q)
		if err or not ticker:
			return jsonify({"ok": False, "error": err or "종목명을 다시확인해 주세요"}), 404
		return jsonify({"ok": True, "ticker": ticker, "name": name or ""})
	except Exception as e:
		return jsonify({"ok": False, "error": "ResolverError", "detail": str(e)}), 500


# ============================================================
# Kiwoom REST helpers
# ============================================================
def _hint_for_token_error(data: dict) -> str:
	try:
		rm = str(data.get("return_msg") or "")
		rc = str(data.get("return_code") or "")
	except Exception:
		return ""
	if "8050" in rm or "지정단말기" in rm:
		return " (힌트: 키움 API센터/HTS에서 지정단말기 인증이 필요합니다. DRY_RUN이면 KIWOOM_USE_KIWOOM_MARKETDATA=0로 두면 토큰 없이도 동작합니다.)"
	if rc and rc != "0":
		return " (힌트: APP_KEY/APP_SECRET 및 키움 API 권한/단말기 상태를 확인하세요.)"
	return ""


def get_token(appkey: str, secretkey: str) -> str:
	url = HOST + "/oauth2/token"
	body = {"grant_type": "client_credentials", "appkey": appkey, "secretkey": secretkey}
	r = _http_post(url, json=body, headers={"Content-Type": "application/json;charset=UTF-8"}, timeout=20)
	r.raise_for_status()
	data = r.json()
	# 키움은 HTTP 200이어도 return_code로 실패가 내려올 수 있음
	if isinstance(data, dict) and "return_code" in data:
		try:
			rc = int(data.get("return_code"))
		except Exception:
			rc = None
		if rc not in (None, 0):
			rm = str(data.get("return_msg") or data)
			raise RuntimeError(f"Token error: {rm} (return_code={data.get('return_code')}){_hint_for_token_error(data)}")
	if "token" in data:
		return data["token"]
	if "access_token" in data:
		return data["access_token"]
	raise RuntimeError(f"Token error: {data}{_hint_for_token_error(data if isinstance(data, dict) else {})}")


def _mock_base_price(stk_cd_6: str) -> int:
	code = re.sub(r"\D", "", stk_cd_6 or "")
	try:
		n = int(code) if code else 5930
	except Exception:
		n = 5930
	# 너무 작은/큰 값이 나오지 않게 범위 제한
	return 10_000 + (n % 80_000)


def mock_ohlcv_90d(stk_cd_6: str, days=90):
	"""
	키움 토큰이 없거나(또는 DRY_RUN 기본) 키움 API 인증이 실패했을 때 UI가 깨지지 않도록
	90일 OHLCV를 "그럴듯한" 랜덤 워크로 생성합니다.
	"""
	days = int(days or 90)
	days = max(10, min(200, days))
	base = _mock_base_price(stk_cd_6)
	rng = random.Random(int(re.sub(r"\D", "", stk_cd_6 or "0") or "0") or 5930)
	out = []
	cur = float(base)
	now = datetime.now(TZ)
	dt = now
	# 주말은 건너뛰되, 90개가 찰 때까지 충분히 생성
	while len(out) < days:
		dt = dt - timedelta(days=1)
		if dt.weekday() >= 5:
			continue
		# 일간 변동폭(±2%)
		ret = rng.uniform(-0.02, 0.02)
		close_p = max(1.0, cur * (1.0 + ret))
		high_p = max(close_p, cur) * (1.0 + rng.uniform(0.0, 0.01))
		low_p = min(close_p, cur) * (1.0 - rng.uniform(0.0, 0.01))
		open_p = cur
		vol = int(max(1, rng.gauss(2_500_000, 900_000)))
		out.append(
			{
				"dt": dt.strftime("%Y-%m-%d"),
				"open": int(round(open_p)),
				"high": int(round(high_p)),
				"low": int(round(low_p)),
				"close": int(round(close_p)),
				"volume": int(vol),
			}
		)
		cur = close_p
	out.sort(key=lambda x: x["dt"], reverse=True)
	return {"ticker": re.sub(r"\D", "", stk_cd_6 or "").zfill(6), "from": out[-1]["dt"], "to": out[0]["dt"], "days": len(out), "ohlcv": out}


def mock_best_bid_ask(stk_cd_6: str, ref_price: int | None = None) -> dict:
	p = int(ref_price or _mock_base_price(stk_cd_6))
	# 간단 스프레드(0.02% 수준)
	spread = max(1, int(p * 0.0002))
	return {"best_bid": max(1, p - spread), "best_ask": p + spread, "raw": {"source": "MOCK"}}


def mock_quote_step(stk_cd_6: str, prev_price: int | None, anchor_price: int | None = None) -> dict:
	"""
	봇(DRY_RUN)에서 키움 토큰이 없을 때 쓰는 "1-step" 시세 시뮬레이터.
	- prev_price를 기준으로 아주 작은 변동(±0.25%)을 주고 bid/ask를 만들어 반환합니다.
	"""
	anchor = int(anchor_price or _mock_base_price(stk_cd_6))
	prev = int(prev_price or 0)
	base = prev if prev > 0 else anchor
	seed = int(datetime.now(TZ).timestamp()) ^ int(re.sub(r"\D", "", stk_cd_6 or "0") or "0")
	rng = random.Random(seed)
	mult = 1.0 + rng.uniform(-0.0025, 0.0025)
	cur = max(1, int(round(base * mult)))
	return mock_best_bid_ask(stk_cd_6, ref_price=cur)


def _yyyymmdd_to_yyyy_mm_dd(s: str) -> str | None:
	ss = re.sub(r"\D", "", str(s or ""))
	if len(ss) != 8:
		return None
	return f"{ss[:4]}-{ss[4:6]}-{ss[6:8]}"


def fetch_ohlcv_90d_public(stk_cd_6: str, days=90) -> dict:
	"""
	키움 토큰이 없어도 "실제 데이터" OHLCV를 보여주기 위한 공개 소스 폴백.
	- 1순위: 네이버 fchart (일봉, OHLCV+거래량)
	- 2순위: stooq CSV
	"""
	days = int(days or 90)
	days = max(10, min(200, days))
	code = re.sub(r"\D", "", stk_cd_6 or "").zfill(6)

	def _naver():
		now = datetime.now(TZ)
		end = now.strftime("%Y%m%d")
		start = (now - timedelta(days=260)).strftime("%Y%m%d")
		url = (
			"https://fchart.stock.naver.com/siseJson.naver"
			f"?symbol={code}&requestType=1&startTime={start}&endTime={end}&timeframe=day"
		)
		r = _http_get(
			url,
			timeout=20,
			headers={
				"User-Agent": "Mozilla/5.0",
				"Referer": "https://finance.naver.com/",
			},
		)
		r.raise_for_status()
		txt = (r.text or "").strip()
		m = re.search(r"\[[\s\S]*\]", txt)
		if not m:
			raise RuntimeError(f"NAVER fchart parse failed: {txt[:120]}")
		raw = m.group(0)
		# 네이버 fchart는 JSON이 아니라 "파이썬 리스트처럼 보이는" 문자열(작은따옴표)인 경우가 흔합니다.
		# - 최대한 안전하게 literal_eval로 파싱합니다.
		raw = re.sub(r",\s*\]", "]", raw)  # trailing comma 방어
		raw2 = raw.replace("null", "None")
		try:
			data = json.loads(raw)  # 혹시라도 JSON으로 오는 케이스
		except Exception:
			data = ast.literal_eval(raw2)
		if not isinstance(data, list) or len(data) < 2:
			raise RuntimeError("NAVER fchart returned empty list")

		out = []
		for row in data[1:]:
			if not isinstance(row, list) or len(row) < 6:
				continue
			dt = _yyyymmdd_to_yyyy_mm_dd(str(row[0]))
			if not dt:
				continue
			o = _to_int(row[1], 0)
			hi = _to_int(row[2], 0)
			lo = _to_int(row[3], 0)
			c = _to_int(row[4], 0)
			v = _to_int(row[5], 0)
			if max(o, hi, lo, c) <= 0:
				continue
			out.append({"dt": dt, "open": o, "high": hi, "low": lo, "close": c, "volume": v})

		out.sort(key=lambda x: x["dt"])
		# 중복 날짜 제거(마지막 값 우선)
		uniq = {}
		for x in out:
			uniq[x["dt"]] = x
		out2 = [uniq[k] for k in sorted(uniq.keys())]
		out2 = out2[-days:]
		if not out2:
			raise RuntimeError("NAVER fchart parsed rows=0")
		return {
			"ticker": code,
			"from": out2[0]["dt"],
			"to": out2[-1]["dt"],
			"days": len(out2),
			"ohlcv": list(reversed(out2)),  # 기존 API는 최신이 앞
			"meta": {"used_api": "NAVER_FCHART", "used_endpoint": url, "raw_rows": len(out), "pages": 1},
		}

	def _stooq():
		# KOSPI(.KS) → KOSDAQ(.KQ) 순으로 시도
		symbols = [f"{code}.KS", f"{code}.KQ"]
		last_err: Exception | None = None
		for sym in symbols:
			url = f"https://stooq.com/q/d/l/?s={sym.lower()}&i=d"
			try:
				r = _http_get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
				r.raise_for_status()
				text = (r.text or "").strip()
				reader = csv.DictReader(io.StringIO(text))
				fns = [x.strip() for x in (reader.fieldnames or []) if x]
				# HTML/차단 응답 등으로 헤더가 없거나 기대 포맷이 아니면 실패 처리
				if not fns or ("Date" not in fns) or ("Close" not in fns):
					raise RuntimeError(f"STOOQ unexpected response for {sym}")
				rows = []
				for rr in reader:
					dt = str(rr.get("Date") or "").strip()
					if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", dt):
						continue
					o = _to_int(rr.get("Open"), 0)
					hi = _to_int(rr.get("High"), 0)
					lo = _to_int(rr.get("Low"), 0)
					c = _to_int(rr.get("Close"), 0)
					v = _to_int(rr.get("Volume"), 0)
					if max(o, hi, lo, c) <= 0:
						continue
					rows.append({"dt": dt, "open": o, "high": hi, "low": lo, "close": c, "volume": v})
				rows.sort(key=lambda x: x["dt"])
				rows = rows[-days:]
				if not rows:
					raise RuntimeError(f"STOOQ rows=0 for {sym}")
				return {
					"ticker": code,
					"from": rows[0]["dt"],
					"to": rows[-1]["dt"],
					"days": len(rows),
					"ohlcv": list(reversed(rows)),  # 최신이 앞
					"meta": {"used_api": "STOOQ", "used_endpoint": url, "raw_rows": len(rows), "pages": 1, "symbol": sym},
				}
			except Exception as e:
				last_err = e
				continue
		raise last_err if last_err else RuntimeError("STOOQ fetch failed")

	# provider 내부 우선순위
	try:
		return _naver()
	except Exception:
		return _stooq()


def _to_int(v, default=0):
	if v is None:
		return default
	if isinstance(v, int):
		return v
	s = str(v).strip()
	if s == "":
		return default
	s = s.replace(",", "")
	try:
		return int(s)
	except Exception:
		return default


def _parse_dt_any(v):
	if v is None:
		return None
	s = str(v).strip()
	if s == "":
		return None
	# YYYYMMDD
	if len(s) >= 8 and s[:8].isdigit():
		y = s[:4]
		m = s[4:6]
		d = s[6:8]
		return f"{y}-{m}-{d}"
	# YYYY-MM-DD
	if len(s) >= 10 and s[4] == "-" and s[7] == "-":
		return s[:10]
	return None


def _pick_list_rows(res_json):
	if not isinstance(res_json, dict):
		return []
	for k in (
		"stk_invsr_orgn_chart",
		"stk_frgnr",
		"output",
		"output1",
		"output2",
		"daly_stkpc",
		"stk_daly_prc",
		# 계좌/주문/체결 계열(가변)
		"ord_list",
		"cntr_list",
		"not_cntr_list",
		"unfilled",
		"filled",
	):
		if k in res_json and isinstance(res_json[k], list):
			return res_json[k]
	for k, v in res_json.items():
		if k in ("return_code", "return_msg"):
			continue
		if isinstance(v, list) and (len(v) == 0 or isinstance(v[0], dict)):
			return v
	return []


def _first_non_empty(d: dict, keys: list[str]):
	if not isinstance(d, dict):
		return None
	for k in keys:
		if k in d and str(d.get(k)).strip() != "":
			return d.get(k)
	return None


def fetch_best_bid_ask(token: str, stk_cd: str, stex_tp: str | None = None) -> dict:
	"""
	호가(주식호가요청 ka10004)
	- endpoint: /api/dostk/mrkcond (시세)
	- 주요 키: buy_fpr_bid(매수최우선호가), sel_fpr_bid(매도최우선호가)
	"""
	body = {"stk_cd": _format_stk_cd(stk_cd, stex_tp or DEFAULT_EXCHANGE or "KRX")}
	# NXT/SOR 환경에서 거래소 구분을 요구하는 케이스 대비
	if stex_tp:
		st = str(stex_tp).strip().upper()
		body["stex_tp"] = st
		body["dmst_stex_tp"] = st
	r = call_tr(token, api_id="ka10004", body=body, endpoint="/api/dostk/mrkcond", timeout=15)
	j = r.json() if hasattr(r, "json") else {}
	if not isinstance(j, dict):
		j = {}
	best_bid = _to_int(j.get("buy_fpr_bid") or j.get("buy_fpr") or j.get("buy_1th_pre_bid"), 0)
	best_ask = _to_int(j.get("sel_fpr_bid") or j.get("sel_fpr") or j.get("sel_1th_pre_bid"), 0)
	return {"best_bid": best_bid, "best_ask": best_ask, "raw": j}


def fetch_unfilled_orders(
	token: str,
	acnt_no: str,
	stk_cd: str | None = None,
	stex_tp: str | None = None,
) -> list[dict]:
	"""
	미체결 조회(ka10075) - 필드가 공개 문서에서 직접 확인이 어려워,
	REST에서 자주 쓰이는 snake_case 파라미터로 구현 + 실패 시 에러 메시지 노출.
	"""
	# 키움(OPT10075/ka10075) 입력값 규칙(대표):
	# - all_stk_tp: 전체종목구분 ("0": 전체, "1": 종목)
	# - trde_tp: 매매구분 ("0": 전체, "1": 매도, "2": 매수)
	# - stk_cd: 종목코드 (all_stk_tp="1"일 때 사용)
	# - ccld_tp: 체결구분 ("0": 전체, "1": 미체결, "2": 체결)
	raw_code = (stk_cd or "").strip()
	# NXT/SOR 주문은 종목코드가 "005930_NX" / "005930_AL" 형태로 들어갈 수 있어
	# 계좌 조회에서도 동일 표기를 요구하는 경우를 대비해 여러 후보를 시도합니다.
	digits6 = re.sub(r"\D", "", raw_code)[:6]
	code_candidates: list[str] = []
	if digits6:
		code_candidates.append(digits6)
	stex_tp_norm = (stex_tp or DEFAULT_EXCHANGE or "KRX").strip().upper()
	if stex_tp_norm == "NXT" and digits6:
		code_candidates.append(f"{digits6}_NX")
	if stex_tp_norm == "SOR" and digits6:
		code_candidates.append(f"{digits6}_AL")
	# 사용자가 이미 suffix 포함 값을 넣은 경우(혹시나)도 포함
	if raw_code and raw_code not in code_candidates:
		code_candidates.append(raw_code)

	# 종목 지정 조회면 all_stk_tp=1
	all_stk_tp = "1" if code_candidates else "0"
	# ka10075에서 stex_tp(거래소구분)를 필수로 요구하는 케이스가 있어 포함
	# - 주문 거래소(KRX/NXT/SOR)에 맞춰 조회해야 NXT 주문/체결이 화면에 표시됩니다.
	stex_tp = stex_tp_norm
	# 조회구분(qry_tp): 문서/구현에 따라 필수로 요구되는 경우가 있어 기본값(0) 포함
	# - 값 규칙은 계좌/주문체결 조회에서 보통 "0"을 기본으로 사용
	qry_tp = "0"
	base = {
		# ✅ 필수: all_stk_tp (기존 stk_tp는 호환용으로 남김)
		"all_stk_tp": all_stk_tp,
		"stk_tp": all_stk_tp,
		# ✅ 필수로 요구될 수 있는 거래소 구분
		"stex_tp": stex_tp,
		"dmst_stex_tp": stex_tp,  # 주문 TR과의 키명 차이 방어
		# ✅ 필수로 요구될 수 있는 조회구분
		"qry_tp": qry_tp,
		"qryTp": qry_tp,
		"trde_tp": "0",     # 0:전체, 1:매도, 2:매수
		# stk_cd는 아래에서 후보를 돌며 주입
		"stk_cd": "",
		"ccld_tp": "1",     # 1:미체결
	}
	# ✅ 실패 원인(1511/all_stk_tp 누락) 방지:
	# - 어떤 body 변형을 쓰더라도 all_stk_tp를 항상 포함
	# - 계좌번호 키도 여러 후보로 시도(문서/예제/서버 구현 차이 방어)
	bodies = []
	for c in (code_candidates or [""]):
		base2 = {**base, "stk_cd": c}
		for acct_key in ("acctNo", "acnt_no", "acct_no", "acntNo"):
			bodies.append({acct_key: acnt_no, **base2})
		# 일부 구현이 camelCase를 기대할 수도 있어 함께 제공(단, 원본 all_stk_tp는 유지)
		bodies.append({"acctNo": acnt_no, "allStkTp": all_stk_tp, **base2})
		# “혹시 어느 키를 보든 잡히게” 모두 넣은 fat body 1개
		bodies.append({"acctNo": acnt_no, "acnt_no": acnt_no, "acct_no": acnt_no, "acntNo": acnt_no, "allStkTp": all_stk_tp, **base2})
	last = None
	for body in bodies:
		try:
			r = call_tr(token, api_id="ka10075", body=body, endpoint="/api/dostk/acnt", timeout=20)
			j = r.json()
			rows = _pick_list_rows(j)
			return rows if isinstance(rows, list) else []
		except Exception as e:
			last = e
	if last:
		raise last
	return []


def fetch_fills(
	token: str,
	acnt_no: str,
	stk_cd: str | None = None,
	stex_tp: str | None = None,
) -> list[dict]:
	"""
	체결 조회(ka10076)
	"""
	# 키움(OPT10076/ka10076)도 all_stk_tp(전체종목구분)가 필수로 요구되는 경우가 있음
	raw_code = (stk_cd or "").strip()
	digits6 = re.sub(r"\D", "", raw_code)[:6]
	code_candidates: list[str] = []
	if digits6:
		code_candidates.append(digits6)
	stex_tp_norm = (stex_tp or DEFAULT_EXCHANGE or "KRX").strip().upper()
	if stex_tp_norm == "NXT" and digits6:
		code_candidates.append(f"{digits6}_NX")
	if stex_tp_norm == "SOR" and digits6:
		code_candidates.append(f"{digits6}_AL")
	if raw_code and raw_code not in code_candidates:
		code_candidates.append(raw_code)

	all_stk_tp = "1" if code_candidates else "0"
	# 주문 거래소(KRX/NXT/SOR)에 맞춰 조회해야 NXT 주문/체결이 화면에 표시됩니다.
	stex_tp = stex_tp_norm
	qry_tp = "0"
	# ka10076에서 sell_tp(매도/전체 등 조회구분)이 필수로 요구되는 케이스가 있어 기본값 포함
	# - 값 규칙이 문서/환경별로 다를 수 있어 우선 "0"(전체)로 둠
	sell_tp = "0"
	base = {
		"all_stk_tp": all_stk_tp,
		"stk_tp": all_stk_tp,  # 호환용
		"stex_tp": stex_tp,
		"dmst_stex_tp": stex_tp,
		"qry_tp": qry_tp,
		"qryTp": qry_tp,
		"sell_tp": sell_tp,
		"sellTp": sell_tp,
		"trde_tp": "0",
		"stk_cd": "",
		"ccld_tp": "2",  # 2:체결
	}
	bodies = []
	for c in (code_candidates or [""]):
		base2 = {**base, "stk_cd": c}
		for acct_key in ("acctNo", "acnt_no", "acct_no", "acntNo"):
			bodies.append({acct_key: acnt_no, **base2})
		bodies.append({"acctNo": acnt_no, "allStkTp": all_stk_tp, **base2})
		bodies.append({"acctNo": acnt_no, "acnt_no": acnt_no, "acct_no": acnt_no, "acntNo": acnt_no, "allStkTp": all_stk_tp, **base2})
	last = None
	for body in bodies:
		try:
			r = call_tr(token, api_id="ka10076", body=body, endpoint="/api/dostk/acnt", timeout=20)
			j = r.json()
			rows = _pick_list_rows(j)
			return rows if isinstance(rows, list) else []
		except Exception as e:
			last = e
	if last:
		raise last
	return []


def call_tr(token: str, api_id: str, body: dict, endpoint: str, cont_yn="N", next_key="", timeout=20):
	url = HOST + endpoint
	headers = {
		"Content-Type": "application/json;charset=UTF-8",
		"authorization": f"Bearer {token}",
		"api-id": api_id,
	}
	if cont_yn:
		headers["cont-yn"] = cont_yn
	if next_key:
		headers["next-key"] = next_key
	r = _http_post(url, json=body, headers=headers, timeout=timeout)
	r.raise_for_status()
	res_json = r.json()
	if isinstance(res_json, dict) and "return_code" in res_json:
		try:
			rc = int(res_json.get("return_code"))
		except Exception:
			rc = None
		if rc not in (None, 0):
			raise RuntimeError(f"{api_id} error: {res_json.get('return_msg')} (return_code={res_json.get('return_code')})")
	return r


def call_tr_all_pages(token: str, api_id: str, body: dict, endpoint: str, max_pages=30):
	all_rows = []
	cont_yn = "N"
	next_key = ""
	pages = 0
	while pages < max_pages:
		pages += 1
		time.sleep(0.25)
		r = call_tr(token, api_id, body, endpoint, cont_yn=cont_yn, next_key=next_key)
		res_json = r.json()
		rows = _pick_list_rows(res_json)
		if rows:
			all_rows.extend(rows)
		resp_cont = r.headers.get("cont-yn", "") or r.headers.get("Cont-Yn", "")
		resp_next = r.headers.get("next-key", "") or r.headers.get("Next-Key", "")
		if str(resp_cont).upper() == "Y" and resp_next:
			cont_yn = "Y"
			next_key = resp_next
		else:
			break
	return {"api_id": api_id, "endpoint": endpoint, "pages": pages, "raw_rows": len(all_rows), "rows": all_rows}


# ============================================================
# Chart (OHLCV 90일) API
# ============================================================
def fetch_ohlcv_90d(token: str, stk_cd: str, days=90):
	"""
	가능하면 키움 일봉/일별주가 TR(ka10086/ka10081)을 시도하고,
	응답 키명이 다를 수 있어 유연하게 파싱합니다.
	"""
	end_dt = datetime.now(TZ).strftime("%Y%m%d")
	endpoints = ["/api/dostk/chart"]
	candidates = ["ka10086", "ka10081"]  # 문서 페이지가 불안정해 후보로 시도

	# 수정주가구분(기본 1). HTS 설정과 동일하게 맞추고 싶으면 .env로 조정하세요.
	# - 예: KIWOOM_OHLCV_UPD_STKPC_TP=0
	upd_stkpc_tp = (os.getenv("KIWOOM_OHLCV_UPD_STKPC_TP") or "1").strip()
	stex_tp = (DEFAULT_EXCHANGE or "KRX").strip().upper()

	last_err = None
	rows = []
	used = None
	for api_id in candidates:
		for ep in endpoints:
			# TR별 입력 파라미터명이 다를 수 있어 여러 body를 순차 시도합니다.
			common = {"stk_cd": stk_cd, "stex_tp": stex_tp, "dmst_stex_tp": stex_tp}
			bodies = []
			# ka10081에서 base_dt 필수로 요구되는 케이스를 확인(1511: base_dt)
			bodies.append({**common, "base_dt": end_dt, "upd_stkpc_tp": upd_stkpc_tp})
			bodies.append({**common, "base_dt": end_dt, "upd_stkpc_tp": "0" if upd_stkpc_tp != "0" else "1"})
			# 일부 TR은 dt/inq_dt 키를 사용할 수 있어 보강
			bodies.append({**common, "dt": end_dt, "upd_stkpc_tp": upd_stkpc_tp})
			bodies.append({**common, "inq_dt": end_dt, "upd_stkpc_tp": upd_stkpc_tp})
			bodies.append({**common, "dt": end_dt})

			for body in bodies:
				try:
					res = call_tr_all_pages(
						token=token,
						api_id=api_id,
						body=body,
						endpoint=ep,
						max_pages=10,
					)
					rows = res["rows"]
					used = (api_id, ep, body, res)
					if rows:
						break
				except Exception as e:
					last_err = e
					continue
			if rows:
				break
		if rows:
			break

	if not rows:
		raise RuntimeError(f"OHLCV fetch failed: {last_err}")

	out = []
	for r in rows:
		dt = _parse_dt_any(r.get("dt") or r.get("date") or r.get("bas_dt") or r.get("base_dt") or r.get("trde_dt"))
		if not dt:
			continue

		open_p = _to_int(r.get("open_pric") or r.get("open") or r.get("stck_oprc") or r.get("opn_prc"), 0)
		high_p = _to_int(r.get("high_pric") or r.get("high") or r.get("stck_hgpr") or r.get("hgh_prc"), 0)
		low_p = _to_int(r.get("low_pric") or r.get("low") or r.get("stck_lwpr") or r.get("low_prc"), 0)
		close_p = _to_int(r.get("close_pric") or r.get("close") or r.get("stck_clpr") or r.get("cur_prc") or r.get("cur_pric"), 0)
		vol = _to_int(r.get("trde_qty") or r.get("volume") or r.get("acml_vol") or r.get("acc_trde_qty"), 0)

		# 데이터가 전혀 없으면 스킵
		if max(open_p, high_p, low_p, close_p, vol) == 0:
			continue

		out.append({"dt": dt, "open": open_p, "high": high_p, "low": low_p, "close": close_p, "volume": vol})

	out.sort(key=lambda x: x["dt"], reverse=True)
	seen = set()
	uniq = []
	for x in out:
		if x["dt"] in seen:
			continue
		seen.add(x["dt"])
		uniq.append(x)
	uniq = uniq[:days]

	from_dt = uniq[-1]["dt"] if uniq else ""
	to_dt = uniq[0]["dt"] if uniq else ""

	meta = {
		"used_api": used[0] if used else "",
		"used_endpoint": used[1] if used else "",
		"used_body_keys": sorted(list((used[2] or {}).keys())) if used else [],
		"raw_rows": used[3]["raw_rows"] if used else 0,
		"pages": used[3]["pages"] if used else 0,
	}
	return {"ticker": stk_cd, "from": from_dt, "to": to_dt, "days": len(uniq), "ohlcv": uniq, "meta": meta}


@app.get("/api/ohlcv")
def api_ohlcv():
	q = (request.args.get("ticker") or "").strip()
	try:
		ticker, name, err = resolve_ticker(q)
		if err or not ticker:
			return jsonify({"error": err or "종목명을 다시확인해 주세요"}), 400
		provider = (MARKETDATA_PROVIDER or "").upper()

		# 구버전 호환: KIWOOM_USE_KIWOOM_MARKETDATA가 명시되면 provider에 반영
		if os.getenv("KIWOOM_MARKETDATA_PROVIDER") is None and os.getenv("KIWOOM_USE_KIWOOM_MARKETDATA") is not None:
			provider = "KIWOOM" if USE_KIWOOM_MARKETDATA else "PUBLIC"

		if provider == "MOCK":
			data = mock_ohlcv_90d(ticker, days=90)
			data["name"] = name or ""
			data["meta"] = {"used_api": "MOCK", "used_endpoint": "", "raw_rows": 0, "pages": 0}
			return jsonify(data)

		if provider == "PUBLIC":
			try:
				data = fetch_ohlcv_90d_public(ticker, days=90)
				data["name"] = name or ""
				return jsonify(data)
			except Exception as e:
				if ALLOW_MOCK_FALLBACK:
					data = mock_ohlcv_90d(ticker, days=90)
					data["name"] = name or ""
					data["meta"] = {"used_api": "MOCK", "used_endpoint": "", "raw_rows": 0, "pages": 0, "note": "PUBLIC fetch failed → mock fallback", "error": str(e)}
					return jsonify(data)
				raise

		# provider == KIWOOM (default for LIVE)
		if not APP_KEY or not APP_SECRET:
			return jsonify({"error": "ServerMisconfigured", "detail": "Set APP_KEY/APP_SECRET in .env"}), 500
		try:
			token = get_token(APP_KEY, APP_SECRET)
			data = fetch_ohlcv_90d(token, ticker, days=90)
			data["name"] = name or ""
			return jsonify(data)
		except Exception as e:
			# 키움 실패 시: PUBLIC → MOCK 순으로 폴백
			if ALLOW_MOCK_FALLBACK:
				try:
					data = fetch_ohlcv_90d_public(ticker, days=90)
					data["name"] = name or ""
					data.setdefault("meta", {})
					data["meta"]["note"] = f"KIWOOM failed → PUBLIC fallback"
					data["meta"]["kiwoom_error"] = str(e)
					return jsonify(data)
				except Exception as e2:
					data = mock_ohlcv_90d(ticker, days=90)
					data["name"] = name or ""
					data["meta"] = {"used_api": "MOCK", "used_endpoint": "", "raw_rows": 0, "pages": 0, "note": "KIWOOM+PUBLIC failed → mock fallback", "kiwoom_error": str(e), "public_error": str(e2)}
					return jsonify(data)
			raise
	except Exception as e:
		return jsonify({"error": "ServerError", "detail": str(e)}), 500


# ============================================================
# Trading bot (기본: DRY_RUN)
# ============================================================
@dataclass
class TradePlan:
	ticker: str
	name: str = ""
	exchange: str = "AUTO"  # AUTO/KRX/NXT/SOR
	qty: int = 1
	buy_price: int = 0
	stop_loss: int = 0
	take_profit: int = 0


@dataclass
class BotState:
	running: bool = False
	mode: str = "DRY_RUN"  # DRY_RUN / LIVE
	message: str = ""
	last_price: int = 0
	last_bid: int = 0
	last_ask: int = 0
	position_qty: int = 0
	buy_submitted: bool = False
	sell_submitted: bool = False
	last_action_at: str = ""
	plan: TradePlan | None = None
	last_order_no: str = ""
	fills: list[dict] | None = None
	unfilled: list[dict] | None = None


_bot_lock = threading.Lock()
_bot_state = BotState()
_stop_event = threading.Event()
_thread: threading.Thread | None = None


def _plan_to_dict(plan: TradePlan) -> dict:
	return asdict(plan)


def _load_persisted_plan() -> dict | None:
	try:
		return bot_store.load_plan()
	except Exception:
		return None


def _persist_plan(plan: TradePlan, enabled: bool | None = None) -> dict:
	"""
	UI 서버가 꺼져도 봇이 이어서 실행할 수 있도록 플랜을 파일로 저장합니다.
	"""
	bot_store.ensure_data_dir()
	now = datetime.now(TZ).isoformat(timespec="seconds")
	existing = bot_store.load_plan() or {}
	plan_id = str(existing.get("plan_id") or "") or f"plan-{int(time.time())}"
	payload = {
		"plan_id": plan_id,
		"updated_at": now,
		"enabled": bool(existing.get("enabled")) if enabled is None else bool(enabled),
		"plan": _plan_to_dict(plan),
	}
	bot_store.save_plan(payload)
	return payload


def _make_entry_from_plan(plan: TradePlan) -> dict:
	"""
	분할매수/엔트리 기반 운용을 위해, 전략 저장 시 엔트리 1건을 생성합니다.
	엔트리는 "각 매수건"의 TP/SL을 고정값으로 보관합니다.
	"""
	now = datetime.now(TZ).isoformat(timespec="seconds")
	eid = f"e-{int(time.time() * 1000)}"
	return {
		"id": eid,
		"created_at": now,
		"ticker": plan.ticker,
		"name": plan.name or "",
		"exchange": (plan.exchange or "AUTO").strip().upper(),
		"qty": int(plan.qty or 1),
		"buy_price": int(plan.buy_price or 0),
		"take_profit": int(plan.take_profit or 0),
		"stop_loss": int(plan.stop_loss or 0),
		"status": "PENDING",  # PENDING/BUY_SUBMITTED/FILLED/SELL_SUBMITTED/CLOSED/CANCELLED
		"buy_ord_no": "",
		"buy_ord_at": "",
		"buy_fill_price": 0,
		"buy_fill_time": "",
		# 보호주문(자동감시 유사): 체결 직후 TP/SL을 "주문으로" 거래소에 등록
		"tp_ord_no": "",
		"sl_ord_no": "",
		"protect_exchange": "",
		"protect_set_at": "",
		"close_reason": "",
		# 호환/기존 표시용(단일 매도주문 번호)
		"sell_ord_no": "",
		"closed_at": "",
	}


def _load_runner_state() -> dict:
	"""
	bot_runner.py가 저장하는 상태 파일을 읽어 UI에 전달합니다.
	"""
	st = bot_store.load_state() or {}
	plan_wrap = bot_store.load_plan() or {}
	plan = plan_wrap.get("plan")
	if plan:
		# ✅ enabled/plan의 '진실 소스'는 plan 파일입니다.
		# runner 상태가 늦게 갱신되더라도 UI에서 ARM 표시가 즉시 맞게 보이도록 override 합니다.
		st["plan"] = plan
		st["enabled"] = bool(plan_wrap.get("enabled"))
	# heartbeat 기반으로 runner 실행 여부 힌트 제공
	try:
		hb = float(st.get("heartbeat_ts") or st.get("runner", {}).get("heartbeat_ts") or 0.0)
	except Exception:
		hb = 0.0
	age = time.time() - hb if hb else 1e9
	st["runner_alive"] = bool(hb and age < 10.0)
	st["runner_heartbeat_age_sec"] = round(age, 1) if hb else None
	if not st["runner_alive"]:
		st.setdefault("message", "bot_runner가 실행 중이 아닙니다. bot_runner.py를 실행해야 자동매매가 동작합니다.")
	return st


def _now_kst():
	return datetime.now(TZ)


def _parse_hhmm(hhmm: str):
	m = re.fullmatch(r"(\d{1,2}):(\d{2})", (hhmm or "").strip())
	if not m:
		return (8, 0)
	h = int(m.group(1))
	mi = int(m.group(2))
	return (max(0, min(23, h)), max(0, min(59, mi)))


def _hhmm_today(now: datetime, hhmm: str) -> datetime:
	h, m = _parse_hhmm(hhmm)
	return now.replace(hour=h, minute=m, second=0, microsecond=0)


def _next_trading_window_start(exchange: str, now: datetime) -> tuple[datetime, str]:
	"""
	거래소별 "주문 제출" 가능 시작 시각(오늘 기준)을 계산합니다.
	- NXT: 08:00 시작(기본)
	- 기타: LIVE_ORDER_START_HHMM(기본 08:00) 사용
	"""
	ex = (exchange or DEFAULT_EXCHANGE or "KRX").strip().upper()
	if ex == "NXT":
		return (_hhmm_today(now, NXT_MARKET_START_HHMM), f"NXT 시작 {NXT_MARKET_START_HHMM}")
	return (_hhmm_today(now, LIVE_ORDER_START_HHMM), f"주문 시작 {LIVE_ORDER_START_HHMM}")


def _nxt_new_order_allowed(now: datetime) -> tuple[bool, datetime | None, str]:
	"""
	NXT에서 신규주문 제출 가능 여부를 보조적으로 판단.
	실제 허용 여부는 키움/증권사/시장 세션에 따라 달라질 수 있으며,
	여기서는 '08:50~09:00 신규주문 불가' 같은 구간을 피하기 위한 UX 개선 목적입니다.
	"""
	start = _hhmm_today(now, NXT_MARKET_START_HHMM)
	pause_s = _hhmm_today(now, NXT_NEW_ORDER_PAUSE_START_HHMM)
	pause_e = _hhmm_today(now, NXT_NEW_ORDER_PAUSE_END_HHMM)
	pause2_s = _hhmm_today(now, NXT_NEW_ORDER_PAUSE2_START_HHMM)
	pause2_e = _hhmm_today(now, NXT_NEW_ORDER_PAUSE2_END_HHMM)
	end = _hhmm_today(now, NXT_MARKET_END_HHMM)

	if now < start:
		return (False, start, f"미개장({NXT_MARKET_START_HHMM} 이전)")
	if pause_s <= now < pause_e:
		return (False, pause_e, f"시가결정 구간({NXT_NEW_ORDER_PAUSE_START_HHMM}~{NXT_NEW_ORDER_PAUSE_END_HHMM})")
	if pause2_s <= now < pause2_e:
		return (False, pause2_e, f"전환/종가결정 구간({NXT_NEW_ORDER_PAUSE2_START_HHMM}~{NXT_NEW_ORDER_PAUSE2_END_HHMM})")
	if now >= end:
		# 다음 영업일 계산은 휴일/주말 고려가 필요하지만, 여기서는 다음날로 안내
		return (False, start + timedelta(days=1), f"장종료({NXT_MARKET_END_HHMM} 이후)")
	return (True, None, "ok")


def _validate_account_no() -> str:
	"""
	키움 REST의 계좌번호는 보통 10자리(예: 5947559410)로 내려옵니다(ka00001의 acctNo).
	사용자가 '5947-5594'처럼 8자리만 넣는 경우가 있어, LIVE에서 명확히 안내합니다.
	"""
	ac = re.sub(r"\D", "", ACCOUNT_NO_RAW or "")
	if not ac:
		raise RuntimeError("LIVE mode requires KIWOOM_ACCOUNT_NO in .env (예: 5947-5594-10 또는 5947559410)")
	if len(ac) < 10:
		raise RuntimeError(
			f"KIWOOM_ACCOUNT_NO looks incomplete: '{ACCOUNT_NO_RAW}' → '{ac}'. "
			"은행 입금용처럼 뒤의 상품코드(예: -10)를 포함한 10자리 계좌를 넣어주세요. "
			"(키움 REST ka00001의 acctNo 값과 동일하게 설정)"
		)
	return ac


def _pick_live_trde_tp(exchange: str, now: datetime) -> str:
	"""
	주문구분(trde_tp) 자동 선택.
	- KRX: 08:00~09:00(기본) => 61(장시작전시간외), 09:00 이후 => 00(보통)
	- NXT/SOR 등: 기본 00(보통)
	주의: 실제 허용 여부는 시장/세션에 따라 다를 수 있음.
	"""
	ex = (exchange or DEFAULT_EXCHANGE or "KRX").strip().upper()
	if ex != "KRX":
		return "00"
	ps_h, ps_m = _parse_hhmm(KRX_PREMARKET_START_HHMM)
	pe_h, pe_m = _parse_hhmm(KRX_PREMARKET_END_HHMM)
	pre_start = now.replace(hour=ps_h, minute=ps_m, second=0, microsecond=0)
	pre_end = now.replace(hour=pe_h, minute=pe_m, second=0, microsecond=0)
	if pre_start <= now < pre_end:
		return "61"  # 장시작전시간외
	return "00"      # 보통


def _get_current_price(token: str, stk_cd: str) -> int | None:
	# best bid/ask 기반으로 "현재가 대용" 반환 (ask 우선, 없으면 bid)
	q = fetch_best_bid_ask(token, stk_cd)
	ask = int(q.get("best_ask") or 0)
	bid = int(q.get("best_bid") or 0)
	return ask if ask > 0 else (bid if bid > 0 else None)


def _place_buy_limit(token: str, stk_cd: str, exchange: str, qty: int, price: int, trde_tp: str = "00", cond_uv: int | None = None) -> dict:
	"""
	실거래 주문: kt10000 (주식매수주문)
	주의: 계좌/권한/주문 파라미터가 맞지 않으면 에러가 납니다.
	"""
	_validate_account_no()
	if not ENABLE_LIVE:
		raise RuntimeError("LIVE trading disabled. Set KIWOOM_ENABLE_LIVE_TRADING=YES and KIWOOM_DRY_RUN=0")

	body = {
		"dmst_stex_tp": (exchange or DEFAULT_EXCHANGE),
		"stk_cd": stk_cd,
		"ord_qty": str(int(qty)),
		"ord_uv": str(int(price)),
		"trde_tp": str(trde_tp or "00").zfill(2),
	}
	if cond_uv is not None:
		body["cond_uv"] = str(int(cond_uv))
	r = call_tr(token, api_id="kt10000", body=body, endpoint="/api/dostk/ordr", timeout=20)
	return r.json()


def _place_sell_limit(token: str, stk_cd: str, exchange: str, qty: int, price: int, trde_tp: str = "00", cond_uv: int | None = None) -> dict:
	_validate_account_no()
	if not ENABLE_LIVE:
		raise RuntimeError("LIVE trading disabled. Set KIWOOM_ENABLE_LIVE_TRADING=YES and KIWOOM_DRY_RUN=0")
	body = {
		"dmst_stex_tp": (exchange or DEFAULT_EXCHANGE),
		"stk_cd": stk_cd,
		"ord_qty": str(int(qty)),
		"ord_uv": str(int(price)),
		"trde_tp": str(trde_tp or "00").zfill(2),
	}
	if cond_uv is not None:
		body["cond_uv"] = str(int(cond_uv))
	r = call_tr(token, api_id="kt10001", body=body, endpoint="/api/dostk/ordr", timeout=20)
	return r.json()


def _place_sell_stop_limit(token: str, stk_cd: str, exchange: str, qty: int, stop_price: int) -> dict:
	"""
	손절(스톱지정가) 주문을 "거래소에 미리" 등록합니다.
	- trde_tp=28: 스톱지정가
	- cond_uv: 조건단가(트리거)
	- ord_uv: 지정가(일반적으로 stop_price로 동일하게 설정)
	주의: 실제 체결 동작은 시장/호가에 따라 달라질 수 있습니다.
	"""
	return _place_sell_limit(
		token,
		stk_cd=stk_cd,
		exchange=exchange,
		qty=qty,
		price=int(stop_price),
		trde_tp="28",
		cond_uv=int(stop_price),
	)


def _normalize_exchange(ex: str | None) -> str:
	"""
	거래소 입력값을 KRX/NXT/SOR로 정규화합니다.
	- ka10075 응답에는 stex_tp가 "1/2/3"으로 오는 케이스가 있어 보정합니다.
	"""
	s = str(ex or "").strip().upper()
	if s in ("1", "KRX"):
		return "KRX"
	if s in ("2", "NXT"):
		return "NXT"
	if s in ("3", "SOR"):
		return "SOR"
	return s or "KRX"


def _cancel_order(token: str, ord_no: str, stk_cd: str, exchange: str, cncl_qty: int | str | None = None) -> dict:
	"""
	실거래 주문취소: kt10003 (주식취소주문)
	- 문서상 주문 TR과 동일 endpoint(/api/dostk/ordr)
	- 공식 스펙(키움 가이드):
	  dmst_stex_tp, orig_ord_no, stk_cd, cncl_qty(0이면 잔량 전부 취소)
	"""
	_validate_account_no()
	if not ENABLE_LIVE:
		raise RuntimeError("LIVE trading disabled. Set KIWOOM_ENABLE_LIVE_TRADING=YES and KIWOOM_DRY_RUN=0")

	ord_no = str(ord_no or "").strip()
	if not ord_no:
		raise RuntimeError("ord_no is required")

	ex = _normalize_exchange(exchange or DEFAULT_EXCHANGE or "KRX")
	code6 = _format_stk_cd(stk_cd, ex)
	q = "0"
	if cncl_qty is not None:
		try:
			qn = int(cncl_qty)
			# 0이면 잔량 전부 취소(권장 기본)
			q = str(max(0, qn))
		except Exception:
			q = "0"

	body = {
		"dmst_stex_tp": ex,
		"orig_ord_no": ord_no,
		"stk_cd": code6,
		"cncl_qty": q,
	}
	r = call_tr(token, api_id="kt10003", body=body, endpoint="/api/dostk/ordr", timeout=20)
	return r.json()


def _bot_loop():
	global _bot_state
	with _bot_lock:
		live = (_bot_state.mode == "LIVE")

	token: str | None = None
	need_token = live or USE_KIWOOM_MARKETDATA
	if need_token and APP_KEY and APP_SECRET:
		try:
			token = get_token(APP_KEY, APP_SECRET)
		except Exception as e:
			# LIVE는 토큰이 반드시 필요하지만, DRY_RUN은 mock으로 계속 진행
			if live:
				with _bot_lock:
					_bot_state.message = f"토큰 발급 실패: {e}"
					_bot_state.running = False
				return
			with _bot_lock:
				_bot_state.message = f"[DRY_RUN] 키움 토큰 실패 → mock 시세로 진행: {e}"
				_bot_state.last_action_at = _now_kst().isoformat(timespec="seconds")

	open_h, open_m = _parse_hhmm(MARKET_OPEN_HHMM)
	live_ord_h, live_ord_m = _parse_hhmm(LIVE_ORDER_START_HHMM)

	while not _stop_event.is_set():
		with _bot_lock:
			st = _bot_state
			plan = st.plan
			live = (st.mode == "LIVE")
		if not plan:
			time.sleep(0.5)
			continue

		now = _now_kst()
		# "시세 모니터링 시작" (기본 08:00). NXT는 08:00 시작을 기본으로 둡니다.
		ex = (plan.exchange or DEFAULT_EXCHANGE or "KRX").strip().upper()
		if ex == "NXT":
			open_time = _hhmm_today(now, NXT_MARKET_START_HHMM)
			open_label = f"NXT 시작 {NXT_MARKET_START_HHMM}"
		else:
			open_time = now.replace(hour=open_h, minute=open_m, second=0, microsecond=0)
			open_label = f"장 시작 {MARKET_OPEN_HHMM}"
		if now < open_time:
			with _bot_lock:
				_bot_state.message = f"대기 중… ({open_label})"
				_bot_state.last_action_at = now.isoformat(timespec="seconds")
			time.sleep(1.0)
			continue

		# LIVE에서는 "주문 제출"만 별도 시작 시각을 둠(장전 모니터링은 가능)
		live_order_time, _ = _next_trading_window_start(plan.exchange, now)
		live_can_order = (not live) or (now >= live_order_time)

		# 현재가 확인
		stk_cd = _format_stk_cd(plan.ticker, plan.exchange)
		q = None
		cur = None
		if token:
			q = fetch_best_bid_ask(token, stk_cd, stex_tp=plan.exchange)
			cur = q["best_ask"] or q["best_bid"] or None
		else:
			with _bot_lock:
				prev_p = int(_bot_state.last_price or 0)
			anchor = int(plan.buy_price or _mock_base_price(plan.ticker))
			q = mock_quote_step(plan.ticker, prev_p, anchor_price=anchor)
			cur = q["best_ask"] or q["best_bid"] or None
		with _bot_lock:
			_bot_state.last_price = int(cur or 0)
			_bot_state.last_bid = int(q.get("best_bid") or 0)
			_bot_state.last_ask = int(q.get("best_ask") or 0)
			_bot_state.last_action_at = now.isoformat(timespec="seconds")

		# 매수 전: 장 시작 이후 1회 주문 제출(실거래) 또는 가격 도달 시 체결(모의)
		with _bot_lock:
			buy_submitted = _bot_state.buy_submitted
			pos_qty = _bot_state.position_qty
			sell_submitted = _bot_state.sell_submitted

		if pos_qty == 0:
			if live:
				if not buy_submitted:
					if not live_can_order:
						with _bot_lock:
							_bot_state.message = f"[LIVE] 대기 중… (주문 시작 {live_order_time.strftime('%H:%M')})"
						time.sleep(1.0)
						continue
					# NXT: 특정 구간 신규주문 보류(예: 08:50~09:00)
					if ex == "NXT":
						allow, next_at, reason = _nxt_new_order_allowed(now)
						if not allow and next_at is not None:
							with _bot_lock:
								_bot_state.message = (
									f"[LIVE] NXT 신규주문 불가({reason}) → {next_at.strftime('%H:%M')}에 재시도… "
									f"(exchange=NXT, stk_cd={stk_cd})"
								)
							time.sleep(2.0)
							continue
					try:
						trde_tp = _pick_live_trde_tp(plan.exchange, now)
						resp = _place_buy_limit(token, stk_cd, plan.exchange, plan.qty, plan.buy_price, trde_tp=trde_tp)
						ord_no = str(_first_non_empty(resp, ["ord_no", "ordNo", "order_no", "ordNo"]) or "")
						with _bot_lock:
							_bot_state.buy_submitted = True
							_bot_state.last_order_no = ord_no
							_bot_state.message = (
								f"[LIVE] 매수 주문 제출: {plan.buy_price} x {plan.qty} "
								f"(exchange={ex}, stk_cd={stk_cd}, trde_tp={trde_tp}, ord_no={ord_no})"
							)
					except Exception as e:
						# 장 종료(return_code=20) 등 "시간 문제"는 에러로 굳히지 말고 대기/재시도
						msg = str(e)
						if "return_code=20" in msg or "장종료" in msg or "장 종료" in msg:
							# 거래소별 다음 재시도 시각 힌트
							next_hint = live_order_time.strftime("%H:%M")
							if ex == "NXT":
								allow2, next_at2, _ = _nxt_new_order_allowed(now)
								if next_at2 is not None:
									next_hint = next_at2.strftime("%H:%M")
							with _bot_lock:
								_bot_state.message = (
									f"[LIVE] 주문 불가 시간(장종료/미개장) → 대기 중… ({next_hint}부터 재시도) "
									f"(exchange={ex}, stk_cd={stk_cd})"
								)
							time.sleep(2.0)
							continue
						with _bot_lock:
							_bot_state.message = f"[LIVE] 매수 주문 실패: {e} (exchange={ex}, stk_cd={stk_cd})"
				# 체결 확인: 미체결/체결 조회
				try:
					unfilled = fetch_unfilled_orders(token, ACCOUNT_NO, stk_cd=plan.ticker, stex_tp=plan.exchange)
					fills = fetch_fills(token, ACCOUNT_NO, stk_cd=plan.ticker, stex_tp=plan.exchange)
					with _bot_lock:
						_bot_state.fills = fills[-50:] if isinstance(fills, list) else []
						_bot_state.unfilled = unfilled[-50:] if isinstance(unfilled, list) else []
				except Exception as e:
					with _bot_lock:
						_bot_state.message = f"[LIVE] 체결조회 실패: {e}"
				# 체결 판단(단순): 미체결 목록에 내 주문번호가 없고, 체결 목록에 나타나면 체결로 간주
				with _bot_lock:
					ord_no = _bot_state.last_order_no
				if ord_no:
					u_has = any(str(_first_non_empty(x, ['ord_no','ordNo','order_no']) or '') == ord_no for x in (unfilled or []))
					f_has = any(str(_first_non_empty(x, ['ord_no','ordNo','order_no']) or '') == ord_no for x in (fills or []))
					if (not u_has) and f_has:
						with _bot_lock:
							_bot_state.position_qty = plan.qty
							_bot_state.message = f"[LIVE] 매수 체결 확인(ord_no={ord_no})"
			else:
				if cur is not None and cur <= plan.buy_price:
					with _bot_lock:
						_bot_state.position_qty = plan.qty
						_bot_state.message = f"[DRY_RUN] 매수 체결(현재가 {cur} <= 매수가 {plan.buy_price})"

		# 매수 후: 익절/손절 조건 충족 시 매도
		with _bot_lock:
			pos_qty = _bot_state.position_qty

		if pos_qty > 0 and cur is not None:
			# LIVE: 매도 체결 확인(미체결/체결)
			if live:
				with _bot_lock:
					sell_submitted_now = _bot_state.sell_submitted
					ord_no_now = _bot_state.last_order_no
				if sell_submitted_now and ord_no_now:
					try:
						unfilled2 = fetch_unfilled_orders(token, ACCOUNT_NO, stk_cd=plan.ticker, stex_tp=plan.exchange)
						fills2 = fetch_fills(token, ACCOUNT_NO, stk_cd=plan.ticker, stex_tp=plan.exchange)
						with _bot_lock:
							_bot_state.fills = fills2[-50:] if isinstance(fills2, list) else []
							_bot_state.unfilled = unfilled2[-50:] if isinstance(unfilled2, list) else []
						u_has2 = any(str(_first_non_empty(x, ["ord_no", "ordNo", "order_no"]) or "") == ord_no_now for x in (unfilled2 or []))
						f_has2 = any(str(_first_non_empty(x, ["ord_no", "ordNo", "order_no"]) or "") == ord_no_now for x in (fills2 or []))
						if (not u_has2) and f_has2:
							with _bot_lock:
								_bot_state.position_qty = 0
								_bot_state.message = f"[LIVE] 매도 체결 확인(ord_no={ord_no_now})"
							# 매도 체결되면 다음 루프에서 포지션 0으로 처리
							time.sleep(POLL_INTERVAL_SEC)
							continue
					except Exception as e:
						with _bot_lock:
							_bot_state.message = f"[LIVE] 매도 체결조회 실패: {e}"

			trigger_sell_price = None
			trigger_reason = None
			if plan.take_profit > 0 and cur >= plan.take_profit:
				trigger_sell_price = plan.take_profit
				trigger_reason = "익절"
			elif plan.stop_loss > 0 and cur <= plan.stop_loss:
				trigger_sell_price = plan.stop_loss
				trigger_reason = "손절"

			if trigger_sell_price is not None and not sell_submitted:
				if live:
					try:
						trde_tp = _pick_live_trde_tp(plan.exchange, now)
						resp = _place_sell_limit(token, stk_cd, plan.exchange, pos_qty, trigger_sell_price, trde_tp=trde_tp)
						ord_no = str(_first_non_empty(resp, ["ord_no", "ordNo", "order_no", "ordNo"]) or "")
						with _bot_lock:
							_bot_state.sell_submitted = True
							_bot_state.last_order_no = ord_no
							_bot_state.message = f"[LIVE] {trigger_reason} 매도 주문 제출: {trigger_sell_price} x {pos_qty} (trde_tp={trde_tp}, ord_no={ord_no})"
					except Exception as e:
						with _bot_lock:
							_bot_state.message = f"[LIVE] 매도 주문 실패: {e}"
				else:
					with _bot_lock:
						_bot_state.sell_submitted = True
						_bot_state.position_qty = 0
						_bot_state.message = f"[DRY_RUN] {trigger_reason} 매도 체결(현재가 {cur}, 매도가 {trigger_sell_price})"

		time.sleep(POLL_INTERVAL_SEC)

	with _bot_lock:
		_bot_state.running = False
		_bot_state.message = "중지됨"


@app.post("/api/bot/plan")
def api_bot_plan():
	try:
		payload = request.get_json(force=True) or {}
		q = str(payload.get("ticker") or "").strip()
		ticker, name, err = resolve_ticker(q)
		if err or not ticker:
			return jsonify({"ok": False, "error": err or "종목명을 다시확인해 주세요"}), 400

		exchange = str(payload.get("exchange") or "AUTO").strip().upper()
		if exchange not in ("AUTO", "KRX", "NXT", "SOR"):
			exchange = "AUTO"
		qty = int(payload.get("qty") or 1)
		buy_price = int(payload.get("buy_price") or 0)
		stop_loss = int(payload.get("stop_loss") or 0)
		take_profit = int(payload.get("take_profit") or 0)

		if qty <= 0:
			return jsonify({"ok": False, "error": "수량(qty)은 1 이상이어야 합니다."}), 400
		if buy_price <= 0:
			return jsonify({"ok": False, "error": "매수가(buy_price)는 1 이상이어야 합니다."}), 400
		if stop_loss and stop_loss >= buy_price:
			return jsonify({"ok": False, "error": "손절가(stop_loss)는 매수가보다 낮아야 합니다."}), 400
		if take_profit and take_profit <= buy_price:
			return jsonify({"ok": False, "error": "익절가(take_profit)는 매수가보다 높아야 합니다."}), 400

		plan = TradePlan(
			ticker=ticker,
			name=name or "",
			exchange=exchange,
			qty=qty,
			buy_price=buy_price,
			stop_loss=stop_loss,
			take_profit=take_profit,
		)

		with _bot_lock:
			_bot_state.plan = plan
			_bot_state.position_qty = 0
			_bot_state.buy_submitted = False
			_bot_state.sell_submitted = False
			_bot_state.message = "전략 저장됨"

		# ✅ persistence: (1) 최신 plan 저장(호환용), (2) 엔트리 1건 append 저장
		wrap = _persist_plan(plan, enabled=None)
		entry = _make_entry_from_plan(plan)
		try:
			entries = bot_store.load_entries()
		except Exception:
			entries = []
		entries.append(entry)
		# 너무 길어지지 않게 최근 N개만 유지(기본 200)
		try:
			keep_n = max(50, min(2000, int(os.getenv("KIWOOM_ENTRIES_KEEP") or "200")))
		except Exception:
			keep_n = 200
		if len(entries) > keep_n:
			entries = entries[-keep_n:]
		bot_store.save_entries(entries)

		return jsonify({
			"ok": True,
			"plan": asdict(plan),
			"entry": entry,
			"stored": {"plan_id": wrap.get("plan_id"), "enabled": wrap.get("enabled")},
		})
	except Exception as e:
		return jsonify({"ok": False, "error": "ServerError", "detail": str(e)}), 500


@app.get("/api/bot/entries")
def api_bot_entries():
	"""
	저장된 엔트리 목록 조회.
	- UI가 TP/SL을 "매수건별"로 표시하기 위한 데이터 소스
	"""
	try:
		entries = bot_store.load_entries()
		return jsonify({"ok": True, "entries": entries})
	except Exception as e:
		return jsonify({"ok": False, "error": "ServerError", "detail": str(e)}), 500


@app.post("/api/bot/start")
def api_bot_start():
	global _thread
	# NOTE:
	# UI 서버의 역할은 "ARM(활성화)"입니다. 토큰 발급/실거래 가능 여부는 bot_runner가 판단하고
	# token_error/message로 UI에 반영합니다.
	# 따라서 APP_KEY/APP_SECRET 미설정이어도 ARM 자체는 막지 않습니다(UX 개선).

	# ✅ 2번 구조: UI 서버는 "ARM(활성화)"만 수행하고, 실제 매매는 bot_runner.py가 수행합니다.
	with _bot_lock:
		if _bot_state.plan is None:
			# persisted plan이 있으면 메모리에도 로드(UX 개선)
			wrap = _load_persisted_plan() or {}
			pp = wrap.get("plan") if isinstance(wrap, dict) else None
			if isinstance(pp, dict) and pp.get("ticker"):
				_bot_state.plan = TradePlan(**pp)
		if _bot_state.plan is None:
			return jsonify({"ok": False, "error": "전략(plan)을 먼저 저장하세요."}), 400

	wrap = _persist_plan(_bot_state.plan, enabled=True)
	status = _load_runner_state()

	# ✅ 레이스 방지: start 응답에서는 방금 저장한 enabled/plan을 확정값으로 반영
	status["enabled"] = True
	if wrap.get("plan"):
		status["plan"] = wrap.get("plan")

	# 설정 누락은 경고로만 표시(ARM은 유지)
	if not APP_KEY or not APP_SECRET:
		status["message"] = "활성화됨(ARM). 단, APP_KEY/APP_SECRET 미설정으로 토큰 발급이 실패할 수 있습니다(.env 확인)."
	else:
		status.setdefault("message", "활성화됨(ARM). bot_runner가 실행 중이면 자동으로 매매를 시작합니다.")

	return jsonify({"ok": True, "status": status, "stored": {"plan_id": wrap.get("plan_id"), "enabled": True}})


@app.post("/api/bot/stop")
def api_bot_stop():
	# ✅ 2번 구조: DISARM(비활성화). runner는 대기 상태로 들어갑니다.
	wrap = _load_persisted_plan() or {}
	pp = wrap.get("plan") if isinstance(wrap, dict) else None
	if isinstance(pp, dict) and pp.get("ticker"):
		try:
			_persist_plan(TradePlan(**pp), enabled=False)
		except Exception:
			pass
	with _bot_lock:
		_bot_state.running = False
		_bot_state.message = "비활성화됨(DISARM)"
	return jsonify({"ok": True, "status": _load_runner_state()})


@app.get("/api/bot/status")
def api_bot_status():
	return jsonify({"ok": True, "status": _load_runner_state()})


@app.post("/api/bot/cancel")
def api_bot_cancel():
	"""
	미체결 주문 취소(kt10003).
	UI의 미체결 테이블에서 주문번호를 전달받아 취소합니다.
	"""
	try:
		if not APP_KEY or not APP_SECRET:
			return jsonify({"ok": False, "error": "ServerMisconfigured", "detail": "Set APP_KEY/APP_SECRET in .env"}), 500
		payload = request.get_json(force=True) or {}
		ord_no = str(payload.get("ord_no") or "").strip()
		stk_cd = str(payload.get("stk_cd") or "").strip()
		ex = _normalize_exchange(payload.get("exchange"))
		cncl_qty = payload.get("qty")

		if not ord_no:
			return jsonify({"ok": False, "error": "ord_no is required"}), 400
		if not stk_cd:
			# 미체결 row에 stk_cd가 없을 수 있어 plan에서 보정
			st = _load_runner_state() or {}
			plan = st.get("plan") if isinstance(st, dict) else None
			if isinstance(plan, dict):
				stk_cd = str(plan.get("ticker") or "").strip()
		if not stk_cd:
			return jsonify({"ok": False, "error": "stk_cd is required"}), 400

		token = get_token(APP_KEY, APP_SECRET)
		resp = _cancel_order(token, ord_no=ord_no, stk_cd=stk_cd, exchange=ex, cncl_qty=cncl_qty)
		# 엔트리도 함께 정리(UX): 해당 주문번호를 가진 BUY_SUBMITTED 엔트리는 CANCELLED 처리
		try:
			entries = bot_store.load_entries()
		except Exception:
			entries = []
		changed = False
		now_iso = datetime.now(TZ).isoformat(timespec="seconds")
		for e in entries:
			if not isinstance(e, dict):
				continue
			st = str(e.get("status") or "").upper()
			if st in ("CLOSED", "CANCELLED"):
				continue
			# 매수 미체결 취소: buy_ord_no가 일치하면 해당 엔트리를 취소 처리
			if str(e.get("buy_ord_no") or "").strip() == ord_no and st in ("BUY_SUBMITTED", "PENDING"):
				e["status"] = "CANCELLED"
				e["closed_at"] = now_iso
				e["close_reason"] = "주문취소"
				changed = True
		if changed:
			try:
				bot_store.save_entries(entries)
			except Exception:
				pass
		return jsonify({"ok": True, "resp": resp, "entries_updated": changed})
	except Exception as e:
		return jsonify({"ok": False, "error": "CancelFailed", "detail": str(e)}), 500


@app.get("/api/debug/paths")
def api_debug_paths():
	"""
	실행 중인 buysell 서버가 실제로 사용하는 파일 경로/빌드 정보를 반환합니다.
	(Windows에서 '다른 폴더에서 실행 중'인 경우를 빠르게 진단하기 위한 용도)
	"""
	try:
		return jsonify({
			"ok": True,
			"cwd": os.getcwd(),
			"buysell_file": __file__,
			"bot_store_file": getattr(bot_store, "__file__", ""),
			"data_dir": bot_store.DATA_DIR,
			"plan_path": bot_store.PLAN_PATH,
			"runtime_path": bot_store.RUNTIME_PATH,
			"state_path": bot_store.STATE_PATH,
			"now_ts": time.time(),
		})
	except Exception as e:
		return jsonify({"ok": False, "error": "ServerError", "detail": str(e)}), 500


@app.get("/api/bot/log")
def api_bot_log():
	"""
	bot_runner 로그(data/bot_log.jsonl) tail 조회.
	- UI에서 최근 이벤트를 빠르게 확인하기 위한 용도
	"""
	try:
		tail = request.args.get("tail") or "200"
		try:
			tail_n = max(1, min(1000, int(str(tail).strip())))
		except Exception:
			tail_n = 200
		pretty = (request.args.get("pretty") or "").strip() in ("1", "true", "True", "yes", "YES")

		path = bot_store.LOG_PATH
		if not os.path.exists(path):
			return jsonify({"ok": True, "lines": [], "events": [], "note": "log file not found yet"})

		# 간단 구현: 파일 전체를 읽고 tail (일반적으로 크지 않음). 필요시 추후 seek 기반으로 최적화 가능.
		with open(path, "r", encoding="utf-8") as f:
			lines = [ln.rstrip("\n") for ln in f.readlines() if ln.strip()]
		lines = lines[-tail_n:]

		if not pretty:
			return jsonify({"ok": True, "lines": lines, "tail": tail_n})

		# pretty: JSONL을 파싱해 사람이 읽기 쉬운 형태로 반환
		events = []
		for ln in lines:
			try:
				obj = json.loads(ln)
			except Exception:
				continue
			if not isinstance(obj, dict):
				continue
			ts = obj.get("ts")
			try:
				# runner는 ts를 epoch seconds로 저장
				ts_f = float(ts) if ts is not None else None
			except Exception:
				ts_f = None
			if ts_f:
				try:
					dt = datetime.fromtimestamp(ts_f, tz=TZ).strftime("%Y-%m-%d %H:%M:%S")
				except Exception:
					dt = ""
			else:
				dt = ""

			ev = {
				"dt": dt,
				"msg": str(obj.get("msg") or ""),
				"exchange": str(obj.get("exchange") or ""),
				"stk_cd": str(obj.get("stk_cd") or ""),
				"ord_no": str(obj.get("ord_no") or ""),
				"reason": str(obj.get("reason") or ""),
				"px": str(obj.get("px") or ""),
			}
			# 공백 정리
			ev = {k: (v.strip() if isinstance(v, str) else v) for k, v in ev.items()}
			events.append(ev)

		return jsonify({"ok": True, "tail": tail_n, "events": events})
	except Exception as e:
		return jsonify({"ok": False, "error": "ServerError", "detail": str(e)}), 500


@app.get("/api/quote")
def api_quote():
	q = (request.args.get("ticker") or "").strip()
	ex = (request.args.get("exchange") or DEFAULT_EXCHANGE).strip().upper()
	try:
		ticker, name, err = resolve_ticker(q)
		if err or not ticker:
			return jsonify({"ok": False, "error": err or "종목명을 다시확인해 주세요"}), 400
		provider = (MARKETDATA_PROVIDER or "").upper()
		if os.getenv("KIWOOM_MARKETDATA_PROVIDER") is None and os.getenv("KIWOOM_USE_KIWOOM_MARKETDATA") is not None:
			provider = "KIWOOM" if USE_KIWOOM_MARKETDATA else "PUBLIC"

		if provider == "MOCK":
			qd = mock_best_bid_ask(ticker)
			return jsonify({"ok": True, "ticker": ticker, "name": name or "", "exchange": ex, **qd})

		if provider == "PUBLIC":
			try:
				# PUBLIC에서는 "현재가 기반"으로 bid/ask를 근사합니다.
				d = fetch_ohlcv_90d_public(ticker, days=30)
				last_close = None
				try:
					last_close = int(d.get("ohlcv", [])[0].get("close") or 0)  # 최신이 앞
				except Exception:
					last_close = None
				qd = mock_best_bid_ask(ticker, ref_price=last_close or _mock_base_price(ticker))
				return jsonify({"ok": True, "ticker": ticker, "name": name or "", "exchange": ex, **qd, "note": f"PUBLIC quote approx ({d.get('meta',{}).get('used_api','?')})"})
			except Exception as e:
				if ALLOW_MOCK_FALLBACK:
					qd = mock_best_bid_ask(ticker)
					return jsonify({"ok": True, "ticker": ticker, "name": name or "", "exchange": ex, **qd, "note": "PUBLIC failed → mock", "error": str(e)})
				raise

		# KIWOOM
		if not APP_KEY or not APP_SECRET:
			return jsonify({"ok": False, "error": "ServerMisconfigured"}), 500
		token = get_token(APP_KEY, APP_SECRET)
		stk_cd = _format_stk_cd(ticker, ex)
		qd = fetch_best_bid_ask(token, stk_cd, stex_tp=ex)
		return jsonify({"ok": True, "ticker": ticker, "name": name or "", "exchange": ex, **qd})
	except Exception as e:
		return jsonify({"ok": False, "error": "ServerError", "detail": str(e)}), 500


# ============================================================
# UI
# ============================================================
@app.get("/")
def index():
	return render_template(
		"buysell.html",
		host=HOST,
		dry_run=DRY_RUN,
		open_hhmm=MARKET_OPEN_HHMM,
		asset_ver=int(time.time()),
	)


if __name__ == "__main__":
	app.run(host="0.0.0.0", port=7788, debug=False)

