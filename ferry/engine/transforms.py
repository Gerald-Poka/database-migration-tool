"""ferry.engine.transforms - column transform registry.

Builtins (use as plain strings in plan `transforms:`):
  pass | strip | upper | lower | null_if_empty | safe_date | safe_json | int | float
  bool_int | epoch_to_ts | offset:<n>

Custom module reference:  {"module": "helpers.py", "fn": "fix_phone"}
Parametrized builtin:     {"fn": "offset", "args": [800000]}  (same as "offset:800000")

`load_custom` compiles a mapping's transforms into col -> callable row functions.
"""

from __future__ import annotations

import decimal
import importlib.util
import json
import re
from datetime import date, datetime, timezone
from pathlib import Path

_PARTIAL_DATE = re.compile(r"^\d{4}-\d{2}-00$")


def _upper(v):
    return v.upper() if isinstance(v, str) else v


def _lower(v):
    return v.lower() if isinstance(v, str) else v


def _strip(v):
    return v.strip() if isinstance(v, str) else v


def _null_if_empty(v):
    if v is None:
        return None
    if isinstance(v, str) and not v.strip():
        return None
    return v


def _safe_date(v):
    """Partial/broken dates (e.g. '2000-01-00') become None."""
    if isinstance(v, str) and _PARTIAL_DATE.match(v.strip()):
        return None
    return v


def _safe_json(v):
    """Normalize JSON / double-encoded JSON into a clean JSON string; non-JSON strings kept verbatim."""
    if v is None or isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False, separators=(",", ":")) if v is not None else None
    if isinstance(v, (datetime, date, decimal.Decimal)):
        return str(v)
    s = str(v).strip()
    if not s:
        return None
    try:
        obj = json.loads(s)
    except Exception:
        return s  # plain text kept as text
    for _ in range(2):  # peel double-encoding
        if isinstance(obj, str):
            try:
                obj = json.loads(obj)
            except Exception:
                break
    if obj is None:
        return None
    if isinstance(obj, (dict, list)):
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return str(obj)


def _bool_int(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return int(bool(v))
    s = str(v).strip().lower()
    if s in ("1", "true", "t", "yes", "y", "on"):
        return 1
    if s in ("0", "false", "f", "no", "n", "off", ""):
        return 0
    try:
        return int(bool(int(s)))
    except ValueError:
        return 0


def _epoch_to_ts(v):
    """Epoch seconds or millis (int/str) -> ISO-8601 UTC; None for absent or bogus values."""
    if v is None or v == "":
        return None
    s = str(v).strip()
    try:
        n = int(s)
    except ValueError:
        try:
            n = int(float(s))
        except ValueError:
            return v
    if n <= 0:
        return None
    ts = n / 1000 if n > 1_000_000_000_000 else n
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return None


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _make_offset(amount: int):
    def _offset(v):
        if v is None or v == "":
            return None
        try:
            return int(v) + amount
        except (TypeError, ValueError) as e:
            raise ValueError(f"offset transform can't apply to non-integer id value {v!r}") from e

    return _offset


#: parametrizable builtins, allowed extra args
PARAM_BUILTINS = {"offset": _make_offset}

BUILTINS: dict[str, object] = {
    "pass": None,
    "strip": _strip,
    "upper": _upper,
    "lower": _lower,
    "null_if_empty": _null_if_empty,
    "safe_date": _safe_date,
    "safe_json": _safe_json,
    "int": _int,
    "float": _float,
    "bool_int": _bool_int,
    "epoch_to_ts": _epoch_to_ts,
}


def compile_transforms(spec, plan_dir: Path) -> dict[str, object]:
    """spec: mapping's `transforms:` dict; returns {target_col: callable}."""
    mods: dict[str, object] = {}
    out: dict[str, object] = {}
    for col, tv in (spec or {}).items():
        out[col] = _compile_one(tv, plan_dir, mods)
    return out


def _compile_one(tv, plan_dir: Path, mods) -> object:
    if tv is None:
        return None
    if isinstance(tv, str):
        if tv.startswith("offset:"):
            return _make_offset(int(tv.split(":", 1)[1]))
        name = tv.strip()
        if name not in BUILTINS:
            raise ValueError(f"unknown builtin transform {tv!r}")
        return BUILTINS[name]
    if isinstance(tv, dict):
        if "module" in tv:
            mod_path = tv["module"]
            base = plan_dir.resolve()
            resolved = (base / mod_path).resolve()
            if not resolved.is_relative_to(base):
                raise ValueError(f"custom transform module must stay next to the plan: {mod_path!r}")
            key = str(resolved)
            if key not in mods:
                mname = "_ferry_helpers_" + re.sub(r"\W", "_", key)
                spec = importlib.util.spec_from_file_location(mname, key)
                if spec is None or spec.loader is None:
                    raise ValueError(f"can't load custom transform module {mod_path!r}")
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                mods[key] = module
            return getattr(mods[key], tv["fn"])
        if "fn" in tv and tv["fn"] in PARAM_BUILTINS:
            return PARAM_BUILTINS[tv["fn"]](int(*tv.get("args", [0])))
        if "fn" in tv:
            return _compile_one(tv["fn"], plan_dir, mods)
    raise ValueError(f"bad transform spec {tv!r} for column")
