from flask import Flask, request, jsonify, render_template
import os
import io
import re
from html.parser import HTMLParser
from html import unescape

try:
	# python-dotenv (선택 의존성): .env 파일에서 환경변수 로드
	from dotenv import load_dotenv  # type: ignore
	load_dotenv()
except Exception:
	# python-dotenv가 없거나 .env가 없어도, 기존 환경변수 방식으로 동작
	pass
import time
import random
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urlparse

HOST = os.getenv("KIWOOM_HOST") or "https://api.kiwoom.com"
TZ = ZoneInfo(os.getenv("KIWOOM_TZ") or "Asia/Seoul")

app = Flask(__name__)

# ✅ KIS 예제처럼 상수로 두되, 실전에서는 env 권장
# - 우선순위: KIWOOM_APP_KEY/SECRET > APP_KEY/SECRET
APP_KEY = os.getenv("KIWOOM_APP_KEY") or os.getenv("APP_KEY") or ""
APP_SECRET = os.getenv("KIWOOM_APP_SECRET") or os.getenv("APP_SECRET") or ""


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


HTTP_TIMEOUT_SEC = _env_float("KIWOOM_HTTP_TIMEOUT_SEC", 20.0, min_value=3.0, max_value=60.0)
API_MAX_RETRY = _env_int("KIWOOM_API_MAX_RETRY", 6, min_value=1, max_value=20)
API_BASE_SLEEP_SEC = _env_float("KIWOOM_API_BASE_SLEEP_SEC", 0.08, min_value=0.0, max_value=2.0)
TR_MAX_RETRY = _env_int("KIWOOM_TR_MAX_RETRY", 2, min_value=0, max_value=10)
TR_RETRY_BASE_SLEEP_SEC = _env_float("KIWOOM_TR_RETRY_BASE_SLEEP_SEC", 0.25, min_value=0.0, max_value=3.0)

# 키움 API 호출은 시스템/환경 프록시를 타지 않도록 별도 세션 사용
# (로컬 프록시 403으로 토큰 발급 실패하는 환경 대응)
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


# =========================
# Ticker resolver (name -> 6-digit code)
# =========================
_krx_cache = {
	"loaded_at": None,   # datetime
	"by_name": None,     # dict[str, str] normalized_name -> code
	"name_by_code": None # dict[str, str] code -> original_name
}

# 자주 쓰는 영문/약칭 보정 (정확 키 매칭만 허용)
_NAME_ALIAS_TO_CODE: dict[str, str] = {
	"kt": "030200",
	"lselectric": "010120",
}


def _norm_name(s: str) -> str:
	# 공백/탭 제거 + 소문자 + 괄호/특수문자 일부 제거 (매칭률↑)
	s = (s or "").strip().lower()
	# 제로폭 공백/문자(BOM 포함) 제거
	s = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", s)
	s = re.sub(r"\s+", "", s)
	s = re.sub(r"[·\.\,\(\)\[\]\-_/&']", "", s)
	return s


def _load_krx_name_map(force=False):
	"""
	KRX 상장법인 목록(회사명/종목코드) 다운로드 후 캐시.
	- 종목명 입력만으로도 조회 가능하게 하기 위한 보조 기능(키움 TR 아님)
	"""
	now = datetime.now()
	if not force and _krx_cache["loaded_at"] and _krx_cache["by_name"] and _krx_cache["name_by_code"]:
		# 하루에 한 번만 갱신
		if (now - _krx_cache["loaded_at"]).total_seconds() < 24 * 3600:
			return

	url = "https://kind.krx.co.kr/corpgeneral/corpList.do?method=download"
	r = _http_get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
	r.raise_for_status()

	# 응답은 CSV가 아니라 "HTML table" 형태(엑셀 MIME)로 내려옵니다.
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
			self.rows = []
			self._in_tr = False
			self._in_cell = False
			self._cell_buf = []
			self._cur_row = []

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
				# 빈 row 제외
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

	by_name = {}
	name_by_code = {}

	for r in rows[1:]:
		# 행 길이가 헤더보다 짧을 수 있어 방어
		if len(r) <= max(i_name, i_code):
			continue
		name = (r[i_name] or "").strip()
		code = (r[i_code] or "").strip()
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


