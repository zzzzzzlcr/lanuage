# Select Explorer Phase 1 MV Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a behavioral dropdown explorer that selects options by observing DOM visibility changes before/after clicking triggers — without knowing component library class names.

**Architecture:** New module `src/select_explorer.py` with `ProbeSession`, `SelectExplorer.execute()` MV loop. Modified `src/json_executor.py` routes form-select steps through `_classify_select_intent()` → `SelectExplorer`. Old `_smart_form` select path preserved behind feature flag for rollback only.

**Tech Stack:** Python 3.10+, cdp binary (subprocess), existing CDPHelper, no new dependencies.

## Global Constraints

- Core explorer code must not contain `ant-`, `Mui`, `react-select`, `css-select__` strings
- `SelectOutcome.status` must never be `SELECTED` when page state hasn't changed
- Old `_smart_form` select path must not be called when Explorer returns NOT_FOUND/NOT_VERIFIED
- Fixed JSON only in Phase 1 tests — no LLM, no pipeline
- Each mock page must pass 10/10 consecutive runs
- Select "Not Existing" must return OPTION_NOT_FOUND
- Explorer must not click Submit or navigate the page

---

## File Structure

```
CREATE  src/select_explorer.py      — ProbeSession, SelectOutcome, SelectIntent,
                                       CandidateRef, SelectExplorer
MODIFY  src/json_executor.py:770-820  — Replace old select-routing with
                                       _classify_select_intent + SelectExplorer
CREATE  tests/test_select_explorer.py  — Fixed-JSON integration tests
CREATE  mock-server/templates/behavior-select-lab/index.html  — Generic mock
MODIFY  run_mock_tests.py             — Add Phase 1 test cases
```

### Task 1: Data types — SelectIntent, CandidateRef, SelectOutcome

**Files:**
- Create: `src/select_explorer.py`

**Interfaces:**
- Produces: `SelectIntent`, `CandidateRef`, `SelectOutcome` dataclasses (used by Tasks 2-6)

- [ ] **Step 1: Write the module skeleton with dataclasses**

```python
# src/select_explorer.py
"""Behavioral dropdown explorer — discovers controls by observing DOM changes."""

import time, json, logging
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SelectIntent:
    """What the operator wants to select."""
    label: str
    mode: str           # "exact" | "random"
    option: str | None  # "United States" (None when random)
    scope: dict | None = None


@dataclass
class CandidateRef:
    """A located DOM element that might be the dropdown trigger."""
    selector: str       # unique marker selector like '[data-probe="p1:c0"]'
    frame_id: str
    source: str         # "label_for" | "adjacent_text" | "aria" | ...
    confidence: float


@dataclass
class SelectOutcome:
    """Result of a select exploration."""
    status: str         # SELECTED | ALREADY_SELECTED | OPTION_NOT_FOUND
                        # | NOT_VERIFIED | AMBIGUOUS | NO_SAFE_TRIGGER
                        # | OPEN_FAILED | NO_CANDIDATE
    evidence: dict = field(default_factory=dict)
    attempts: list = field(default_factory=list)
    selected_text: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in ("SELECTED", "ALREADY_SELECTED")
```

- [ ] **Step 2: Verify module imports cleanly**

Run: `cd /company/lanuage && python3 -c "import sys; sys.path.insert(0,'src'); from select_explorer import SelectIntent, CandidateRef, SelectOutcome; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add src/select_explorer.py
git commit -m "feat: add SelectExplorer data types — SelectIntent, CandidateRef, SelectOutcome"
```

---

### Task 2: ProbeSession — marker injection and cleanup

**Files:**
- Modify: `src/select_explorer.py` (append ProbeSession class)

**Interfaces:**
- Produces: `ProbeSession(marker_prefix: str)`, `.mark_element(selector, frame_id) -> str`, `.cleanup(frame_id)` context manager
- Consumed by: Task 3 (normalize_and_mark), Task 4 (execute_mv)

- [ ] **Step 1: Add ProbeSession class**

