from __future__ import annotations

import json
import os
import queue
import re
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from flask import Flask, Response, jsonify, render_template_string, request, stream_with_context

# Reuse stable logic from existing modules
import demand as core
import simuldemand as base


app = Flask(__name__)
TZ = core.TZ

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


THEMES = ["반도체", "원자력_전력", "로봇", "방산"]

MAX_CANDIDATES = _env_int("STGDEMAND_MAX_CANDIDATES", 1500, min_value=1, max_value=1500)
WORKERS = _env_int("STGDEMAND_WORKERS", 2, min_value=1, max_value=3)
ITEM_SLEEP_SEC = _env_float("STGDEMAND_ITEM_SLEEP_SEC", 0.05, min_value=0.0, max_value=2.0)
AUTO_LOOP_SLEEP_SEC = _env_float("STGDEMAND_AUTO_LOOP_SLEEP_SEC", 20.0, min_value=5.0, max_value=120.0)
CACHE_KEEP_DAYS = _env_int("STGDEMAND_CACHE_KEEP_DAYS", 180, min_value=90, max_value=365)
INCR_FETCH_DAYS = _env_int("STGDEMAND_INCR_FETCH_DAYS", 5, min_value=2, max_value=20)
OHLC_FULL_PAGES = _env_int("STGDEMAND_OHLC_FULL_PAGES", 18, min_value=6, max_value=40)
OHLC_INCR_PAGES = _env_int("STGDEMAND_OHLC_INCR_PAGES", 2, min_value=1, max_value=10)


DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
CACHE_DIR = os.path.join(DATA_DIR, "stgdemand_cache")
SCHEDULE_PATH = os.path.join(DATA_DIR, "stgdemand_schedule.json")
AUTO_HISTORY_PATH = os.path.join(DATA_DIR, "stgdemand_auto_history.json")
os.makedirs(CACHE_DIR, exist_ok=True)


@dataclass
class _Job:
	id: str
	source: str  # manual | auto
	manual_theme: str | None
	active_themes: list[str]
	created_at: float
	days: int
	cands: list[str]
	total: int
	events: list[dict]
	q: "queue.Queue[dict]"
	done: bool = False
	error: str | None = None
	resolved_inputs: set[str] | None = None


_JOBS: dict[str, _Job] = {}
_JOBS_LOCK = threading.Lock()
_CACHE_LOCK = threading.Lock()
_SCHEDULE_LOCK = threading.Lock()
_AUTO_HISTORY_LOCK = threading.Lock()


def _emit(job: _Job, payload: dict):
	job.events.append(payload)
	try:
		job.q.put(payload, timeout=0.1)
	except Exception:
		pass


def _parse_hhmm(v: str) -> str:
	s = (v or "").strip()
	if not re.fullmatch(r"\d{2}:\d{2}", s):
		return "20:10"
	h, m = s.split(":")
	hh = max(0, min(23, int(h)))
	mm = max(0, min(59, int(m)))
	return f"{hh:02d}:{mm:02d}"


def _sanitize_theme(v: str) -> str | None:
	s = (v or "").strip()
	return s if s in THEMES else None


def _detect_theme_from_filename(filename: str) -> str | None:
	base_name = os.path.basename((filename or "").strip())
	stem = os.path.splitext(base_name)[0]
	for t in THEMES:
		if t in stem:
			return t
	return None


def _normalize_theme_items(theme_items: Any) -> dict[str, list[str]]:
	out: dict[str, list[str]] = {t: [] for t in THEMES}
	if isinstance(theme_items, dict):
		for t in THEMES:
			raw = theme_items.get(t) or []
			if isinstance(raw, list):
				seen: set[str] = set()
				clean: list[str] = []
				for x in raw:
					s = str(x).strip()
					if not s or s in seen:
						continue
					seen.add(s)
					clean.append(s)
				out[t] = clean
	return out


def _normalize_theme_enabled(theme_enabled: Any) -> dict[str, bool]:
	out = {t: False for t in THEMES}
	if isinstance(theme_enabled, dict):
		for t in THEMES:
			out[t] = bool(theme_enabled.get(t, False))
	return out


def _collect_enabled_items(cfg: dict[str, Any]) -> tuple[list[str], list[str]]:
	theme_items = _normalize_theme_items(cfg.get("theme_items"))
	theme_enabled = _normalize_theme_enabled(cfg.get("theme_enabled"))
	active_themes = [t for t in THEMES if theme_enabled.get(t, False)]
	seen: set[str] = set()
	items: list[str] = []
	for t in active_themes:
		for x in (theme_items.get(t) or []):
			s = str(x).strip()
			if not s or s in seen:
				continue
			seen.add(s)
			items.append(s)
	return items, active_themes


