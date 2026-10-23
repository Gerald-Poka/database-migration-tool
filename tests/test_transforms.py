"""Transform registry tests."""

import json

import pytest

from ferry.engine.transforms import compile_transforms


def _run(spec, value):
    tbl = {"col": spec}
    fns = compile_transforms(tbl, __import__("pathlib").Path("."))
    return fns["col"](value)


def test_no_transform_means_raw():
    fns = compile_transforms({"col": None}, __import__("pathlib").Path("."))
    assert fns["col"] is None  # engine treats None as pass-through


def test_upper_lower_strip():
    assert _run("upper", "abc") == "ABC"
    assert _run("lower", "ABC") == "abc"
    assert _run("strip", "  x  ") == "x"


def test_null_if_empty():
    assert _run("null_if_empty", "   ") is None
    assert _run("null_if_empty", "x") == "x"


def test_safe_date_partial():
    assert _run("safe_date", "2000-01-00") is None
    assert _run("safe_date", "2000-01-02") == "2000-01-02"


def test_safe_json_double_encoded():
    v = json.dumps(json.dumps({"a": 1}))
    out = _run("safe_json", v)
    assert json.loads(out) == {"a": 1}
    assert _run("safe_json", "") is None
    assert _run("safe_json", "plain text") == "plain text"


def test_bool_int():
    assert _run("bool_int", "true") == 1
    assert _run("bool_int", 0) == 0
    assert _run("bool_int", None) is None


def test_epoch_to_ts():
    out = _run("epoch_to_ts", 1751990400)
    assert out.startswith("2025-")
    assert _run("epoch_to_ts", None) is None
    assert _run("epoch_to_ts", 0) is None
    out = _run("epoch_to_ts", 1751990400000)  # millis
    assert out.startswith("2025-")


def test_int_float():
    assert _run("int", "12") == 12
    assert _run("int", "x") is None
    assert _run("float", "1.5") == 1.5


def test_offset_string_and_struct():
    assert _run("offset:800000", 5) == 800005
    spec = {"fn": "offset", "args": [800000]}
    assert _run(spec, 17) == 800017


def test_offset_rejects_text():
    with pytest.raises(ValueError):
        _run("offset:10", "not-an-id")


def test_custom_module(tmp_path):
    helper = tmp_path / "helpers.py"
    helper.write_text("def fix(v):\n    return (v or '').strip().upper()\n")
    fns = compile_transforms({"col": {"module": "helpers.py", "fn": "fix"}}, tmp_path)
    assert fns["col"](" x ") == "X"


def test_custom_module_cannot_escape_plan_directory(tmp_path):
    with pytest.raises(ValueError, match="must stay next to the plan"):
        compile_transforms(
            {"col": {"module": "../helpers.py", "fn": "fix_phone"}},
            tmp_path / "plan",
        )


def test_unknown_builtin_raises():
    with pytest.raises(ValueError):
        compile_transforms({"col": "nope"}, None)