```python
class ProbeSession:
    """Manages unique marker injection and cleanup for one exploration attempt."""

    def __init__(self, cdp, marker_prefix: str = "probe"):
        self.cdp = cdp
        self.prefix = marker_prefix
        self._counter = 0
        self._markers: list[str] = []

    def mark_element(self, selector: str, frame_id: str = "") -> str:
        """Inject a unique marker attribute onto the element. Returns the marker selector."""
        token = f"{self.prefix}:c{self._counter}"
        self._counter += 1
        marker = f'[data-{self.prefix}="{token}"]'
        esc = selector.replace("'", "\\'")
        self.cdp.eval(
            f"(function(){{var e=document.querySelector('{esc}');"
            f"if(e)e.setAttribute('data-{self.prefix}','{token}');}})()",
            frame_id)
        self._markers.append(marker)
        return marker

    def cleanup(self, frame_id: str = ""):
        """Remove all markers injected during this session."""
        if self._markers:
            self.cdp.eval(
                f"(function(){{var els=document.querySelectorAll('[data-{self.prefix}]');"
                f"for(var i=0;i<els.length;i++)els[i].removeAttribute('data-{self.prefix}');}})()",
                frame_id)
            self._markers.clear()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.cleanup()
```

- [ ] **Step 2: Test marker injection on a real page**

```python
# Quick manual test
import os, sys
os.environ['WS_URL'] = 'ws://localhost:9222/devtools/browser/...'
sys.path.insert(0, 'src')
from common import CDPHelper
from select_explorer import ProbeSession

cdp = CDPHelper(os.environ['WS_URL'])
cdp.navigate('http://localhost:8080/ant-design')
import time; time.sleep(1)

with ProbeSession(cdp, "test") as s:
    m = s.mark_element('[id*="Country" i]')
    print(f"Marker: {m}")
    # Verify marker exists
    r = cdp.eval(f"document.querySelector('{m}') ? 'found' : 'NF'")
    print(f"Found: {r}")
print("After cleanup:")
r2 = cdp.eval(f"document.querySelector('{m}') ? 'found' : 'NF'")
print(f"Still there: {r2}")
```
Expected: `Found: found`, `Still there: NF`

- [ ] **Step 3: Commit**

```bash
git add src/select_explorer.py
git commit -m "feat: add ProbeSession with marker injection and cleanup"
```

---

### Task 3: Helper functions — normalize_and_mark, snapshot, safe trigger

**Files:**
- Modify: `src/select_explorer.py` (append helper functions)

**Interfaces:**
- Produces: `normalize_and_mark(candidates, session) -> list[dict]`, `snapshot_visible_text_nodes(cdp, frame_id) -> set`, `find_single_safe_trigger(anchor, cdp, frame_id) -> dict | None`
- Consumed by: Task 4 (execute_mv)

- [ ] **Step 1: Add normalize_and_mark**

```python
def normalize_and_mark(candidates: list[CandidateRef], session: ProbeSession) -> list[dict]:
    """Inject unique markers into DOM for each candidate. Returns list of {marker, source, confidence}."""
    refs = []
    seen = set()
    for c in candidates:
        esc = c.selector.replace("'", "\\'")
        # Check if selector matches any visible element
        count = int(session.cdp.eval(
            f"(function(){{return document.querySelectorAll('{esc}').length;}})()",
            c.frame_id) or 0)
        if count == 0:
            continue
        marker = session.mark_element(c.selector, c.frame_id)
        if marker not in seen:
            seen.add(marker)
            refs.append({"marker": marker, "source": c.source, "confidence": c.confidence})
    return refs
```

- [ ] **Step 2: Add snapshot_visible_text_nodes**

```python
def snapshot_visible_text_nodes(cdp, frame_id: str = "") -> set[str]:
    """Return set of trimmed textContent for all visible leaf text nodes in option-like elements."""
    js = (
        "(function(){var nodes=new Set();"
        "var els=document.querySelectorAll('div,span,li,button,label,a,[role=option],option');"
        "for(var i=0;i<els.length;i++){"
        "if(els[i].offsetWidth>0){"
        "var t=els[i].textContent.trim();"
        "if(t.length>=2&&t.length<=80)nodes.add(t);}}"
        "return JSON.stringify(Array.from(nodes));})()"
    )
    raw = cdp.eval(js, frame_id)
    try:
        arr = json.loads(raw) if isinstance(raw, str) else raw
        return set(arr)
    except Exception:
        return set()
```