def _load_schedule() -> dict[str, Any]:
	with _SCHEDULE_LOCK:
		try:
			with open(SCHEDULE_PATH, "r", encoding="utf-8") as f:
				obj = json.load(f)
		except Exception:
			obj = {}
		legacy_items = [str(x).strip() for x in (obj.get("items") or []) if str(x).strip()]
		theme_items = _normalize_theme_items(obj.get("theme_items"))
		theme_enabled = _normalize_theme_enabled(obj.get("theme_enabled"))
		# 이전 단일 목록(items) 포맷 마이그레이션: 반도체 그룹으로 이관
		if all(len(theme_items.get(t) or []) == 0 for t in THEMES) and legacy_items:
			theme_items["반도체"] = legacy_items
			if bool(obj.get("enabled", False)):
				theme_enabled["반도체"] = True
		cfg = {
			"enabled": bool(obj.get("enabled", False)),
			"time_hhmm": _parse_hhmm(str(obj.get("time_hhmm") or "20:10")),
			"last_run_date": str(obj.get("last_run_date") or ""),
			"theme_items": theme_items,
			"theme_enabled": theme_enabled,
			"updated_at": str(obj.get("updated_at") or ""),
		}
		return cfg


def _save_schedule(cfg: dict[str, Any]):
	with _SCHEDULE_LOCK:
		theme_items = _normalize_theme_items(cfg.get("theme_items"))
		theme_enabled = _normalize_theme_enabled(cfg.get("theme_enabled"))
		items_union, _ = _collect_enabled_items({"theme_items": theme_items, "theme_enabled": {t: True for t in THEMES}})
		obj = {
			"enabled": bool(cfg.get("enabled", False)),
			"time_hhmm": _parse_hhmm(str(cfg.get("time_hhmm") or "20:10")),
			"last_run_date": str(cfg.get("last_run_date") or ""),
			"theme_items": theme_items,
			"theme_enabled": theme_enabled,
			# 하위 호환(읽기 전용 용도)
			"items": items_union,
			"updated_at": datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"),
		}
		tmp = SCHEDULE_PATH + ".tmp"
		with open(tmp, "w", encoding="utf-8") as f:
			json.dump(obj, f, ensure_ascii=False, indent=2)
			f.write("\n")
		os.replace(tmp, SCHEDULE_PATH)


def _load_auto_history() -> dict[str, Any]:
	with _AUTO_HISTORY_LOCK:
		try:
			with open(AUTO_HISTORY_PATH, "r", encoding="utf-8") as f:
				obj = json.load(f)
		except Exception:
			obj = {}
		runs = obj.get("runs")
		if not isinstance(runs, list):
			runs = []
		return {"runs": [r for r in runs if isinstance(r, dict)]}


def _save_auto_history(obj: dict[str, Any]):
	with _AUTO_HISTORY_LOCK:
		tmp = AUTO_HISTORY_PATH + ".tmp"
		with open(tmp, "w", encoding="utf-8") as f:
			json.dump(obj, f, ensure_ascii=False, indent=2)
			f.write("\n")
		os.replace(tmp, AUTO_HISTORY_PATH)


def _upsert_auto_run(job_id: str, patch: dict[str, Any]):
	h = _load_auto_history()
	runs = h.get("runs") or []
	found = False
	for i in range(len(runs) - 1, -1, -1):
		if str((runs[i] or {}).get("job_id") or "") == str(job_id):
			row = dict(runs[i] or {})
			row.update(patch or {})
			runs[i] = row
			found = True
			break
	if not found:
		row = {"job_id": str(job_id)}
		row.update(patch or {})
		runs.append(row)
	h["runs"] = runs[-50:]
	_save_auto_history(h)


def _latest_auto_run() -> dict[str, Any] | None:
	h = _load_auto_history()
	runs = h.get("runs") or []
	return runs[-1] if runs else None


def _cache_path(ticker: str) -> str:
	return os.path.join(CACHE_DIR, f"{ticker}.json")


def _merge_daily_rows(old_rows: list[dict], new_rows: list[dict], keep_days: int) -> list[dict]:
	by_dt: dict[str, int] = {}
	for row in (old_rows or []):
		dt = core._parse_dt_any(row.get("dt"))
		if not dt:
			continue
		by_dt[dt] = int(row.get("net_trade_qty") or 0)
	for row in (new_rows or []):
		dt = core._parse_dt_any(row.get("dt"))
		if not dt:
			continue
		by_dt[dt] = int(row.get("net_trade_qty") or 0)
	out = [{"dt": dt, "net_trade_qty": by_dt[dt]} for dt in sorted(by_dt.keys(), reverse=True)]
	return out[:keep_days]


def _merge_ohlc_map(old_map: dict[str, dict], new_map: dict[str, dict], keep_days: int) -> dict[str, dict]:
	merged = dict(old_map or {})
	for dt, v in (new_map or {}).items():
		if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(dt)):
			merged[str(dt)] = dict(v)
	dts = sorted([d for d in merged.keys() if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d)], reverse=True)
	keep = set(dts[:keep_days])
	return {d: merged[d] for d in merged.keys() if d in keep}


def _limit_daily_rows(rows: list[dict], days: int) -> list[dict]:
	d = max(1, int(days))
	xs = []
	for r in (rows or []):
		dt = core._parse_dt_any((r or {}).get("dt"))
		if not dt:
			continue
		xs.append({"dt": dt, "net_trade_qty": int((r or {}).get("net_trade_qty") or 0)})
	xs.sort(key=lambda z: z["dt"], reverse=True)  # 최신 -> 과거
	return xs[:d]