def resolve_ticker(query: str) -> tuple[str | None, str | None]:
	"""
	입력값이 종목코드(6자리)이면 그대로 반환.
	종목명이면 KRX 목록을 통해 종목코드로 변환.
	"""
	q = (query or "").strip()
	if re.fullmatch(r"\d{6}", q):
		return (q, None)

	nq = _norm_name(q)
	if not nq:
		return (None, "종목명을 다시확인해 주세요")
	if nq in _NAME_ALIAS_TO_CODE:
		return (_NAME_ALIAS_TO_CODE[nq], None)

	_load_krx_name_map()
	by_name = _krx_cache["by_name"] or {}

	# 1) 정확 매칭
	if nq in by_name:
		return (by_name[nq], None)

	# 2) 부분 매칭(유일할 때만 허용)
	cands = [(name_norm, code) for (name_norm, code) in by_name.items() if nq in name_norm]
	if len(cands) == 1:
		return (cands[0][1], None)

	return (None, "종목명을 다시확인해 주세요")


@app.get("/api/resolve-ticker")
def api_resolve_ticker():
	q = (request.args.get("q") or "").strip()
	try:
		ticker, err = resolve_ticker(q)
		if err or not ticker:
			return jsonify({"ok": False, "error": err or "종목명을 다시확인해 주세요"}), 404
		name = ""
		try:
			name = (_krx_cache.get("name_by_code") or {}).get(ticker, "")
		except Exception:
			name = ""
		return jsonify({"ok": True, "ticker": ticker, "name": name})
	except Exception as e:
		return jsonify({"ok": False, "error": "ResolverError", "detail": str(e)}), 500


# =========================
# UI
# =========================
@app.get("/")
def index():
	"""
	브라우저에서 수급 데이터를 표/차트로 보기 위한 간단한 UI.
	"""
	return render_template("index.html")


# =========================
# Auth
# =========================
def get_token(appkey: str, secretkey: str) -> str:
	url = HOST + "/oauth2/token"
	body = {
		"grant_type": "client_credentials",
		"appkey": appkey,
		"secretkey": secretkey,
	}
	r = _http_post(url, json=body, headers={"Content-Type": "application/json;charset=UTF-8"})
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
			hint = ""
			if "8050" in rm or "지정단말기" in rm:
				hint = " (힌트: 키움 API센터/HTS에서 지정단말기 인증이 필요합니다.)"
			raise RuntimeError(f"Token error: {rm} (return_code={data.get('return_code')}){hint}")

	if "token" in data:
		return data["token"]
	if "access_token" in data:
		return data["access_token"]

	raise RuntimeError(f"Token error: {data}")


# =========================
# Helpers
# =========================
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
	except:
		return default


def _parse_dt_any(v):
	if v is None:
		return None
	s = str(v).strip()
	if s == "":
		return None

	if len(s) >= 8 and s[:8].isdigit():
		y = s[:4]
		m = s[4:6]
		d = s[6:8]
		return f"{y}-{m}-{d}"

	if len(s) >= 10 and s[4] == "-" and s[7] == "-":
		return s[:10]

	return None