- [ ] **Step 3: Add find_single_safe_trigger**

```python
def find_single_safe_trigger(anchor: dict, cdp, frame_id: str = "") -> dict | None:
    """Search near the anchor for the most likely dropdown trigger. Returns {marker, evidence} or None."""
    marker = anchor["marker"]
    esc = marker.replace("'", "\\'")
    js = (
        f"(function(){{var a=document.querySelector('{esc}');if(!a)return'no anchor';"
        # Walk up to form control area
        f"var area=a.closest('[class*=form-item],fieldset,div');"
        f"if(!area)area=a.parentElement;"
        # Search for trigger signals
        f"var triggers=area.querySelectorAll('select,[role=combobox],[aria-haspopup=listbox],"
        f"[tabindex]:not([tabindex=\"-1\"]),[onclick],button');"
        f"var best=null;var bestScore=0;"
        f"for(var i=0;i<triggers.length;i++){{"
        f"var t=triggers[i];if(!t.offsetWidth)continue;"
        f"var score=0;"
        f"if(t.tagName==='SELECT')score+=10;"
        f"if(t.getAttribute('role')==='combobox')score+=8;"
        f"if(t.hasAttribute('aria-haspopup'))score+=6;"
        f"if(t.hasAttribute('onclick'))score+=2;"
        f"if(t.tagName==='BUTTON')score+=1;"
        # Penalize submit-like buttons
        f"var txt=t.textContent.trim().toLowerCase();"
        f"if(/submit|next|continue|search|go|sign/.test(txt))score-=5;"
        f"if(score>bestScore){{best=t;bestScore=score;}}}}"
        f"if(best){{best.setAttribute('data-probe','trigger');return'trigger';}}"
        f"return'none';}})()"
    )
    raw = cdp.eval(js, frame_id)
    if "trigger" in str(raw):
        return {"marker": "[data-probe=\"trigger\"]", "evidence": {"discovery": "signal_scan"}}
    return None
```

- [ ] **Step 4: Test helpers on ant-design**

Run manual test that normalizes a Country candidate, snapshots, finds trigger. Verify trigger is `.ant-select-selector` but code doesn't contain `ant-` string.

- [ ] **Step 5: Commit**

```bash
git add src/select_explorer.py
git commit -m "feat: add helpers — normalize_and_mark, snapshot, find_trigger"
```

---

### Task 4: execute_mv — the core behavioral loop

**Files:**
- Modify: `src/select_explorer.py` (add SelectExplorer class)

**Interfaces:**
- Produces: `SelectExplorer(cdp, log).execute(intent: SelectIntent, candidates: list[CandidateRef]) -> SelectOutcome`
- Consumed by: Task 5 (json_executor routing)

- [ ] **Step 1: Add SelectExplorer class with execute()**