def _limit_ohlc_map(ohlc_map: dict[str, dict], days: int) -> dict[str, dict]:
	d = max(1, int(days))
	valid_dts = sorted([k for k in (ohlc_map or {}).keys() if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(k))], reverse=True)
	keep = set(valid_dts[:d])
	return {k: v for k, v in (ohlc_map or {}).items() if k in keep}


def _load_ticker_cache(ticker: str) -> dict[str, Any] | None:
	path = _cache_path(ticker)
	with _CACHE_LOCK:
		try:
			with open(path, "r", encoding="utf-8") as f:
				obj = json.load(f)
			return obj if isinstance(obj, dict) else None
		except Exception:
			return None


def _save_ticker_cache(ticker: str, payload: dict[str, Any]):
	path = _cache_path(ticker)
	with _CACHE_LOCK:
		tmp = path + ".tmp"
		with open(tmp, "w", encoding="utf-8") as f:
			json.dump(payload, f, ensure_ascii=False, indent=2)
			f.write("\n")
		os.replace(tmp, path)


def _fetch_cached_investor_ohlc(token: str, ticker: str, days: int) -> tuple[dict[str, list[dict]], dict[str, dict], str]:
	"""
	mode:
	- full: 최초/강제 전체 수집
	- incr: 캐시 기반 최근 데이터 증분 수집
	"""
	need_days = max(int(days), 90)
	now_str = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
	cache = _load_ticker_cache(ticker) or {}
	has_cache = bool(cache.get("investor") and cache.get("ohlc"))

	if not has_cache:
		inv = base._fetch_investor_daily(token, ticker, days=max(CACHE_KEEP_DAYS, need_days))
		ohlc = base._fetch_ohlc_map(token, ticker, pages=OHLC_FULL_PAGES)
		payload = {
			"ticker": ticker,
			"updated_at": now_str,
			"mode": "full",
			"investor": inv,
			"ohlc": ohlc,
		}
		_save_ticker_cache(ticker, payload)
		return inv, ohlc, "full"

	old_inv = cache.get("investor") or {}
	old_ohlc = cache.get("ohlc") or {}
	inv_new = base._fetch_investor_daily(token, ticker, days=INCR_FETCH_DAYS)
	ohlc_new = base._fetch_ohlc_map(token, ticker, pages=OHLC_INCR_PAGES)
	inv = {
		"foreign": _merge_daily_rows(old_inv.get("foreign") or [], inv_new.get("foreign") or [], CACHE_KEEP_DAYS),
		"institution": _merge_daily_rows(old_inv.get("institution") or [], inv_new.get("institution") or [], CACHE_KEEP_DAYS),
	}
	ohlc = _merge_ohlc_map(old_ohlc if isinstance(old_ohlc, dict) else {}, ohlc_new, CACHE_KEEP_DAYS)
	payload = {
		"ticker": ticker,
		"updated_at": now_str,
		"mode": "incr",
		"investor": inv,
		"ohlc": ohlc,
	}
	_save_ticker_cache(ticker, payload)
	return inv, ohlc, "incr"


def simulate_one_cached(token: str, ticker: str, days=90) -> dict[str, Any]:
	inv, ohlc_map, mode = _fetch_cached_investor_ohlc(token, ticker, days=days)
	# 캐시는 넉넉히 유지하되, 실제 계산/표시는 사용자가 지정한 기간(days)으로만 수행
	inv_limited = {
		"foreign": _limit_daily_rows(inv.get("foreign") or [], days),
		"institution": _limit_daily_rows(inv.get("institution") or [], days),
	}
	ohlc_limited = _limit_ohlc_map(ohlc_map, days)
	f_rev = base._find_down2_up2_dates(inv_limited["foreign"])
	i_rev = base._find_down2_up2_dates(inv_limited["institution"])
	matched = sorted(list(set(f_rev).intersection(set(i_rev))))
	# simuldemand 내부 시뮬레이터 함수명이 변경된 경우를 호환 처리합니다.
	if hasattr(base, "_simulate_trailing_stop"):
		sim_rows = base._simulate_trailing_stop(
			matched,
			ohlc_limited,
			stop_loss_pct=int(getattr(base, "DEFAULT_STOP_LOSS_PCT", 8)),
			trailing_drop_pct=int(getattr(base, "DEFAULT_TRAILING_DROP_PCT", 10)),
		)
	else:
		sim_rows = base._simulate_mfe_mae(matched, ohlc_limited)
	latest = sim_rows[-1] if sim_rows else None
	return {
		"ticker": ticker,
		"foreign_signal_dates": f_rev,
		"institution_signal_dates": i_rev,
		"matched_signal_dates": matched,
		"signals_count": len(matched),
		"latest": latest.__dict__ if latest else None,
		"rows": [r.__dict__ for r in sim_rows],
		"cache_mode": mode,
	}


