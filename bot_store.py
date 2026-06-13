from __future__ import annotations

import json
import os
import time
from typing import Any


DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
PLAN_PATH = os.path.join(DATA_DIR, "bot_plan.json")
RUNTIME_PATH = os.path.join(DATA_DIR, "bot_runtime.json")
STATE_PATH = os.path.join(DATA_DIR, "bot_state.json")
LOG_PATH = os.path.join(DATA_DIR, "bot_log.jsonl")
ENTRIES_PATH = os.path.join(DATA_DIR, "bot_entries.json")


def ensure_data_dir():
	os.makedirs(DATA_DIR, exist_ok=True)


def _read_json(path: str) -> dict[str, Any] | None:
	try:
		with open(path, "r", encoding="utf-8") as f:
			obj = json.load(f)
		return obj if isinstance(obj, dict) else None
	except FileNotFoundError:
		return None
	except Exception:
		return None


def _atomic_write_json(path: str, obj: dict[str, Any]):
	ensure_data_dir()
	tmp = path + ".tmp"
	with open(tmp, "w", encoding="utf-8") as f:
		json.dump(obj, f, ensure_ascii=False, indent=2)
		f.write("\n")
	os.replace(tmp, path)


def _append_jsonl(path: str, obj: dict[str, Any]):
	ensure_data_dir()
	with open(path, "a", encoding="utf-8") as f:
		f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def now_ts() -> float:
	return time.time()


def load_plan() -> dict[str, Any] | None:
	return _read_json(PLAN_PATH)


def save_plan(plan: dict[str, Any]):
	_atomic_write_json(PLAN_PATH, plan)


def load_runtime() -> dict[str, Any] | None:
	return _read_json(RUNTIME_PATH)


def save_runtime(rt: dict[str, Any]):
	_atomic_write_json(RUNTIME_PATH, rt)


def load_state() -> dict[str, Any] | None:
	return _read_json(STATE_PATH)


def save_state(st: dict[str, Any]):
	_atomic_write_json(STATE_PATH, st)


def load_entries() -> list[dict[str, Any]]:
	obj = _read_json(ENTRIES_PATH) or {}
	entries = obj.get("entries")
	if isinstance(entries, list):
		return [e for e in entries if isinstance(e, dict)]
	return []


def save_entries(entries: list[dict[str, Any]]):
	_atomic_write_json(
		ENTRIES_PATH,
		{
			"updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
			"entries": entries,
		},
	)


def log_event(ev: dict[str, Any]):
	ev = dict(ev)
	ev.setdefault("ts", now_ts())
	_append_jsonl(LOG_PATH, ev)

