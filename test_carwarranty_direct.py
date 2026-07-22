"""Quick test: execute a hardcoded JSON against carwarranty page.
This isolates execution from LLM generation to find where the bug is.
"""
import json, sys, time, os, logging
sys.path.insert(0, '/company/lanuage/src')
from json_pipeline import JSONPipeline, ValidationResult

logging.basicConfig(level=logging.INFO, format='%(name)s: %(message)s')

# Mock LLM that returns a fixed config (no API needed)
class FixedLLM:
    class chat:
        class completions:
            @staticmethod
            def create(*a, **kw):
                class Resp:
                    class Choice:
                        class Msg:
                            content = ""
                    choices = [Choice()]
                return Resp()

os.environ.setdefault('OPENAI_API_KEY', 'sk-fake')

# Import CDP
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'forms'))
from common import CDPHelper

WS_URL = os.environ.get('WS_URL', 'ws://127.0.0.1:9222/devtools/browser/00000000-0000-0000-0000-000000000000')

# Generate the WS URL from HTTP endpoint
import subprocess
try:
    result = subprocess.run(
        ['curl', '-s', 'http://127.0.0.1:9222/json/version'],
        capture_output=True, text=True, timeout=5
    )
    info = json.loads(result.stdout)
    ws_url = info.get('webSocketDebuggerUrl', '')
    print(f"WS URL: {ws_url[:60]}...")
except Exception as e:
    print(f"Failed to get WS URL: {e}")
    sys.exit(1)

cdp = CDPHelper(ws_url)

# The correct JSON for carwarranty
config = {
    "site": "localhost:8080/carwarranty",
    "form_type": "auto_warranty",
    "success": {
        "any": [{"body_contains": ["Your quotes are ready"]}]
    },
    "steps": [
        {"action": "wait", "min": 2, "max": 4},
        {"action": "form", "field": {"label": "Select Year"}, "value": "2020"},  # LLM bug: uses value not select
        {"action": "wait", "min": 1, "max": 2},
        {"action": "click", "find": {"text": "Continue"}},
        {"action": "wait", "min": 2, "max": 3},
        {"action": "form", "field": {"label": "Select Make"}, "value": "Toyota"},  # LLM bug: uses value not select
        {"action": "wait", "min": 1, "max": 2},
        {"action": "click", "find": {"text": "Continue"}},
        {"action": "wait", "min": 2, "max": 3},
        {"action": "form", "field": {"label": "Select Model"}, "value": "Camry"},  # LLM bug
        {"action": "wait", "min": 1, "max": 2},
        {"action": "click", "find": {"text": "Continue"}},
        {"action": "wait", "min": 2, "max": 3},
        {"action": "form", "field": {"label": "Mileage"}, "value": "45000"},
        {"action": "wait", "min": 0.5, "max": 1},
        {"action": "click", "find": {"text": "Continue"}},
        {"action": "wait", "min": 3, "max": 5},
        {"action": "form", "field": {"label": "Full Name"}, "value": "John Doe"},
        {"action": "wait", "min": 0.5, "max": 1},
        {"action": "form", "field": {"label": "Email", "type": "email"}, "value": "john@test.com"},
        {"action": "wait", "min": 0.5, "max": 1},
        {"action": "form", "field": {"label": "Phone", "type": "tel"}, "value": "1234567890"},
        {"action": "wait", "min": 0.5, "max": 1},
        {"action": "click", "find": {"text": "Get My Quote"}},
        {"action": "wait", "min": 3, "max": 5},
    ]
}

print("\n=== Executing config ===\n")
print(f"Steps: {len(config['steps'])}")

# Navigate first
cdp.eval(f"(function(){{window.location.href='http://localhost:8080/carwarranty';}})()")
time.sleep(2)

# Run each step manually and report
from locator import FieldLocator, LocatorError
locator = FieldLocator(cdp)

for i, step in enumerate(config['steps']):
    action = step.get('action', '?')
    print(f"\n--- Step {i}: {action} ---")

    if action == 'wait':
        import random
        t = random.uniform(float(step.get('min', 0.3)), float(step.get('max', 1.5)))
        print(f"  Waiting {t:.1f}s")
        time.sleep(t)
        continue

    if action == 'click':
        find = step.get('find', {})
        text = find.get('text', '')
        js = f"(function(){{var bs=document.querySelectorAll('button,a');for(var i=0;i<bs.length;i++){{if(bs[i].textContent.trim()==='{text}'&&bs[i].offsetWidth>0){{bs[i].click();return'clicked';}}}}return'not found';}})()"
        result = cdp.eval(js)
        print(f"  Click '{text}': {result}")
        continue

    if action == 'form':
        field = step.get('field', {})
        label = field.get('label', '')
        select_val = step.get('select')
        value = step.get('value')

        try:
            loc_result = locator.locate(field)
            selector = loc_result.selector
            print(f"  Locate '{label}': {selector} (strategy={loc_result.strategy})")

            # Auto-fix: if target is <select> and LLM used "value", convert to "select"
            if not select_val and value:
                esc = selector.replace("'", "\\'")
                tag = cdp.eval(
                    f"(function(){{var e=document.querySelector('{esc}');"
                    f"return e?e.tagName:'';}})()")
                if tag and tag.strip().strip('"').upper() == 'SELECT':
                    select_val = value
                    value = None
                    print(f"  [auto-fix] value→select for <select>")

            if select_val:
                cdp.form(selector, select=select_val)
                # Verify
                actual = cdp.eval(f"(function(){{var e=document.querySelector('{selector}');return e?e.value:'no elem';}})()")
                print(f"  Select '{select_val}' → value={actual}")
            elif value:
                cdp.form(selector, value=value)
                print(f"  Fill '{value}' ✓")
        except LocatorError as e:
            print(f"  ❌ FAILED: {e}")
            for a in e.attempts:
                print(f"     attempt: {a}")
            break
        except Exception as e:
            print(f"  ❌ ERROR: {e}")
            break

# Check final state
info = cdp.get_page_info()
body = cdp.eval("(function(){return document.body?document.body.innerText.substring(0,500):'';})()")
print(f"\n=== Final ===")
print(f"URL: {info.get('url', '?')}")
if 'Your quotes are ready' in body:
    print("✅ SUCCESS: 'Your quotes are ready' found!")
else:
    print(f"❌ Body: {body[:200]}")