def _process_one(token: str, q: str, days: int, job_id: str, idx: int) -> tuple[dict, bool]:
	start = time.perf_counter()
	key = f"{job_id}:{idx}"
	ticker, disp = base._resolve_to_ticker(q)
	if not ticker:
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
			"cache_mode": "-",
			"elapsed_ms": int((time.perf_counter() - start) * 1000),
		}, False)
	try:
		res = simulate_one_cached(token, ticker, days=days)
		name = q
		if re.fullmatch(r"\d{6}", str(q).strip()):
			try:
				name = (getattr(core, "_krx_cache", {}).get("name_by_code") or {}).get(ticker, q)
			except Exception:
				name = q
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
			"cache_mode": str(res.get("cache_mode") or "-"),
			"elapsed_ms": int((time.perf_counter() - start) * 1000),
		}, True)
	except Exception as e:
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
			"cache_mode": "-",
			"elapsed_ms": int((time.perf_counter() - start) * 1000),
		}, False)


def _run_job(job: _Job):
	done = 0
	ok = 0
	fail = 0
	job.resolved_inputs = set()
	started_at = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
	if job.source == "auto":
		_upsert_auto_run(
			job.id,
			{
				"source": "auto",
				"status": "running",
				"started_at": started_at,
				"finished_at": "",
				"total": int(job.total),
				"done": 0,
				"ok": 0,
				"fail": 0,
				"error": "",
			},
		)
	_emit(job, {"type": "start", "job_id": job.id, "source": job.source, "workers": WORKERS, "total": job.total, "themes": job.active_themes})
	try:
		token = core.get_token(APP_KEY, APP_SECRET)
	except Exception as e:
		job.error = str(e)
		if job.source == "auto":
			_upsert_auto_run(
				job.id,
				{
					"status": "failed",
					"finished_at": datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"),
					"done": int(done),
					"ok": int(ok),
					"fail": int(fail),
					"error": str(e),
				},
			)
		_emit(job, {"type": "error", "job_id": job.id, "error": str(e)})
		job.done = True
		_emit(job, {"type": "done", "job_id": job.id, "done": done, "ok": ok, "fail": fail, "total": job.total})
		return

	with ThreadPoolExecutor(max_workers=WORKERS) as ex:
		fmap = {ex.submit(_process_one, token, q, job.days, job.id, idx): (idx, q) for idx, q in enumerate(job.cands, start=1)}
		for fut in as_completed(fmap):
			idx, q = fmap[fut]
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
					"cache_mode": "-",
					"elapsed_ms": 0,
				}
				success = False

			if row.get("ticker"):
				job.resolved_inputs.add(str(row.get("input") or "").strip())

			done += 1
			ok += 1 if success else 0
			fail += 0 if success else 1
			_emit(job, {"type": "row", "job_id": job.id, "done": done, "ok": ok, "fail": fail, "total": job.total, "row": row})
			if ITEM_SLEEP_SEC > 0:
				time.sleep(ITEM_SLEEP_SEC)

	job.done = True
	_emit(job, {"type": "done", "job_id": job.id, "done": done, "ok": ok, "fail": fail, "total": job.total})
	if job.source == "auto":
		_upsert_auto_run(
			job.id,
			{
				"status": "done" if not job.error else "failed",
				"finished_at": datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"),
				"done": int(done),
				"ok": int(ok),
				"fail": int(fail),
				"error": str(job.error or ""),
			},
		)

	# Manual run result -> schedule 대상 종목 자동 갱신
	if job.source == "manual" and job.resolved_inputs and job.manual_theme:
		cfg = _load_schedule()
		items = [x for x in sorted(job.resolved_inputs) if x]
		theme_items = _normalize_theme_items(cfg.get("theme_items"))
		theme_items[job.manual_theme] = items
		cfg["theme_items"] = theme_items
		_save_schedule(cfg)


def _has_running_job() -> bool:
	with _JOBS_LOCK:
		return any(not j.done for j in _JOBS.values())


def _create_job(cands: list[str], days: int, source: str, manual_theme: str | None = None, active_themes: list[str] | None = None) -> _Job:
	job_id = uuid.uuid4().hex[:12]
	job = _Job(
		id=job_id,
		source=source,
		manual_theme=manual_theme,
		active_themes=list(active_themes or ([] if source == "manual" else ["(자동)"])),
		created_at=time.time(),
		days=days,
		cands=cands[:MAX_CANDIDATES],
		total=min(len(cands), MAX_CANDIDATES),
		events=[],
		q=queue.Queue(),
	)
	with _JOBS_LOCK:
		_JOBS[job_id] = job
	th = threading.Thread(target=_run_job, args=(job,), daemon=True)
	th.start()
	return job