def fetch_ohlcv_close_map(token: str, stk_cd: str, days=200) -> dict[str, dict]:
	"""
	종가/전일대비를 ka10081/ka10086에서 가져와 날짜로 매핑합니다.
	run.py의 수급(ka10060) 응답에는 "종가"가 항상 정확히 포함되지 않을 수 있어,
	종가 차트/테이블의 정확도를 위해 별도 TR로 보강합니다.
	"""
	end_dt = datetime.now(TZ).strftime("%Y%m%d")
	stex_tp = (os.getenv("KIWOOM_DMST_STEX_TP") or "KRX").strip().upper()
	upd_stkpc_tp = (os.getenv("KIWOOM_OHLCV_UPD_STKPC_TP") or "1").strip()
	candidates = ["ka10081", "ka10086"]
	endpoint = "/api/dostk/chart"

	last_err: Exception | None = None
	rows = []
	used = None
	for api_id in candidates:
		bodies = [
			{"stk_cd": stk_cd, "base_dt": end_dt, "upd_stkpc_tp": upd_stkpc_tp, "stex_tp": stex_tp, "dmst_stex_tp": stex_tp},
			{"stk_cd": stk_cd, "base_dt": end_dt, "stex_tp": stex_tp, "dmst_stex_tp": stex_tp},
			{"stk_cd": stk_cd, "dt": end_dt, "stex_tp": stex_tp, "dmst_stex_tp": stex_tp},
		]
		for body in bodies:
			try:
				res = call_tr_all_pages(token, api_id=api_id, body=body, endpoint=endpoint, max_pages=10)
				rows = res["rows"]
				used = (api_id, body)
				if rows:
					break
			except Exception as e:
				last_err = e
				continue
		if rows:
			break

	if not rows:
		raise last_err if last_err else RuntimeError("OHLCV close map fetch failed")

	# rows는 최신이 먼저인 경우가 많아 보강: 날짜별 마지막 값 우선
	m: dict[str, dict] = {}
	for r in rows[: max(100, int(days) * 3)]:
		dt = _parse_dt_any(_first_non_empty(r, ["dt", "date", "bas_dt", "base_dt", "trde_dt", "trd_dt"]))
		if not dt:
			continue
		close_pric = _to_int(_first_non_empty(r, ["close_pric", "close", "stck_clpr", "cur_prc", "cur_pric"]), 0)
		pre = _to_int(_first_non_empty(r, ["pred_pre", "pre", "prdy_vrss"]), 0)
		if close_pric <= 0:
			continue
		m[dt] = {"close_pric": int(close_pric), "pre": int(pre), "used_api": used[0] if used else ""}
	return m


def fetch_ohlcv_ohlc_map(token: str, stk_cd: str, days=260) -> dict[str, dict]:
	"""
	전략 시뮬레이션용: 날짜별 OHLC를 ka10081(일봉)에서 가져와 매핑합니다.
	- buy: 신호일 다음 거래일 '시가'
	- eval: 이후 매 거래일 고가/저가로 MFE/MAE 계산(현실성↑)
	"""
	end_dt = datetime.now(TZ).strftime("%Y%m%d")
	stex_tp = (os.getenv("KIWOOM_DMST_STEX_TP") or "KRX").strip().upper()
	upd_stkpc_tp = (os.getenv("KIWOOM_OHLCV_UPD_STKPC_TP") or "1").strip()
	api_id = "ka10081"
	endpoint = "/api/dostk/chart"

	bodies = [
		{"stk_cd": stk_cd, "base_dt": end_dt, "upd_stkpc_tp": upd_stkpc_tp, "stex_tp": stex_tp, "dmst_stex_tp": stex_tp},
		{"stk_cd": stk_cd, "base_dt": end_dt, "stex_tp": stex_tp, "dmst_stex_tp": stex_tp},
	]

	last_err: Exception | None = None
	rows = []
	for body in bodies:
		try:
			res = call_tr_all_pages(token, api_id=api_id, body=body, endpoint=endpoint, max_pages=10)
			rows = res["rows"]
			if rows:
				break
		except Exception as e:
			last_err = e
			continue

	if not rows:
		raise last_err if last_err else RuntimeError("OHLCV open/close map fetch failed")

	m: dict[str, dict] = {}
	for r in rows[: max(160, int(days) * 3)]:
		dt = _parse_dt_any(_first_non_empty(r, ["dt", "date", "bas_dt", "base_dt", "trde_dt", "trd_dt"]))
		if not dt:
			continue
		open_pric = _to_int(_first_non_empty(r, ["open_pric", "open", "stck_oprc", "opn_prc"]), 0)
		high_pric = _to_int(_first_non_empty(r, ["high_pric", "high", "stck_hgpr", "hgh_prc"]), 0)
		low_pric = _to_int(_first_non_empty(r, ["low_pric", "low", "stck_lwpr", "low_prc"]), 0)
		close_pric = _to_int(_first_non_empty(r, ["close_pric", "close", "stck_clpr", "cur_prc", "cur_pric"]), 0)
		if open_pric <= 0 or high_pric <= 0 or low_pric <= 0 or close_pric <= 0:
			continue
		m[dt] = {"open": int(open_pric), "high": int(high_pric), "low": int(low_pric), "close": int(close_pric)}
	return m


