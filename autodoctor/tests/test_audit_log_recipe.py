"""Encoding contract regressions; the boundary model is not a live HA engine."""
from __future__ import annotations

import ast
import base64
import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from autodoctor.audit_log_recipe import PATCHED_EXPRESSION, compile_repair, matches_incident
from autodoctor.repair_backup import RepairBlocked

ENTITY = "script.synthetic_audit"
SCRIPT_KEY = "synthetic_audit"


def logger_config():
    fields = {name: {} for name in ("category", "event", "entity", "old_state", "new_state", "automation", "trigger_id", "notes")}
    return {"alias": "Synthetic logger", "description": "Fixture only", "fields": fields,
            "mode": "parallel", "max": 10,
            "sequence": [
                {"variables": {"payload": {"ts": "fixture", "notes": "{{ notes | default(none) }}"},
                               "json_line": "{{ payload | to_json }}\n", "b64": "{{ json_line | base64_encode }}"}},
                {"action": "shell_command.synthetic_append", "data": {"b64": "{{ b64 }}"}}]}


class Wrapper(dict):
    """The relevant native result contract: this object is not bytes or str."""


def native_result(text):
    try:
        value = ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return text
    return Wrapper(value) if isinstance(value, dict) else value


def render_supported(expression, variables):
    """Evaluate ONLY the reviewed pipeline's variable and two fixed filters.

    Mirrors HA's documented filter contracts and native result boundary, rather
    than pretending this miniature test helper is Jinja or the complete HA engine.
    """
    expression = expression.strip().removeprefix("{{").removesuffix("}}")
    name, *filters = [part.strip() for part in expression.split("|")]
    value = variables[name]
    for operation in filters:
        if operation == "to_json":
            value = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
        elif operation == "base64_encode":
            data = value.encode("utf-8") if isinstance(value, str) else value
            value = base64.b64encode(data).decode("utf-8")
        else:
            raise AssertionError("Unknown test pipeline operation")
    return native_result(value.strip())


CASES = [
    ({"ts": "t", "category": "c", "event": "e", "entity": "x.y", "old": "off", "new": "on", "automation": "A", "trigger_id": "t", "notes": "ok"}, True),
    ({"old": None, "new": None, "trigger_id": None}, False),
    ({"old": False, "new": True}, False),
    ({"notes": 'café "quoted" \\ path\nnewline\ttab'}, True),
    ({"notes": "", "number": "0042", "value": 42}, True),
    ({"words": "true false null None"}, True),
    ({"value": "{'a': 1}", "list": "[1, 2]"}, True),
    ({"nested": {"x": [1, "café"]}}, True),
    ({"nested": {"x": [None, True]}}, False),
    ({"float": 1.25, "negative": -12}, True),
    ({"notes": "line1\nline2\r\nline3"}, True),
    ({"unicode": "☕ 汉字 🧪"}, True),
    ({"empty": [], "mapping": {}}, True),
]


@pytest.mark.parametrize("payload,old_fails", CASES)
def test_exact_compiled_fix_survives_native_variable_boundary(payload, old_fails):
    before = logger_config()
    before_vars = before["sequence"][0]["variables"]
    intermediate = render_supported(before_vars["json_line"], {"payload": payload})
    if old_fails:
        with pytest.raises(TypeError, match="a bytes-like object is required, not 'Wrapper'"):
            render_supported(before_vars["b64"], {"json_line": intermediate})
    after = compile_repair(before)
    fixed = render_supported(after["sequence"][0]["variables"]["b64"], {"payload": payload})
    decoded = base64.b64decode(fixed, validate=True).decode("utf-8")
    assert json.loads(decoded) == payload
    assert not decoded.endswith("\n")
    if not old_fails:
        assert fixed == render_supported(before_vars["b64"], {"json_line": intermediate})


def test_patch_only_removes_intermediate_and_combines_encoding():
    before = logger_config()
    expected = copy.deepcopy(before)
    del expected["sequence"][0]["variables"]["json_line"]
    expected["sequence"][0]["variables"]["b64"] = PATCHED_EXPRESSION
    original = copy.deepcopy(before)
    assert compile_repair(before) == expected
    assert before == original
    with pytest.raises(RepairBlocked):
        compile_repair(expected)


@pytest.mark.parametrize("mutate", [
    lambda c: c.update(variables={"additional": "unreviewed"}),
    lambda c: c.update(use_blueprint={"path": "something"}),
    lambda c: c.update(mode="queued"),
    lambda c: c.update(max=True),
    lambda c: c["fields"].update(extra={}),
    lambda c: c["sequence"].append({"action": "light.turn_on"}),
    lambda c: c["sequence"][0].update(continue_on_error=True),
    lambda c: c["sequence"][0]["variables"].update(other="value"),
    lambda c: c["sequence"][0]["variables"].update(json_line="{{ payload | to_json | trim }}"),
    lambda c: c["sequence"][0]["variables"].update(b64="{{ json_line | string | base64_encode }}"),
    lambda c: c["sequence"][0]["variables"].update(payload="{{ json_line }}"),
    lambda c: c["sequence"][1].update(action="script.other"),
    lambda c: c["sequence"][1].update(action="{{ service }}"),
    lambda c: c["sequence"][1].update(target={"entity_id": "light.other"}),
    lambda c: c["sequence"][1]["data"].update(b64="{{ b64 }}; extra"),
])
def test_recipe_rejects_unreviewed_shapes(mutate):
    config = logger_config()
    mutate(config)
    with pytest.raises(RepairBlocked):
        compile_repair(config)


def test_variable_order_is_part_of_recipe_precondition():
    config = logger_config()
    variables = config["sequence"][0]["variables"]
    config["sequence"][0]["variables"] = dict(reversed(list(variables.items())))
    with pytest.raises(RepairBlocked):
        compile_repair(config)


def test_recognition_does_not_confuse_old_trigger_recipe():
    assert matches_incident("TypeError: a bytes-like object is required, not 'Wrapper'")
    assert not matches_incident("has no attribute 'state'")
    assert not matches_incident("network unavailable")