def _auto_scheduler_loop():
	while True:
		try:
			cfg = _load_schedule()
			if not cfg.get("enabled"):
				time.sleep(AUTO_LOOP_SLEEP_SEC)
				continue
			now = datetime.now(TZ)
			today = now.strftime("%Y-%m-%d")
			hhmm = now.strftime("%H:%M")
			target = _parse_hhmm(str(cfg.get("time_hhmm") or "20:10"))
			if hhmm >= target and cfg.get("last_run_date") != today:
				items, active_themes = _collect_enabled_items(cfg)
				if items and not _has_running_job():
					job = _create_job(items, days=20,source="auto", active_themes=active_themes)
					# 실제 자동 실행 job이 시작된 경우에만 
					# last_run_date 갱신
					if job and job.id:
						cfg["last_run_date"] = today
						_save_schedule(cfg)
		except Exception:
			pass
		time.sleep(AUTO_LOOP_SLEEP_SEC)


HTML = """
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>STG 수급 시뮬레이터</title>
  <link rel="stylesheet" href="/static/styles.css" />
  <style>
    .mono { font-family: var(--mono); }
    .right { text-align: right; }
    .small { font-size: 12px; color: var(--muted); }
    .ok { color: var(--good); }
    .err { color: var(--bad); }
    .rowDone { background: rgba(34,197,94,0.06); }
    .rowFail { background: rgba(239,68,68,0.06); }
    .hidden { display: none; }
    .signals { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
    .sig {
      display: inline-block;
      padding: 2px 8px;
      border: 1px solid var(--border);
      border-radius: 999px;
      background: rgba(15,23,42,0.04);
      line-height: 1.2;
    }
  </style>
</head>
<body>
  <div class="container">
    <header class="header">
      <div>
        <div class="title">STG 수급 시뮬레이터 (캐시/스케줄)</div>
        <div class="subtitle">초회 전체 수집 후 매일 증분 갱신으로 실행 속도 단축</div>
      </div>
      <div class="hint">서버: <span class="mono">{{ base }}</span></div>
    </header>

    <section class="card">
      <div class="cardTitle">자동 실행 설정</div>
      <div class="controls">
        <label class="field"><span class="label">자동실행</span><input id="autoEnabled" type="checkbox" /></label>
        <label class="field"><span class="label">실행시각</span><input id="autoTime" class="input mono" type="time" value="20:10" /></label>
        <button id="btnSaveSchedule" class="btn" type="button">스케줄 저장</button>
        <button id="btnRunNow" class="btn" type="button">지금 실행</button>
      </div>
      <div class="controls" style="margin-top:8px">
        <label class="field"><span class="label">테마선택</span><label class="mono"><input type="checkbox" id="themeAll" /> 전체선택</label></label>
        <label class="mono"><input type="checkbox" class="themeChk" data-theme="반도체" /> 반도체</label>
        <label class="mono"><input type="checkbox" class="themeChk" data-theme="원자력_전력" /> 원자력_전력</label>
        <label class="mono"><input type="checkbox" class="themeChk" data-theme="로봇" /> 로봇</label>
        <label class="mono"><input type="checkbox" class="themeChk" data-theme="방산" /> 방산</label>
      </div>
      <div class="small">수동 실행 시 파일명(반도체/원자력_전력/로봇/방산)에 따라 테마별 종목이 저장됩니다.</div>
      <div class="small mono" id="scheduleInfo"></div>
    </section>
    <section class="card">
      <div class="cardTitle">최근 자동 실행 결과</div>
      <div class="small mono" id="autoRunInfo">자동 실행 이력이 없습니다.</div>
    </section>

    <section class="card">
      <div class="cardTitle">MD 파일 업로드(수동 실행)</div>
      <form method="post" action="/simulate" enctype="multipart/form-data" class="controls">
        <label class="field">
          <span class="label">파일(.md)</span>
          <input class="input mono" type="file" name="file" accept=".md,text/markdown,text/plain" required />
        </label>
        <label class="field">
          <span class="label">기간(days)</span>
          <input class="input mono" type="number" name="days" min="10" max="365" step="1" value="{{ days }}" />
        </label>
        <button class="btn" type="submit">시뮬레이션 실행</button>
      </form>
    </section>

    {% if job_id %}
    <section class="card">
      <div class="cardTitle">결과(신호 있는 종목 우선 표시)</div>
      <div class="kv" style="margin-bottom:10px">
        <div><span class="k">JOB</span><span class="v mono">{{ job_id }}</span></div>
        <div><span class="k">진행</span><span class="v mono" id="prog">0 / {{ total }}</span></div>
        <div><span class="k">성공</span><span class="v mono ok" id="okCnt">0</span></div>
        <div><span class="k">실패</span><span class="v mono err" id="failCnt">0</span></div>
        <div><span class="k">상태</span><span class="v mono" id="status">대기</span></div>
      </div>
      <div class="tableWrap">
        <table class="table" style="min-width: 1080px">
          <thead>
            <tr>
              <th>종목명</th>
              <th>종목코드</th>
              <th class="right">일치 신호 개수</th>
              <th>신호일</th>
              <th class="right">처리시간(ms)</th>
              <th>캐시</th>
              <th>보기</th>
            </tr>
          </thead>
          <tbody id="tbody"></tbody>
        </table>
      </div>
      <div id="noSignalWrap" class="hidden" style="margin-top:12px">
        <button id="btnShowNoSignal" class="btn" type="button">신호일이 없는 종목 한번에 보기 (0)</button>
        <div id="noSignalPanel" class="hidden" style="margin-top:10px">
          <div class="tableWrap">
            <table class="table" style="min-width: 920px">
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
        let done = 0, ok = 0, fail = 0, noSignalCount = 0;

        function fmtNum(v){
          if (v === null || v === undefined || v === "") return "";
          const n = Number(v);
          if (!Number.isFinite(n)) return String(v);
          return n.toLocaleString("ko-KR");
        }
        function renderSignals(signalDates){
          const xs = Array.isArray(signalDates) ? signalDates : [];
          if (!xs.length) return `<span class="small">-</span>`;
          const shown = xs.slice(-6);
          const more = xs.length - shown.length;
          const pills = shown.map(d => `<span class="sig mono">${d}</span>`).join("");
          const tail = more > 0 ? `<span class="sig mono">+${more}</span>` : "";
          return `<div class="signals">${pills}${tail}</div>`;
        }
        function refreshNoSignalButtonText() {
          if (!btnShowNoSignal || !noSignalPanel) return;
          btnShowNoSignal.textContent = noSignalPanel.classList.contains("hidden")
            ? `신호일이 없는 종목 한번에 보기 (${noSignalCount})`
            : `신호일이 없는 종목 닫기 (${noSignalCount})`;
        }
        function addRow(r){
          const jobKey = String(r.key || "");
          const isFail = (r.note && (r.note.includes("오류") || r.note.includes("해석 실패")));
          const sigCnt = Number(r.signals_count || 0);
          const hasSignal = sigCnt > 0;
          const name = r.name || r.input || r.display || "";
          const code = r.ticker || "-";
          const elapsedMs = Number(r.elapsed_ms || 0);
          const cacheMode = String(r.cache_mode || "-");
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
              <td class="right mono">${fmtNum(elapsedMs)}</td>
              <td class="mono small ${isFail ? "err" : ""}">${isFail ? (r.note || "") : "-"}</td>
            `;
            noSignalBody.appendChild(trNs);
            return;
          }
          const tr = document.createElement("tr");
          tr.className = "rowDone";
          tr.innerHTML = `
            <td class="mono">${name}</td>
            <td class="mono">${code}</td>
            <td class="right mono">${fmtNum(sigCnt)}</td>
            <td>${renderSignals(r.signal_dates || [])}</td>
            <td class="right mono">${fmtNum(elapsedMs)}</td>
            <td class="mono">${cacheMode}</td>
            <td class="mono"><button class="btn btnSmall" type="button" data-key="${jobKey}">더보기</button></td>
          `;
          tbody.appendChild(tr);

          const details = Array.isArray(r.rows) ? r.rows : [];
          const tr2 = document.createElement("tr");
          tr2.className = "detailRow hidden";
          tr2.setAttribute("data-detail", jobKey);
          tr2.innerHTML = `<td colspan="7"><div class="small" style="margin-bottom:6px"><b>상세 결과</b></div><pre class="mono small">${JSON.stringify(details, null, 2)}</pre></td>`;
          tbody.appendChild(tr2);
          const btn = tr.querySelector("button[data-key]");
          if (btn) {
            btn.addEventListener("click", () => {
              const row = tbody.querySelector(`tr[data-detail="${jobKey}"]`);
              if (!row) return;
              row.classList.toggle("hidden");
              btn.textContent = row.classList.contains("hidden") ? "더보기" : "닫기";
            });
          }
        }
        function update(){ prog.textContent = `${done} / ${total}`; okCntEl.textContent = String(ok); failCntEl.textContent = String(fail); }
        if (btnShowNoSignal) btnShowNoSignal.addEventListener("click", () => { noSignalPanel.classList.toggle("hidden"); refreshNoSignalButtonText(); });
        statusEl.textContent = "연결 중...";
        const es = new EventSource(`/stream/${jobId}`);
        es.onmessage = (ev) => {
          try {
            const msg = JSON.parse(ev.data || "{}");
            if (!msg.type) return;
            if (msg.type === "start") { statusEl.textContent = `실행 중... (worker=${msg.workers || "?"})`; return; }
            if (msg.type === "row") { done = msg.done || done; ok = msg.ok || ok; fail = msg.fail || fail; addRow(msg.row || {}); update(); return; }
            if (msg.type === "done") { done = msg.done || done; ok = msg.ok || ok; fail = msg.fail || fail; update(); statusEl.textContent = "완료"; es.close(); return; }
            if (msg.type === "error") { statusEl.textContent = "오류"; return; }
          } catch(e) {}
        };
        es.onerror = () => { statusEl.textContent = "연결 끊김(재시도 중...)"; };
      })();
    </script>
    {% endif %}
  </div>
  <script>
    (function(){
      const enabledEl = document.getElementById("autoEnabled");
      const timeEl = document.getElementById("autoTime");
      const infoEl = document.getElementById("scheduleInfo");
      const autoRunInfoEl = document.getElementById("autoRunInfo");
      const themeAllEl = document.getElementById("themeAll");
      const themeChkEls = Array.from(document.querySelectorAll(".themeChk"));
      const btnSave = document.getElementById("btnSaveSchedule");
      const btnRunNow = document.getElementById("btnRunNow");
      function getThemeEnabledFromUI(){
        const out = {};
        for (const el of themeChkEls){
          const t = el.getAttribute("data-theme");
          out[t] = !!el.checked;
        }
        return out;
      }
      function refreshThemeAll(){
        if (!themeAllEl || !themeChkEls.length) return;
        const onCnt = themeChkEls.filter(el => el.checked).length;
        themeAllEl.checked = (onCnt === themeChkEls.length);
      }
      async function loadCfg(){
        const r = await fetch("/schedule");
        const d = await r.json();
        if (!d.ok) return;
        enabledEl.checked = !!d.config.enabled;
        timeEl.value = d.config.time_hhmm || "20:10";
        const themeEnabled = d.config.theme_enabled || {};
        const themeCounts = d.theme_counts || {};
        for (const el of themeChkEls){
          const t = el.getAttribute("data-theme");
          el.checked = !!themeEnabled[t];
        }
        refreshThemeAll();
        const activeThemes = (d.active_themes || []).join(", ") || "-";
        const rawCnt = Number(d.selected_items_raw_count || 0);
        const dedupCnt = Number(d.selected_items_dedup_count || d.selected_items_count || 0);
        const dupRemoved = Math.max(0, rawCnt - dedupCnt);
        infoEl.textContent = `활성테마 ${activeThemes} · 실행대상 ${dedupCnt}개(원본합계 ${rawCnt}개, 중복제거 ${dupRemoved}개) · [반도체 ${themeCounts["반도체"] || 0}, 원자력_전력 ${themeCounts["원자력_전력"] || 0}, 로봇 ${themeCounts["로봇"] || 0}, 방산 ${themeCounts["방산"] || 0}] · 최근자동실행 ${d.config.last_run_date || "-"} · 갱신 ${d.config.updated_at || "-"}`;
        const a = d.latest_auto || null;
        if (!a) {
          autoRunInfoEl.textContent = "자동 실행 이력이 없습니다.";
        } else {
          const statusTxt = (a.status === "done") ? "완료" : (a.status === "running" ? "실행중" : "실패");
          autoRunInfoEl.textContent = `상태 ${statusTxt} · 시작 ${a.started_at || "-"} · 종료 ${a.finished_at || "-"} · 진행 ${a.done || 0}/${a.total || 0} · 성공 ${a.ok || 0} · 실패 ${a.fail || 0}${a.error ? ` · 오류 ${a.error}` : ""}`;
        }
      }
      if (themeAllEl) {
        themeAllEl.addEventListener("change", () => {
          const on = !!themeAllEl.checked;
          for (const el of themeChkEls) el.checked = on;
        });
      }
      for (const el of themeChkEls) {
        el.addEventListener("change", refreshThemeAll);
      }
      if (btnSave) btnSave.addEventListener("click", async () => {
        const payload = { enabled: !!enabledEl.checked, time_hhmm: timeEl.value || "20:10", theme_enabled: getThemeEnabledFromUI() };
        const r = await fetch("/schedule", { method:"POST", headers:{ "Content-Type":"application/json" }, body: JSON.stringify(payload) });
        const d = await r.json();
        alert(d.ok ? "저장 완료" : `실패: ${d.error || "unknown"}`);
        await loadCfg();
      });
      if (btnRunNow) btnRunNow.addEventListener("click", async () => {
        const r = await fetch("/schedule/run-now", { method:"POST" });
        const d = await r.json();
        if (!d.ok) { alert(`실패: ${d.error || "unknown"}`); return; }
        location.href = `/?job_id=${encodeURIComponent(d.job_id)}`;
      });
      loadCfg().catch(() => {});
    })();
  </script>
</body>
</html>
"""


