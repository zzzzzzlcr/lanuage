"""Post-generation auto-fixer. Rules-based, no LLM call. Fixes common LLM mistakes."""

import re

# ── Type inference from field labels ─────────────────────────────
TYPE_KEYWORDS = {
    "email": ["email", "邮箱", "e-mail", "mail"],
    "password": ["password", "密码", "pass"],
    "tel": ["phone", "手机", "电话", "phone number", "mobile"],
    "number": ["zip", "postal", "邮编", "age", "年龄"],
}


def infer_type(label: str) -> str:
    """Infer HTML input type from label text."""
    label_lower = label.lower()
    for typ, keywords in TYPE_KEYWORDS.items():
        for kw in keywords:
            if kw in label_lower:
                return typ
    return "text"


# ── Main fixer ────────────────────────────────────────────────────

def fix(config: dict) -> dict:
    """Apply all rule-based fixes. Returns fixed config (mutates input)."""
    _fix_steps(config)
    _fix_success(config)
    _fix_loop_until(config)
    _fix_missing_wait(config)
    return config


def _fix_steps(config):
    """Fix common step-level issues."""
    steps = config.get("steps", [])
    for step in steps:
        _fix_field_types(step)
        _fix_wait_eval(step)
        _fix_button_eval(step)
        _fix_field_exists_type(step)
        _fix_duration_ms(step)
        _remove_form_id(step)


def _fix_field_types(step):
    """Ensure field.type is present."""
    field = step.get("field", {})
    if field and not field.get("type"):
        label = field.get("label", "")
        field["type"] = infer_type(label)


def _fix_field_exists_type(step):
    """Ensure field_exists also has type."""
    fe = (step.get("when", {}) or {}).get("field_exists", {})
    if fe and not fe.get("type"):
        label = fe.get("label", "")
        fe["type"] = infer_type(label)


def _fix_wait_eval(step):
    """Convert eval-based wait (Promise/setTimeout) to proper wait step."""
    if step.get("action") != "eval":
        return
    script = step.get("script", "")
    if "Promise" in script or "setTimeout" in script or "Date.now" in script:
        step["action"] = "wait"
        step["min"] = 1
        step["max"] = 3
        step.pop("script", None)
        step.pop("optional", None)


def _fix_button_eval(step):
    """Only convert click to eval for iframe buttons (CDP click fails in iframes).
    Regular buttons should keep structured click with find field."""
    if step.get("action") != "click":
        return
    # Only convert for iframe steps — keep structured click for everything else
    field = step.get("field", {}) or {}
    find = step.get("find", {}) or {}
    has_frame = (step.get("frame_url") or field.get("frame_url") or find.get("frame_url"))
    if not has_frame:
        return  # preserve structured click for non-iframe buttons
    text = find.get("text", "")
    if not text:
        return
    esc = text.replace("'", "\\'")
    step["action"] = "eval"
    step["script"] = (
        f"var bs=document.querySelectorAll('button');"
        f"for(var i=0;i<bs.length;i++){{"
        f"if(bs[i].textContent.trim()==='{esc}'&&bs[i].offsetWidth>0)"
        f"{{bs[i].click();break;}}}}"
    )
    step.pop("find", None)
    step.pop("retry", None)


def _fix_duration_ms(step):
    """Convert duration in ms to seconds (min/max)."""
    dur = step.get("duration", {})
    if dur:
        step["min"] = dur.get("min", 1000) / 1000
        step["max"] = dur.get("max", 3000) / 1000
        step.pop("duration", None)


def _remove_form_id(step):
    """Keep id on form steps — state machine needs them for deduplication.
    Previously removed id to allow retry, but field_exists+when handles this properly."""
    pass  # no-op: keep form step ids intact


def _fix_success(config):
    """Normalize success conditions."""
    succ = config.get("success", {})
    if not succ:
        return

    # Flat format: {"url_contains": "xxx"} → {"url_contains": ["xxx"]}
    if isinstance(succ.get("url_contains"), str):
        succ["url_contains"] = [succ["url_contains"]]

    # Wrap any conditions with "any"
    conditions = succ.get("any", [])
    if not conditions:
        # Check for flat conditions
        flat = {k: v for k, v in succ.items() if k != "any"}
        if flat:
            succ["any"] = [flat]
            for k in flat:
                del succ[k]


def _fix_loop_until(config):
    """Normalize loop_until format."""
    lu = config.get("loop_until", {})
    if not lu:
        return

    # "or" → "any"
    if "or" in lu and "any" not in lu:
        lu["any"] = lu.pop("or")

    # Flat format → wrap
    if "any" not in lu:
        flat = {k: v for k, v in lu.items() if k != "or"}
        if flat:
            lu["any"] = [flat]
            for k in flat:
                del lu[k]


def _fix_missing_wait(config):
    """Ensure first step is a wait, ensure there's a wait after submit."""
    steps = config.get("steps", [])
    if not steps:
        return

    # Add initial wait if missing
    if steps[0].get("action") != "wait":
        steps.insert(0, {"action": "wait", "min": 2, "max": 4})

    # Add final wait if last step is a click/eval/form
    last = steps[-1]
    if last.get("action") in ("click", "eval", "form"):
        steps.append({"action": "wait", "min": 5, "max": 10})