def _simulate_from_matched_dates(
	matched_dates: list[str],
	ohlcv_map: dict[str, dict],
) -> dict:
	"""
	matched_dates의 다음 거래일 시가에 매수하고, 이후 각 거래일 종가 기준 수익률을 계산.
	- 최대 수익률/날짜
	- 최대 손실률(최저)/날짜
	"""
	dates = sorted([d for d in (ohlcv_map or {}).keys() if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d)])
	if not dates:
		return {"rows": [], "note": "OHLCV 데이터가 없습니다."}

	idx_by_dt = {d: i for i, d in enumerate(dates)}
	rows = []

	for sig in (matched_dates or []):
		sig_dt = _parse_dt_any(sig)
		if not sig_dt or sig_dt not in idx_by_dt:
			continue
		i = idx_by_dt[sig_dt]
		if i + 1 >= len(dates):
			continue
		buy_dt = dates[i + 1]
		buy_open = int((ohlcv_map.get(buy_dt) or {}).get("open") or 0)
		if buy_open <= 0:
			continue

		# MFE/MAE (고가/저가 기준)
		best = {"ret": -10**9, "dt": ""}   # 최대 수익률(MFE)
		worst = {"ret": 10**9, "dt": ""}   # 최대 손실률(MAE, 가장 낮은 수익률)
		last_ret_close = None
		last_dt = dates[-1]

		for j in range(i + 1, len(dates)):
			dt = dates[j]
			row = ohlcv_map.get(dt) or {}
			hi = int(row.get("high") or 0)
			lo = int(row.get("low") or 0)
			cl = int(row.get("close") or 0)
			if hi <= 0 or lo <= 0:
				continue
			ret_hi = (hi - buy_open) * 100.0 / buy_open
			ret_lo = (lo - buy_open) * 100.0 / buy_open
			if ret_hi > best["ret"]:
				best = {"ret": ret_hi, "dt": dt}
			if ret_lo < worst["ret"]:
				worst = {"ret": ret_lo, "dt": dt}
			if cl > 0:
				last_ret_close = (cl - buy_open) * 100.0 / buy_open

		rows.append(
			{
				"signal_dt": sig_dt,
				"buy_dt": buy_dt,
				"buy_open": buy_open,
				"max_profit_pct": round(best["ret"], 2) if best["dt"] else None,
				"max_profit_dt": best["dt"] or None,
				"max_loss_pct": round(worst["ret"], 2) if worst["dt"] else None,
				"max_loss_dt": worst["dt"] or None,
				"latest_dt": last_dt,
				# 현재 수익률은 참고용으로 종가 기준 유지(표시 목적)
				"latest_pct": round(last_ret_close, 2) if last_ret_close is not None else None,
			}
		)

	return {
		"rows": rows,
		"assumption": "신호일 다음 거래일 시가 매수. 최대수익(MFE)=이후 고가 기준, 최대손실(MAE)=이후 저가 기준. 현재수익률은 종가 기준(참고).",
	}

def _first_non_empty(row: dict, keys: list[str]):
	"""
	응답 필드명이 케이스별로 달라질 수 있어, 후보 키 목록 중 첫 번째 유효값을 반환합니다.
	"""
	if not isinstance(row, dict):
		return None
	for k in keys:
		if k in row and str(row.get(k)).strip() != "":
			return row.get(k)
	return None