@app.get("/")
def home():
	job_id = (request.args.get("job_id") or "").strip()
	total = 0
	if job_id:
		with _JOBS_LOCK:
			j = _JOBS.get(job_id)
		if j:
			total = j.total
	return render_template_string(HTML, base=request.host_url.rstrip("/"), job_id=job_id or None, total=total, days=90)


@app.get("/stream/<job_id>")
def stream(job_id: str):
	with _JOBS_LOCK:
		job = _JOBS.get(job_id)
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
	if not APP_KEY or not APP_SECRET:
		return "APP_KEY/APP_SECRET(.env) 설정이 필요합니다.", 500
	if _has_running_job():
		return "다른 작업이 실행 중입니다. 잠시 후 다시 시도하세요.", 409
	file = request.files.get("file")
	if not file:
		return "파일이 없습니다.", 400
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
	manual_theme = _detect_theme_from_filename(file.filename or "")
	cands = base._extract_candidates_from_md(text)[:MAX_CANDIDATES]
	job = _create_job(cands, days=days, source="manual", manual_theme=manual_theme, active_themes=[manual_theme] if manual_theme else [])
	return render_template_string(HTML, base=request.host_url.rstrip("/"), job_id=job.id, total=job.total, days=days)


@app.get("/schedule")
def get_schedule():
	cfg = _load_schedule()
	selected_items, active_themes = _collect_enabled_items(cfg)
	theme_items = _normalize_theme_items(cfg.get("theme_items"))
	theme_counts = {t: len(theme_items.get(t) or []) for t in THEMES}
	raw_active_count = sum(theme_counts.get(t, 0) for t in active_themes)
	dedup_count = len(selected_items)
	return jsonify(
		{
			"ok": True,
			"config": cfg,
			"themes": THEMES,
			"active_themes": active_themes,
			"selected_items_count": dedup_count,
			"selected_items_raw_count": raw_active_count,
			"selected_items_dedup_count": dedup_count,
			"theme_counts": theme_counts,
			"latest_auto": _latest_auto_run(),
		}
	)


