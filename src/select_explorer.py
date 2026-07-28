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


class ProbeSession:
    """Manages unique marker injection and cleanup for one exploration attempt."""

    def __init__(self, cdp, marker_prefix: str = "probe"):
        self.cdp = cdp
        self.prefix = marker_prefix
        self._counter = 0
        self._markers: list[str] = []

    def mark_element(self, selector: str, frame_id: str = "") -> str:
        """Inject unique marker onto element. Returns marker CSS selector."""
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


def snapshot_visible_text_nodes(cdp, frame_id: str = "") -> set:
    """Return set of trimmed textContent for all visible leaf nodes."""
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


def find_single_safe_trigger(marker_sel: str, cdp, frame_id: str = "") -> str | None:
    """Search near anchor for most likely dropdown trigger. Returns marker selector or None."""
    esc = marker_sel.replace("'", "\\'")
    js = (
        f"(function(){{var a=document.querySelector('{esc}');if(!a)return'none';"
        f"var area=a.closest('[class*=form-item],fieldset,div');"
        f"if(!area)area=a.parentElement;"
        # Also check area itself + parent for trigger (anchor may be hidden input inside container)
        f"var searchRoot=area.parentElement||area;"
        f"var triggers=searchRoot.querySelectorAll('select,[role=combobox],[aria-haspopup=listbox],"
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
        f"var txt=t.textContent.trim().toLowerCase();"
        f"if(/submit|next|continue|search|go|sign/.test(txt))score-=5;"
        f"if(score>bestScore&&score>0){{best=t;bestScore=score;}}}}"
        f"if(best){{best.setAttribute('data-probe','trigger');return'trigger';}}"
        f"return'none';}})()"
    )
    raw = cdp.eval(js, frame_id)
    if "trigger" in str(raw):
        return "[data-probe=\"trigger\"]"
    return None


class SelectExplorer:
    """Discovers and selects dropdown options by observing DOM visibility changes."""

    def __init__(self, cdp, log=None):
        self.cdp = cdp
        self.log = log or logging.getLogger(__name__)

    def execute(self, intent: SelectIntent,
                candidates: list[CandidateRef]) -> SelectOutcome:
        """MV execution: snapshot before → click trigger → snapshot after →
        pick from became_visible → verify."""
        attempts = []

        with ProbeSession(self.cdp, "px") as sess:
            # 1. Mark the primary candidate
            if not candidates:
                return SelectOutcome("NO_CANDIDATE", attempts=attempts)
            marker = sess.mark_element(candidates[0].selector, candidates[0].frame_id)
            ref = {"marker": marker, "source": candidates[0].source}

            # 2. Try native select
            native = self._try_native(marker, intent)
            if native:
                return native

            # 3. Find trigger near anchor
            trigger_marker = find_single_safe_trigger(marker, self.cdp)
            if not trigger_marker:
                return SelectOutcome("NO_SAFE_TRIGGER", attempts=attempts)

            # 4. Snapshot BEFORE
            before = snapshot_visible_text_nodes(self.cdp)

            # 5. Click trigger
            self.cdp.click(trigger_marker)
            time.sleep(0.6)

            # 6. Snapshot AFTER
            after = snapshot_visible_text_nodes(self.cdp)
            became_visible = after - before

            if not became_visible:
                attempts.append({"trigger": trigger_marker, "delta": "empty"})
                return SelectOutcome("OPEN_FAILED",
                                     evidence={"before_count": len(before), "after_count": len(after)},
                                     attempts=attempts)

            # 7. Find option
            if intent.mode == "random":
                opts = sorted(became_visible)
                if not opts:
                    return SelectOutcome("OPTION_NOT_FOUND", attempts=attempts)
                target_text = opts[0]  # Phase 1: pick first for random
            else:
                target = intent.option or ""
                matches = [t for t in became_visible if t.strip().lower() == target.strip().lower()]
                if not matches:
                    return SelectOutcome("OPTION_NOT_FOUND",
                                         evidence={"visible_options": sorted(became_visible)[:20]},
                                         attempts=attempts)
                if len(matches) > 1:
                    return SelectOutcome("AMBIGUOUS",
                                         evidence={"duplicates": matches},
                                         attempts=attempts)
                target_text = matches[0]

            # 8. Click option by text
            escaped = target_text.replace("'", "\\'")
            opt_js = (
                f"(function(){{var els=document.querySelectorAll('div,span,li,button,[role=option],option');"
                f"for(var i=0;i<els.length;i++){{"
                f"if(els[i].textContent.trim()==='{escaped}'&&els[i].offsetWidth>0){{"
                f"els[i].setAttribute('data-probe','selected');els[i].click();return'clicked';}}}}"
                f"return'not found';}})()"
            )
            opt_result = self.cdp.eval(opt_js)
            if "not found" in str(opt_result):
                return SelectOutcome("OPTION_NOT_FOUND", attempts=attempts,
                                     evidence={"click_result": str(opt_result)})
            time.sleep(0.3)

            # 9. Verify
            trigger_esc = trigger_marker.replace("'", "\\'")
            target_lower = target_text.lower()
            verify_js = (
                f"(function(){{var t=document.querySelector('{trigger_esc}');"
                f"if(!t)return'not verified';"
                f"var txt=t.textContent.trim().toLowerCase();"
                f"if(txt.indexOf('{target_lower}')!==-1)return'verified';"
                f"var area=t.closest('[class*=form-item],fieldset,div');"
                f"if(!area)return'not verified';"
                f"var hidden=area.querySelector('input[type=hidden]');"
                f"if(hidden&&hidden.value.toLowerCase().indexOf('{target_lower}')!==-1)return'verified';"
                f"return'not verified';}})()"
            )
            verified = self.cdp.eval(verify_js)

            if "verified" in str(verified):
                return SelectOutcome("SELECTED", selected_text=target_text, attempts=attempts)
            return SelectOutcome("NOT_VERIFIED", attempts=attempts,
                                 evidence={"verify": str(verified)})

    def _try_native(self, marker: str, intent: SelectIntent) -> SelectOutcome | None:
        """If anchor is a native <select>, use cdp.form. Returns None if not native."""
        esc = marker.replace("'", "\\'")
        info = self.cdp.eval(
            f"(function(){{var e=document.querySelector('{esc}');return e?e.tagName:'';}})()")
        if "SELECT" not in str(info).upper():
            return None
        self.cdp.form(marker, select=intent.option)
        time.sleep(0.2)
        val = self.cdp.eval(
            f"(function(){{var e=document.querySelector('{esc}');"
            f"if(!e||!e.selectedOptions||!e.selectedOptions.length)return'';"
            f"return e.selectedOptions[0].textContent.trim();}})()")
        if intent.option and intent.option.lower() in str(val).lower():
            return SelectOutcome("SELECTED", selected_text=str(val).strip(),
                                 evidence={"native_select": True})
        return SelectOutcome("NOT_VERIFIED", evidence={"native_value": str(val)})