class _WrongEndpointError(RuntimeError):
	"""
	특정 api-id를 현재 endpoint(URI)에서 지원하지 않을 때(예: 1504) 사용.
	"""


def _pick_list_rows(res_json):
	if not isinstance(res_json, dict):
		return []

	for k in ["stk_frgnr", "orgn_frgnr_cont_trde_prst", "stk_invsr_orgn_chart"]:
		if k in res_json and isinstance(res_json[k], list):
			return res_json[k]

	for k, v in res_json.items():
		if k in ("return_code", "return_msg"):
			continue
		if isinstance(v, list) and (len(v) == 0 or isinstance(v[0], dict)):
			return v

	# 일부 TR은 1-depth 안에 리스트가 없고, 하위 dict에 리스트가 들어갈 수 있어 재귀로 탐색
	def _find_list(obj, depth=0):
		if depth > 4:
			return None
		if isinstance(obj, list) and (len(obj) == 0 or isinstance(obj[0], dict)):
			return obj
		if isinstance(obj, dict):
			for kk, vv in obj.items():
				if kk in ("return_code", "return_msg"):
					continue
				found = _find_list(vv, depth + 1)
				if found is not None:
					return found
		return None

	found = _find_list(res_json, 0)
	if found is not None:
		return found

	return []


def _call_tr_with_retry(
	token: str,
	api_id: str,
	body: dict,
	cont_yn="N",
	next_key="",
	endpoint="/api/dostk/frgnistt",
	max_retry: int | None = None,
	base_sleep: float | None = None,
):
	max_retry = int(max_retry if max_retry is not None else API_MAX_RETRY)
	base_sleep = float(base_sleep if base_sleep is not None else API_BASE_SLEEP_SEC)
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

	last_err = None
	for i in range(max_retry):
		time.sleep(base_sleep + random.random() * 0.15)

		try:
			r = _http_post(url, json=body, headers=headers, timeout=HTTP_TIMEOUT_SEC)

			if r.status_code == 429 or (500 <= r.status_code <= 599):
				backoff = min(8.0, (2 ** i) * 0.7) + random.random() * 0.25
				time.sleep(backoff)
				last_err = RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
				continue

			r.raise_for_status()
			return r

		except Exception as e:
			backoff = min(8.0, (2 ** i) * 0.7) + random.random() * 0.25
			time.sleep(backoff)
			last_err = e

	raise last_err if last_err else RuntimeError("Unknown request error")


def call_tr_all_pages(
	token: str,
	api_id: str,
	body: dict,
	endpoint="/api/dostk/frgnistt",
	max_pages=30,
	max_tr_retry: int | None = None,
):
	all_rows = []
	cont_yn = "N"
	next_key = ""
	max_tr_retry = int(max_tr_retry if max_tr_retry is not None else TR_MAX_RETRY)

	pages = 0
	while pages < max_pages:
		pages += 1

		tr_retry = 0
		while True:
			r = _call_tr_with_retry(
				token=token,
				api_id=api_id,
				body=body,
				cont_yn=cont_yn,
				next_key=next_key,
				endpoint=endpoint
			)

			res_json = r.json()
			# TR 자체 오류(return_code != 0)는 명확히 예외로 올려서 원인을 화면에 표시
			if isinstance(res_json, dict) and "return_code" in res_json:
				try:
					rc = int(res_json.get("return_code"))
				except Exception:
					rc = None
				if rc not in (None, 0):
					rm = str(res_json.get("return_msg") or "")
					# 1504: 해당 URI에서는 지원하는 API ID가 아닙니다.
					if "1504" in rm or "해당 URI" in rm:
						raise _WrongEndpointError(
							f"{api_id} error: {rm} (endpoint={endpoint}, return_code={res_json.get('return_code')})"
						)
					# 간헐적으로 발생하는 서버 처리 오류(예: return_code=7, [1631])는 짧게 재시도
					is_transient = (rc == 7) or ("1631" in rm) or ("서비스를 처리하는 중에 오류" in rm)
					if is_transient and tr_retry < max_tr_retry:
						backoff = TR_RETRY_BASE_SLEEP_SEC + (tr_retry * 0.45) + random.random() * 0.25
						time.sleep(backoff)
						tr_retry += 1
						continue
					raise RuntimeError(f"{api_id} error: {rm} (endpoint={endpoint}, return_code={res_json.get('return_code')})")
			break
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

	return {
		"api_id": api_id,
		"endpoint": endpoint,
		"pages": pages,
		"raw_rows": len(all_rows),
		"rows": all_rows,
	}