@app.post("/schedule")
def set_schedule():
	payload = request.get_json(force=True) or {}
	cfg = _load_schedule()
	cfg["enabled"] = bool(payload.get("enabled", cfg.get("enabled", False)))
	cfg["time_hhmm"] = _parse_hhmm(str(payload.get("time_hhmm") or cfg.get("time_hhmm") or "20:10"))
	if isinstance(payload.get("theme_enabled"), dict):
		theme_enabled = _normalize_theme_enabled(payload.get("theme_enabled"))
		cfg["theme_enabled"] = theme_enabled
	_save_schedule(cfg)
	return jsonify({"ok": True, "config": cfg, "latest_auto": _latest_auto_run()})


@app.post("/schedule/run-now")
def run_schedule_now():
	if _has_running_job():
		return jsonify({"ok": False, "error": "Busy"}), 409
	cfg = _load_schedule()
	items, active_themes = _collect_enabled_items(cfg)
	if not items:
		return jsonify({"ok": False, "error": "NoScheduledItems"}), 400
	job = _create_job(items, days=20, source="auto", active_themes=active_themes)
	return jsonify({"ok": True, "job_id": job.id, "total": job.total})


@app.get("/health")
def health():
	cfg = _load_schedule()
	items, active_themes = _collect_enabled_items(cfg)
	theme_items = _normalize_theme_items(cfg.get("theme_items"))
	raw_active_count = sum(len(theme_items.get(t) or []) for t in active_themes)
	return jsonify({
		"ok": True,
		"workers": WORKERS,
		"max_candidates": MAX_CANDIDATES,
		"cache_keep_days": CACHE_KEEP_DAYS,
		"incr_fetch_days": INCR_FETCH_DAYS,
		"schedule_enabled": bool(cfg.get("enabled")),
		"schedule_time_hhmm": cfg.get("time_hhmm"),
		"active_themes": active_themes,
		"selected_items_count": len(items),
		"selected_items_raw_count": raw_active_count,
	})