```python
class SelectExplorer:
    """Discovers and selects dropdown options by observing DOM visibility changes."""

    def __init__(self, cdp, log=None):
        self.cdp = cdp
        self.log = log or logging.getLogger(__name__)

    def execute(self, intent: SelectIntent, candidates: list[CandidateRef]) -> SelectOutcome:
        """MV execution: before/after snapshot → pick from became_visible → verify."""
        attempts = []

        with ProbeSession(self.cdp, "px") as sess:
            # 1. Normalize
            refs = normalize_and_mark(candidates, sess)
            if not refs:
                return SelectOutcome("NO_CANDIDATE", attempts=attempts)

            # 2. Check native select
            native = self._try_native(refs[0], intent)
            if native:
                return native

            # 3. Find trigger
            trigger = find_single_safe_trigger(refs[0], self.cdp)
            if not trigger:
                return SelectOutcome("NO_SAFE_TRIGGER", attempts=attempts)

            # 4. Snapshot BEFORE
            before = snapshot_visible_text_nodes(self.cdp)

            # 5. Click trigger
            self.cdp.click(trigger["marker"])
            time.sleep(0.6)

            # 6. Snapshot AFTER
            after = snapshot_visible_text_nodes(self.cdp)
            became_visible = after - before

            if not became_visible:
                attempts.append({"trigger": trigger["marker"], "delta": "empty"})
                return SelectOutcome("OPEN_FAILED", attempts=attempts)

            # 7. Find option
            target = intent.option
            matches = [t for t in became_visible if t.strip().lower() == target.strip().lower()]

            if not matches:
                attempts.append({"trigger": trigger["marker"], "visible": list(became_visible)[:20]})
                return SelectOutcome("OPTION_NOT_FOUND", attempts=attempts)

            if len(matches) > 1:
                return SelectOutcome("AMBIGUOUS", attempts=attempts,
                                     evidence={"duplicates": matches})

            # 8. Click option by text (use eval to find and click)
            matched_text = matches[0].replace("'", "\\'")
            opt_js = (
                f"(function(){{var els=document.querySelectorAll('div,span,li,button,[role=option],option');"
                f"for(var i=0;i<els.length;i++){{"
                f"if(els[i].textContent.trim()==='{matched_text}'&&els[i].offsetWidth>0){{"
                f"els[i].setAttribute('data-probe','selected');els[i].click();return'clicked';}}}}"
                f"return'not found';}})()"
            )
            opt_result = self.cdp.eval(opt_js)
            if "not found" in str(opt_result):
                attempts.append({"option_text": matched_text, "click": "not found"})
                return SelectOutcome("OPTION_NOT_FOUND", attempts=attempts)

            time.sleep(0.3)

            # 9. Verify — trigger text changed or hidden input value changed
            trigger_marker = trigger["marker"].replace("'", "\\'")
            verify_js = (
                f"(function(){{var t=document.querySelector('{trigger_marker}');"
                f"if(!t)return'no trigger';"
                f"var txt=t.textContent.trim();"
                f"if(txt.toLowerCase().indexOf('{matched_text.lower()}')!==-1)return'verified';"
                f"var area=t.closest('[class*=form-item],fieldset,div');"
                f"if(!area)return'not verified';"
                f"var hidden=area.querySelector('input[type=hidden]');"
                f"if(hidden&&hidden.value.toLowerCase().indexOf('{matched_text.lower()}')!==-1)return'verified';"
                f"return'not verified';}})()"
            )
            verified = self.cdp.eval(verify_js)

            if "verified" in str(verified):
                return SelectOutcome("SELECTED", selected_text=matched_text, attempts=attempts)
            return SelectOutcome("NOT_VERIFIED", attempts=attempts,
                                 evidence={"verify": str(verified)})

    def _try_native(self, anchor: dict, intent: SelectIntent) -> SelectOutcome | None:
        """If anchor is a native <select>, use cdp.form directly. Returns None if not applicable."""
        esc = anchor["marker"].replace("'", "\\'")
        info = self.cdp.eval(
            f"(function(){{var e=document.querySelector('{esc}');"
            f"return e?e.tagName:'';}})()")
        if "SELECT" not in str(info).upper():
            return None
        self.cdp.form(anchor["marker"], select=intent.option)
        # Verify
        val = self.cdp.eval(
            f"(function(){{var e=document.querySelector('{esc}');"
            f"return e?e.value:'';}})()")
        opts = self.cdp.eval(
            f"(function(){{var e=document.querySelector('{esc}');"
            f"if(!e||!e.selectedOptions||!e.selectedOptions.length)return'';"
            f"return e.selectedOptions[0].textContent.trim();}})()")
        if intent.option and (intent.option.lower() in str(opts).lower() or intent.option.lower() in str(val).lower()):
            return SelectOutcome("SELECTED", selected_text=str(opts).strip(),
                                 evidence={"native_select_value": str(val).strip()})
        return None
```

- [ ] **Step 2: Manual smoke test on ant-design**

```bash
cd /company/lanuage
python3 -c "
import os,sys,time
os.environ['WS_URL']='ws://localhost:9222/devtools/browser/...'
sys.path.insert(0,'src')
from common import CDPHelper
from select_explorer import SelectExplorer, SelectIntent, CandidateRef, SelectOutcome

cdp = CDPHelper(os.environ['WS_URL'])
cdp.navigate('http://localhost:8080/ant-design')
time.sleep(1)

explorer = SelectExplorer(cdp)
outcome = explorer.execute(
    SelectIntent(label='Country', mode='exact', option='United States'),
    [CandidateRef(selector='[id*=\"Country\" i]', frame_id='', source='id', confidence=0.85)]
)
print(f'Status: {outcome.status}')
print(f'OK: {outcome.ok}')
"
```
Expected: `Status: SELECTED`, `OK: True`