def call_tr_all_pages_auto_endpoint(
	token: str,
	api_id: str,
	body: dict,
	endpoints: list[str],
	max_pages=30,
):
	"""
	api-id별 endpoint가 다른 경우가 있어, 후보 endpoint를 순서대로 시도합니다.
	- 1504(URI 미지원) 에러면 다음 endpoint로 재시도
	"""
	last_err: Exception | None = None
	for ep in endpoints:
		try:
			return call_tr_all_pages(
				token=token,
				api_id=api_id,
				body=body,
				endpoint=ep,
				max_pages=max_pages,
			)
		except _WrongEndpointError as e:
			last_err = e
			continue

	# 전부 실패
	raise last_err if last_err else RuntimeError(f"{api_id} error: no valid endpoint in {endpoints}")


def _summarize_daily(daily_list):
	total = sum(x.get("net_trade_qty", 0) for x in daily_list)
	buy_days = sum(1 for x in daily_list if x.get("net_trade_qty", 0) > 0)
	sell_days = sum(1 for x in daily_list if x.get("net_trade_qty", 0) < 0)
	return {
		"total_net_trade_qty": int(total),
		"buy_dominant_days": int(buy_days),
		"sell_dominant_days": int(sell_days),
		"days": int(len(daily_list)),
	}


def _find_down2_up2_dates(daily_list: list[dict]) -> list[str]:
	"""
	연속 2일 이상 '감소' 후, 다시 연속 2일 이상 '증가'로 전환되는 날짜를 찾습니다.

	정의(가정):
	- daily_list는 {"dt": "YYYY-MM-DD", "net_trade_qty": int} 형태
	- 감소: 오늘 값 < 전일 값 (엄격하게 작을 때만)
	- 증가: 오늘 값 > 전일 값 (엄격하게 클 때만)
	- "연속 2일": 비교(증감) 2회 연속을 의미 (즉 최소 3거래일 구간)
	- 기록하는 날짜: '증가 구간이 시작되는 첫 날'의 dt
	"""
	pts: list[tuple[str, int]] = []
	for x in (daily_list or []):
		dt = _parse_dt_any(x.get("dt"))
		if not dt:
			continue
		try:
			v = int(x.get("net_trade_qty") or 0)
		except Exception:
			continue
		pts.append((dt, v))

	# 날짜 오름차순
	pts.sort(key=lambda t: t[0])
	n = len(pts)
	if n < 4:
		return []

	out: list[str] = []
	i = 1
	while i < n:
		# 감소 run
		neg = 0
		while i < n and pts[i][1] < pts[i - 1][1]:
			neg += 1
			i += 1

		if neg >= 2:
			# 증가 run
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

		# 감소 run이 없거나(neg=0) 2 미만이면 한 칸 전진
		if neg == 0:
			i += 1

	# 중복 제거(순서 유지)
	seen = set()
	uniq = []
	for d in out:
		if d in seen:
			continue
		seen.add(d)
		uniq.append(d)
	return uniq


