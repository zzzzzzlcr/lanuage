"""Quick test: does auto-fixer fix common LLM mistakes?"""
import json
from src.auto_fixer import fix

# Simulate common LLM mistakes
bad_config = {
    "site": "test.com",
    "success": {"url_contains": "customer_posted=true"},  # string, not array
    "steps": [
        {"action": "eval", "script": "return new Promise(r => setTimeout(r, 2000));"},  # Promise wait
        {"action": "form", "field": {"label": "Email"}},  # missing type
        {"action": "click", "find": {"text": "Subscribe"}},  # click should be eval
        {"action": "form", "field": {"label": "First Name"}, "id": "fn"},  # has id
        {"action": "form", "field": {"label": "Phone"}, "when": {"field_exists": {"label": "Phone"}}},  # field_exists missing type
    ]
}

fixed = fix(json.loads(json.dumps(bad_config)))
steps = fixed["steps"]
errors = []

# Helper to find step by field label
def find_form(label):
    for s in steps:
        if s.get("action") == "form" and s.get("field", {}).get("label") == label:
            return s
    return {}

# Check 1: success format (should be wrapped in any with array)
succ = fixed.get("success", {})
any_cond = succ.get("any", [{}])
url_contains = any_cond[0].get("url_contains", []) if any_cond else []
if not isinstance(url_contains, list):
    errors.append("url_contains not converted to array")

# Check 2: Promise wait converted
eval_steps = [s for s in steps if s.get("action") == "eval" and "Promise" in s.get("script", "")]
if eval_steps:
    errors.append("Promise wait not converted")

# Check 3: field type inferred
em = find_form("Email")
if em.get("field", {}).get("type") != "email":
    errors.append(f"Email type not set: {em.get('field', {}).get('type')}")

# Check 4: click → eval
click_steps = [s for s in steps if s.get("action") == "click"]
if click_steps:
    errors.append(f"Click not converted: {len(click_steps)} remaining")

# Check 5: id removed
fn = find_form("First Name")
if fn and "id" in fn:
    errors.append("Form step id not removed")

# Check 6: field_exists type
ph = find_form("Phone")
fe_type = ((ph.get("when", {}) or {}).get("field_exists", {}) or {}).get("type")
if fe_type != "tel":
    errors.append(f"field_exists type not set: {fe_type}")

if errors:
    print(f"FAILED: {errors}")
else:
    print("ALL FIXES PASSED")
    print(json.dumps(fixed, indent=2, ensure_ascii=False)[:800])