- [ ] **Step 3: Commit**

```bash
git add src/select_explorer.py
git commit -m "feat: add SelectExplorer.execute() — before/after visibility delta loop"
```

---

### Task 5: Route form-select to Explorer in json_executor

**Files:**
- Modify: `src/json_executor.py:770-820`

**Interfaces:**
- Consumes: `SelectExplorer.execute()`, `_classify_select_intent()`, `_normalize_candidates()`
- Modifies: form action handler in `_execute_step` to route DROPDOWN intents to Explorer

- [ ] **Step 1: Add _classify_select_intent and _normalize_candidates**

```python
# In JSONExecutor class, before _execute_step:

def _classify_select_intent(self, step: dict, candidates) -> str:
    """Classify form+select intent: DROPDOWN | CHOICE_GROUP | UNKNOWN."""
    if step.get("action") != "form" or "select" not in step:
        return "NOT_APPLICABLE"
    # DOM evidence first
    probe = self._probe_candidates(candidates)
    if probe.get("has_native_select") or probe.get("has_aria_combobox"):
        return "DROPDOWN"
    if probe.get("has_visible_choice_group"):
        return "CHOICE_GROUP"
    if probe.get("has_single_trigger"):
        return "DROPDOWN"
    return "UNKNOWN"

def _probe_candidates(self, candidates) -> dict:
    """Non-interactive probe of candidates to gather evidence."""
    # Simplified probe: check if any candidate's area has visible radio/checkbox
    if not hasattr(candidates[0], 'selector') if candidates else True:
        return {}
    esc = candidates[0].selector.replace("'", "\\'")
    js = (
        f"(function(){{var a=document.querySelector('{esc}');if(!a)return'{{}}';"
        f"var area=a.closest('[class*=form-item],fieldset,div');"
        f"if(!area)area=a.parentElement;"
        f"var radios=area.querySelectorAll('input[type=radio],input[type=checkbox]');"
        f"var vis=0;for(var i=0;i<radios.length;i++){{if(radios[i].offsetWidth>0)vis++;}}"
        f"var sel=area.querySelector('select');"
        f"var cb=area.querySelector('[role=combobox]');"
        f"return JSON.stringify({{has_native_select:!!sel,has_aria_combobox:!!cb,"
        f"has_visible_choice_group:vis>=2,"
        f"has_single_trigger:!vis||vis<2}});}})()"
    )
    raw = self.cdp.eval(js)
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except:
        return {}

def _normalize_candidates(self, loc_result) -> list:
    """Convert LocatorResult to list of CandidateRef."""
    from select_explorer import CandidateRef
    refs = [CandidateRef(
        selector=loc_result.selector,
        frame_id=loc_result.frame_id,
        source=loc_result.strategy,
        confidence=loc_result.confidence
    )]
    for alt in (loc_result.alternatives or []):
        refs.append(CandidateRef(
            selector=alt.selector,
            frame_id=alt.frame_id,
            source=alt.strategy,
            confidence=alt.confidence
        ))
    return refs
```

- [ ] **Step 2: Modify form handler routing (line ~813)**

Replace lines 813-820:
```python
# OLD:
select = step.get("select")
if select == "__random__" and not value and not check:
    if self._try_quiz_group(selector, self._frame_id):
        return True
ok = self._smart_form(selector, value=value, check=check, select=select, ...)

# NEW:
select = step.get("select")
if select and not value and not check:
    # Build intent
    from select_explorer import SelectExplorer, SelectIntent
    if not hasattr(self, '_select_explorer'):
        self._select_explorer = SelectExplorer(self.cdp, self.log)

    classification = self._classify_select_intent(step, [loc]) if 'loc' in dir() else "UNKNOWN"

    if classification == "DROPDOWN":
        candidates = self._normalize_candidates(loc)
        intent = SelectIntent(
            label=field.get("label", ""),
            mode="random" if select == "__random__" else "exact",
            option=None if select == "__random__" else select,
        )
        outcome = self._select_explorer.execute(intent, candidates)
        self.log.info(f"[JSON] explorer: {outcome.status}")
        if outcome.ok:
            return True
        if outcome.status in ("NOT_VERIFIED", "OPTION_NOT_FOUND", "OPEN_FAILED"):
            return False  # fail closed — no fallback to old path

    # Quiz group routing (existing)
    if select == "__random__" and not value and not check:
        if self._try_quiz_group(selector, self._frame_id):
            return True

ok = self._smart_form(selector, value=value, check=check, select=select, ...)
```