def _start_scheduler_once():
	if getattr(_start_scheduler_once, "_started", False):
		return
	setattr(_start_scheduler_once, "_started", True)
	th = threading.Thread(target=_auto_scheduler_loop, daemon=True)
	th.start()


def _load_bsdemand_extension_once():
	"""
	stgdemand 단독 실행 시에도 bsdemand 확장(자동매수/매도 UI+스케줄러)을 자동 로드합니다.
	- 기본값: 활성화
	- 비활성화: STG_ENABLE_BSDEMAND=0
	"""
	if getattr(_load_bsdemand_extension_once, "_loaded", False):
		return
	setattr(_load_bsdemand_extension_once, "_loaded", True)
	flag = str(os.getenv("STG_ENABLE_BSDEMAND") or "1").strip().lower()
	if flag in ("0", "false", "no", "off"):
		return

	def _has_bsdemand_routes() -> bool:
		try:
			return any(getattr(r, "rule", "") == "/buy-config" for r in app.url_map.iter_rules())
		except Exception:
			return False

	try:
		# stgdemand.py를 스크립트(__main__)로 실행한 경우에도
		# bsdemand가 동일 모듈 객체를 참조하도록 별칭을 고정합니다.
		sys.modules["stgdemand"] = sys.modules[__name__]
		# 모듈명 충돌을 피하기 위해 파일 경로로 bsdemand를 로드합니다.
		from importlib.util import module_from_spec, spec_from_file_location
		from pathlib import Path

		bs_path = Path(__file__).with_name("bsdemand.py")
		spec = spec_from_file_location("bsdemand", str(bs_path))
		if spec and spec.loader:
			mod = module_from_spec(spec)
			sys.modules["bsdemand"] = mod
			spec.loader.exec_module(mod)
		else:
			raise RuntimeError("bsdemand module spec load failed")
	except Exception as e:
		print(f"[WARN] bsdemand extension load failed: {e}")
	if not _has_bsdemand_routes():
		print("[WARN] bsdemand extension loaded but /buy-config route is still missing")


_load_bsdemand_extension_once()
_start_scheduler_once()


if __name__ == "__main__":
	app.run(host="0.0.0.0", port=7791, debug=False)