# =========================
# Main function (dict return)
# =========================
def get_foreign_institution_daily_3m(app_key: str, app_secret: str, stk_cd: str, days=90) -> dict:
	token = get_token(app_key, app_secret)

	# ✅ 목적(단일 종목의 "일별 수급")에 가장 맞는 TR: ka10060 (종목별투자자기관별차트요청)
	# - 공식 가이드(jobTpCode=07)에 따르면 URL은 /api/dostk/chart
	# - amt_qty_tp=2(수량), trde_tp=0(순매수), unit_tp=1(단주)로 수급(순매수 수량) 조회
	end_dt = datetime.now(TZ).strftime("%Y%m%d")
	res_10060 = call_tr_all_pages(
		token=token,
		api_id="ka10060",
		body={
			"dt": end_dt,
			"stk_cd": stk_cd,
			"amt_qty_tp": "2",
			"trde_tp": "0",
			"unit_tp": "1",
		},
		endpoint="/api/dostk/chart",
		max_pages=30,
	)
	rows_chart = res_10060["rows"]

	# ✅ 종가/전일대비는 별도 일봉 TR로 보강(HTS/공식 종가와 불일치 방지)
	close_map: dict[str, dict] = {}
	try:
		close_map = fetch_ohlcv_close_map(token, stk_cd, days=max(120, days))
	except Exception:
		close_map = {}

	# 외국인 보유비중/한도소진률 등 부가정보는 ka10008에서 날짜 매핑해 보강(표 컬럼 유지용)
	res_10008 = call_tr_all_pages(
		token=token,
		api_id="ka10008",
		body={"stk_cd": stk_cd},
		endpoint="/api/dostk/frgnistt",
		max_pages=30,
	)
	rows_finfo = res_10008["rows"]
	finfo_by_dt: dict[str, str] = {}
	for r in rows_finfo:
		dt = _parse_dt_any(_first_non_empty(r, ["dt", "date", "trde_dt", "trd_dt", "bas_dt"]))
		if not dt:
			continue
		# "외인비중(%)"에 더 맞는 필드가 wght(비중)라 우선 사용, 없으면 limit_exh_rt(한도소진률) 사용
		finfo_by_dt[dt] = str(_first_non_empty(r, ["wght", "limit_exh_rt"]) or "")

	foreign_daily = []
	institution_daily = []
	for row in rows_chart:
		dt = _parse_dt_any(_first_non_empty(row, ["dt", "date", "trde_dt", "trd_dt", "bas_dt"]))
		if not dt:
			continue

		# ka10060의 가격 필드는 환경/문서 버전에 따라 "종가"가 아닐 수 있어 close_map 우선 사용
		cm = close_map.get(dt) or {}
		close_pric = int(cm.get("close_pric") or _to_int(_first_non_empty(row, ["cur_prc", "cur_pric", "close_pric", "close"]), 0))
		pre = int(cm.get("pre") or _to_int(_first_non_empty(row, ["pred_pre", "pre", "prdy_vrss"]), 0))
		frgn_qty = _to_int(_first_non_empty(row, ["frgnr_invsr", "frgnr"]), 0)
		orgn_qty = _to_int(_first_non_empty(row, ["orgn"]), 0)

		foreign_daily.append({
			"dt": dt,
			"net_trade_qty": int(frgn_qty),
			"close_pric": int(close_pric),
			"pre": int(pre),
			"frgnr_qota_rt": finfo_by_dt.get(dt, ""),
		})
		institution_daily.append({
			"dt": dt,
			"net_trade_qty": int(orgn_qty),
			"close_pric": int(close_pric),
			"pre": int(pre),
		})

	foreign_daily.sort(key=lambda x: x["dt"], reverse=True)
	institution_daily.sort(key=lambda x: x["dt"], reverse=True)

	def _unique_by_dt(xs):
		seen = set()
		out = []
		for x in xs:
			if x["dt"] in seen:
				continue
			seen.add(x["dt"])
			out.append(x)
		return out

	foreign_daily_unique = _unique_by_dt(foreign_daily)[:days]
	institution_daily_unique = _unique_by_dt(institution_daily)[:days]

	# ✅ 패턴 분석: 감소(2+) → 증가(2+) 전환 날짜
	foreign_rev = _find_down2_up2_dates(foreign_daily_unique)
	inst_rev = _find_down2_up2_dates(institution_daily_unique)
	match_rev = sorted(list(set(foreign_rev).intersection(set(inst_rev))))

	# ✅ 전략 시뮬레이션: 일치 신호 다음날 시가 매수 → 종가 기준 최대/최저 수익률
	sim = {"rows": [], "note": ""}
	try:
		ohlcv_map = fetch_ohlcv_ohlc_map(token, stk_cd, days=max(260, days + 60))
		sim = _simulate_from_matched_dates(match_rev, ohlcv_map)
	except Exception as e:
		sim = {"rows": [], "note": f"시뮬레이션 계산 실패: {e}"}

	def _range_from_daily(daily):
		if not daily:
			return ("", "")
		to_dt = daily[0]["dt"]
		from_dt = daily[-1]["dt"]
		return (from_dt, to_dt)

	f_from, f_to = _range_from_daily(foreign_daily_unique)
	i_from, i_to = _range_from_daily(institution_daily_unique)

	result = {
		"ticker": stk_cd,  # ✅ KIS 예제와 동일한 키명(호환)
		"from": f_from or i_from,
		"to": f_to or i_to,
		"unique_days": max(len(foreign_daily_unique), len(institution_daily_unique)),

		"foreign": {
			"summary": _summarize_daily(foreign_daily_unique),
			"daily": foreign_daily_unique,
		},
		"institution": {
			"summary": _summarize_daily(institution_daily_unique),
			"daily": institution_daily_unique,
		},

		"analysis": {
			"foreign_down2_up2_dates": foreign_rev,
			"institution_down2_up2_dates": inst_rev,
			"matched_down2_up2_dates": match_rev,
			"definition": "감소: 오늘<전일, 증가: 오늘>전일. 각 '연속 2일'은 증감 비교 2회 연속(최소 3거래일)이며, 기록 날짜는 증가 구간 시작일(dt).",
		},

		"simulation": sim,

		"meta": {
			"foreign_fetch": {k: res_10060[k] for k in ("api_id", "endpoint", "pages", "raw_rows")},
			"institution_fetch": {k: res_10060[k] for k in ("api_id", "endpoint", "pages", "raw_rows")},
			"foreign_extra": {k: res_10008[k] for k in ("api_id", "endpoint", "pages", "raw_rows")},
			"foreign_range": {"from": f_from, "to": f_to},
			"institution_range": {"from": i_from, "to": i_to},
			"note": "수급(일별 순매수 수량)은 ka10060(/api/dostk/chart, amt_qty_tp=2, trde_tp=0, unit_tp=1) 기반입니다. 외인비중(%) 컬럼은 ka10008의 wght(비중) 기반입니다.",
		},
	}

	return result