- [ ] **Step 3: Verify manual test still passes**

Run the manual test from Task 4 through the web_editor endpoint:
```bash
curl -s -X POST http://localhost:5000/api/run_json -H 'Content-Type: application/json' -d '{"site":"localhost:8080/ant-design","steps":[{"action":"form","field":{"label":"Country","type":"select"},"select":"United States"}]}' | python3 -c "import sys,json; print(json.load(sys.stdin).get('passed'))"
```
Expected: `True`

- [ ] **Step 4: Commit**

```bash
git add src/json_executor.py src/select_explorer.py
git commit -m "feat: route form-select DROPDOWN intents to SelectExplorer"
```

---

### Task 6: Integration tests — ant-design, react-select, mui-select

**Files:**
- Create: `tests/test_select_explorer.py`

**Interfaces:**
- Consumes: `SelectExplorer` from Task 4, routing from Task 5
- Tests: fixed JSON on 3 mock pages, 10/10 each

- [ ] **Step 1: Write test cases**

```python
# tests/test_select_explorer.py
"""Integration tests for Select Explorer Phase 1 MV."""
import os, sys, time, json
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from common import CDPHelper
from json_executor import JSONExecutor


def get_cdp():
    ws = os.environ.get('WS_URL', 'ws://localhost:9222/devtools/browser/...')
    return CDPHelper(ws)


TEST_CASES = [
    # (page, label, option, expected_status)
    ("http://localhost:8080/ant-design", "Country", "United States", True),
    ("http://localhost:8080/ant-design", "Country", "Canada", True),
    ("http://localhost:8080/react-select", "Country", "Canada", True),
    ("http://localhost:8080/react-select", "Country", "United States", True),
    ("http://localhost:8080/mui-select", "State", "California", True),
    ("http://localhost:8080/mui-select", "State", "New York", True),
    # Negative
    ("http://localhost:8080/ant-design", "Country", "Not Existing", False),
    ("http://localhost:8080/react-select", "Country", "Mars", False),
    ("http://localhost:8080/mui-select", "State", "Moon", False),
]


@pytest.mark.parametrize("page,label,option,expected", TEST_CASES)
def test_select_explorer(page, label, option, expected):
    """Each case runs 10 times — all must pass."""
    cdp = get_cdp()
    for run in range(10):
        cdp.navigate(page)
        time.sleep(1.5)
        config = {
            "site": page.replace("http://localhost:8080/", ""),
            "steps": [{
                "action": "form",
                "field": {"label": label, "type": "select"},
                "select": option,
            }]
        }
        executor = JSONExecutor(config, {"task_id": "test"}, cdp)
        ok = executor.run()
        if expected:
            assert ok, f"Run {run+1}/10: expected SELECTED, got False for {label}→{option} on {page}"
        else:
            assert not ok, f"Run {run+1}/10: expected failure, got True for {label}→{option} on {page}"
```

- [ ] **Step 2: Run tests**

```bash
cd /company/lanuage
WS_URL=ws://localhost:9222/devtools/browser/... pytest tests/test_select_explorer.py -v
```
Expected: 90 tests (9 cases × 10 runs), all PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_select_explorer.py
git commit -m "test: add Select Explorer integration tests — 3 pages × 10 runs each"
```

---

### Task 7: Add behavior-select-lab mock page

**Files:**
- Create: `mock-server/templates/behavior-select-lab/index.html`
- Modify: `mock-server/app.py` (add route)

- [ ] **Step 1: Create generic mock page with randomized class/id**

```html
<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Behavior Select Lab</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font:14px/1.6 system-ui,sans-serif;background:#f5f5f5;display:flex;justify-content:center;align-items:center;min-height:100vh}
.card{background:#fff;border-radius:12px;padding:32px;max-width:480px;width:100%}
/* Random class names — no ant-, mui-, react- prefixes */
._f7x2_{display:flex;flex-direction:column;margin-bottom:16px}
._f7x2_ label{font-size:14px;font-weight:500;margin-bottom:4px}
._a3b9_{border:1px solid #ccc;border-radius:6px;padding:10px 14px;cursor:pointer;display:flex;align-items:center;justify-content:space-between}
._a3b9_:hover{border-color:#2563eb}
._c4d1_{display:none;border:1px solid #2563eb;border-radius:6px;margin-top:2px;background:#fff;position:absolute;z-index:10;width:100%}
._c4d1_._open_{display:block}
._e5f2_{padding:10px 14px;cursor:pointer}
._e5f2_:hover{background:#eff6ff}
</style></head>
<body>
<div class="card">
  <h2>Select Your Preference</h2>
  <div class="_f7x2_">
    <label>Country</label>
    <div class="_a3b9_" onclick="toggleDropdown()" tabindex="0">
      <span id="display">Select country</span>
      <span>▼</span>
    </div>
    <div class="_c4d1_" id="dropdown">
      <div class="_e5f2_" onclick="selectOption('United States')">United States</div>
      <div class="_e5f2_" onclick="selectOption('Canada')">Canada</div>
      <div class="_e5f2_" onclick="selectOption('United Kingdom')">United Kingdom</div>
    </div>
  </div>
  <button onclick="handleSubmit()">Submit</button>
  <div id="result"></div>
</div>
<script>
function toggleDropdown(){document.getElementById('dropdown').classList.toggle('_open_')}
function selectOption(v){document.getElementById('display').textContent=v;document.getElementById('dropdown').classList.remove('_open_')}
function handleSubmit(){document.getElementById('result').textContent='Selected: '+document.getElementById('display').textContent}
</script>
</body>
</html>
```

- [ ] **Step 2: Add route to mock-server**

```python
# In mock-server/app.py, add:
@app.route('/behavior-select-lab')
def behavior_select_lab():
    return render_template('behavior-select-lab/index.html')
```

- [ ] **Step 3: Commit**

```bash
git add mock-server/templates/behavior-select-lab/ mock-server/app.py
git commit -m "feat: add behavior-select-lab mock — generic dropdown, random class names"
```

---

### Task 8: Regression guard — verify existing pages still pass

**Files:**
- Modify: `run_mock_tests.py` (add explorer test cases)

- [ ] **Step 1: Add phase 1 test cases to run_mock_tests.py**

```python
# Add to EXISTING CASES list in run_mock_tests.py:
("ant-design-select", "http://localhost:8080/ant-design",
    {"form_type": "form", "success_condition": {"body_contains": ["success"]},
     "steps": [
         {"action": "form", "field": {"label": "Country", "type": "select"}, "select": "United States"},
         {"action": "click", "find": {"text": "Submit"}},
     ]}),
("react-select-country", "http://localhost:8080/react-select",
    {"form_type": "form", "success_condition": {"body_contains": ["Country: Canada"]},
     "steps": [
         {"action": "form", "field": {"label": "Country", "type": "select"}, "select": "Canada"},
     ]}),
```

- [ ] **Step 2: Run existing test suite to verify no regressions**

```bash
cd /company/lanuage
WS_URL=... python3 run_mock_tests.py
```
Expected: all existing tests continue to pass, new explorer tests pass

- [ ] **Step 3: Commit**

```bash
git add run_mock_tests.py
git commit -m "test: add Phase 1 explorer cases to regression suite"
```

---

## Completion Checklist

- [ ] ant-design Country 10/10
- [ ] react-select Country 10/10
- [ ] mui-select State 10/10
- [ ] Negative tests: NOT_FOUND returns False
- [ ] Explorer code grep: zero `ant-`, `Mui`, `react-select`, `css-select__` in core logic
- [ ] No regression on existing mock pages
- [ ] ProbeSession cleanup verified