# =========================
# ✅ Flask API (URL 동일)
# =========================
@app.get("/api/investor-bias")
def investor_bias():
	ticker = (request.args.get("ticker") or "").strip()
	days = (request.args.get("days") or "").strip()

	if not ticker.isdigit() or len(ticker) != 6:
		return jsonify({"error": "ticker must be 6-digit numeric string (e.g., 005930)"}), 400

	if not APP_KEY or not APP_SECRET:
		return jsonify({
			"error": "ServerMisconfigured",
			"detail": "Set env vars: APP_KEY & APP_SECRET (or KIWOOM_APP_KEY & KIWOOM_APP_SECRET)"
		}), 500

	# days 파라미터는 옵션(기본 90)
	try:
		if days:
			days = int(days)
		else:
			days = 90
	except:
		days = 90

	try:
		result = get_foreign_institution_daily_3m(APP_KEY, APP_SECRET, ticker, days=days)
		return jsonify(result)

	except requests.HTTPError as e:
		body = ""
		try:
			body = e.response.text
		except:
			pass
		return jsonify({
			"error": "KIWOOM HTTPError",
			"detail": str(e),
			"response_body": body
		}), 502

	except Exception as e:
		return jsonify({"error": "ServerError", "detail": str(e)}), 500


if __name__ == "__main__":
	# app.run(host="0.0.0.0", port=7777, debug=False)
	app.run(host="0.0.0.0", port=7777, debug=False)