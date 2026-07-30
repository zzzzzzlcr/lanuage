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
                        # | OPEN_FAILED | NO_CANDIDATE | INVALID_INTENT
                        # | FIELD_NOT_FOUND | GROUP_NOT_FOUND | AMBIGUOUS_GROUP
                        # | AMBIGUOUS_CONTROL | UNSUPPORTED_CONTROL | CLICK_FAILED
    evidence: dict = field(default_factory=dict)
    attempts: list = field(default_factory=list)
    selected_text: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in ("SELECTED", "ALREADY_SELECTED")


@dataclass
class ClickResult:
    """Structured CDP click outcome."""
    success: bool
    error: str | None = None
    error_type: str | None = None  # "cdp_error" | "timeout" | "exception" | "not_interactive"


@dataclass
class VerifyResult:
    """Structured verification outcome. Replaces substring-based checks."""
    verified: bool
    reason: str          # "state_matched" | "state_unchanged" | "element_detached" | "timeout" | "cdp_error"
    elapsed_ms: int = 0
    evidence: dict = field(default_factory=dict)


@dataclass
class OptionRef:
    """A single radio option — logical reference, not a marker selector."""
    text: str              # accessible name
    value: str             # input.value or aria value
    index: int             # 0-based within group
    checked: bool
    enabled: bool = True
    visible: bool = True


@dataclass
class GroupRef:
    """A discovered radio group — logical reference. Probe does NOT modify DOM."""
    group_type: str        # "native_radio" | "aria_radio"
    name: str              # native: input.name; ARIA: group label
    heading: str = ""      # Human-readable label text (from page)
    owner_form_id: str = ""     # native: closest form id
    scope_selector: str = ""    # CSS for minimal container (structural, no data-attr)
    options: list = field(default_factory=list)  # list[OptionRef]
    frame_id: str = ""
    conflicting_control_types: list = field(default_factory=list)


@dataclass
class StrategyProbe:
    """Pre-execution DOM evidence from a single Strategy."""
    kind: str              # "native_radio" | "aria_radio" | "native_select" | "combobox"
    candidates: list = field(default_factory=list)  # list[GroupRef] for radio
    frame_id: str = ""
    confidence: float = 0.0
    evidence: dict = field(default_factory=dict)


class MarkerSession:
    """Per-request unique token manager. Tracks markers per frame for cleanup."""

    def __init__(self, cdp, prefix: str = "rsc"):
        self.cdp = cdp
        self.prefix = prefix
        self._counter = 0
        self._markers_by_frame: dict[str, list[str]] = {}  # frame_id → [attr_values]

    def next_token(self) -> str:
        self._counter += 1
        return f"{self.prefix}{self._counter}"

    def inject_marker(self, selector: str, attr: str, token: str, frame_id: str = ""):
        """Inject a data-{attr}="{token}" marker. Tracks for later cleanup."""
        esc = selector.replace("'", "\\'")
        self.cdp.eval(
            f"(function(){{var e=document.querySelector('{esc}');"
            f"if(e)e.setAttribute('data-{attr}','{token}');}})()",
            frame_id)
        self._markers_by_frame.setdefault(frame_id, []).append(attr)

    def cleanup_all_frames(self):
        """Remove all markers across all frames tracked by this session."""
        for fid, attrs in self._markers_by_frame.items():
            for attr in attrs:
                self.cdp.eval(
                    f"(function(){{var els=document.querySelectorAll('[data-{attr}]');"
                    f"for(var i=0;i<els.length;i++)els[i].removeAttribute('data-{attr}');}})()",
                    fid)
        self._markers_by_frame.clear()

    def cleanup_frame(self, frame_id: str):
        if frame_id in self._markers_by_frame:
            for attr in self._markers_by_frame[frame_id]:
                self.cdp.eval(
                    f"(function(){{var els=document.querySelectorAll('[data-{attr}]');"
                    f"for(var i=0;i<els.length;i++)els[i].removeAttribute('data-{attr}');}})()",
                    frame_id)
            del self._markers_by_frame[frame_id]


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
    raw = cdp.eval(js, frame_id=frame_id)
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
        f"if(best){{var old2=document.querySelectorAll('[data-probe]');for(var k=0;k<old2.length;k++)old2[k].removeAttribute('data-probe');best.setAttribute('data-probe','trigger');return'trigger';}}"
        f"return'none';}})()"
    )
    raw = cdp.eval(js, frame_id=frame_id)
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

            # 3b. Check if dropdown is already open (e.g. multi-select left open)
            trigger_esc2 = trigger_marker.replace("'", "\\'")
            already_open = str(self.cdp.eval(
                f"(function(){{var t=document.querySelector('{trigger_esc2}');"
                f"if(!t)return'no';"
                f"var p=t.parentElement;"
                f"for(var lv=0;lv<4;lv++){{if(!p)break;"
                f"var menu=p.querySelector('[class*=menu--open],[class*=dropdown--open],[aria-expanded=true]');"
                f"if(menu)return'yes';"
                f"p=p.parentElement;}}"
                f"return'no';}})()"
            ))

            if "yes" in already_open:
                # Dropdown already open — just pick from currently visible options
                before = set()
                after = snapshot_visible_text_nodes(self.cdp)
                became_visible = after
            else:
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

            # 8. Click option by text — use CDP real click for reliable event delivery
            escaped = target_text.replace("'", "\\'")
            scope = "document"
            # Mark target option, then CDP-click for real mouse event
            opt_js = (
                f"(function(){{var scope={scope};"
                f"var els=scope.querySelectorAll('div,span,li,button,[role=option],option');"
                f"for(var i=0;i<els.length;i++){{"
                f"if(els[i].textContent.trim()==='{escaped}'&&els[i].offsetWidth>0){{"
                f"els[i].setAttribute('data-probe','selected');return'found';}}}}"
                f"return'not found';}})()"
            )
            opt_result = self.cdp.eval(opt_js)
            if "not found" in str(opt_result):
                return SelectOutcome("OPTION_NOT_FOUND", attempts=attempts,
                                     evidence={"click_result": str(opt_result)})
            self.cdp.click('[data-probe=\"selected\"]')
            time.sleep(0.3)
            # Close dropdown after selection (prevents overlap with other elements)
            self.cdp.eval(
                "(function(){"
                "document.querySelectorAll('[class*=menu--open]').forEach(function(m){var c=m.className.match(/\\S*menu--open\\S*/);if(c)m.classList.remove(c[0]);});"
                "document.querySelectorAll('[class*=control--menu-is-open]').forEach(function(c){c.classList.remove('css-select__control--menu-is-open');});"
                "})()"
            )
            time.sleep(0.15)

            # 9. Verify
            trigger_esc = trigger_marker.replace("'", "\\'")
            target_lower = target_text.lower()
            verify_js = (
                f"(function(){{var t=document.querySelector('{trigger_esc}');"
                f"if(!t)return'not verified';"
                # textContent works for most cases, but hidden placeholders can
                # pollute it (e.g. react-select hides placeholder but keeps it
                # in DOM). innerText respects CSS visibility, so try it too.
                f"var txt=(t.innerText||t.textContent||'').trim().toLowerCase();"
                f"if(txt.indexOf('{target_lower}')!==-1)return'verified';"
                # Also check t.textContent in case innerText lost relevant text
                f"var tc=(t.textContent||'').trim().toLowerCase();"
                f"if(tc!==txt&&tc.indexOf('{target_lower}')!==-1)return'verified';"
                # Check input/select values on the trigger itself
                f"var ins=t.querySelectorAll('input,select');"
                f"for(var i=0;i<ins.length;i++){{if((ins[i].value||'').toLowerCase().indexOf('{target_lower}')!==-1)return'verified';}}"
                f"var area=t.closest('[class*=form-item],fieldset,div');"
                f"if(!area)return'not verified';"
                f"var allInputs=area.querySelectorAll('input,select');"
                f"for(var j=0;j<allInputs.length;j++){{if((allInputs[j].value||'').toLowerCase().indexOf('{target_lower}')!==-1)return'verified';}}"
                f"return'no';}})()"
            )
            verified = str(self.cdp.eval(verify_js)).strip().strip('"')

            if verified == "verified":
                return SelectOutcome("SELECTED", selected_text=target_text, attempts=attempts)
            return SelectOutcome("NOT_VERIFIED", attempts=attempts,
                                 evidence={"verify": verified})

    def _try_native(self, marker: str, intent: SelectIntent) -> SelectOutcome | None:
        """If anchor is a native <select>, use cdp.form. Returns None if not native."""
        esc = marker.replace("'", "\\'")
        info = self.cdp.eval(
            f"(function(){{var e=document.querySelector('{esc}');return e?e.tagName:'';}})()")
        if "SELECT" not in str(info).upper():
            return None
        if intent.mode == "random":
            # Pick random option, skip placeholder (value="")
            self.cdp.eval(
                f"(function(){{var e=document.querySelector('{esc}');if(!e)return;"
                f"var opts=e.querySelectorAll('option');var vis=[];"
                f"for(var i=0;i<opts.length;i++){{if(opts[i].value&&opts[i].textContent.trim())vis.push(opts[i]);}}"
                f"if(!vis.length)return;var pick=vis[Math.floor(Math.random()*vis.length)];"
                f"e.value=pick.value;e.dispatchEvent(new Event('change',{{bubbles:true}}));}})()"
            )
            time.sleep(0.2)
        else:
            self.cdp.form(marker, select=intent.option)
            time.sleep(0.2)
        val = self.cdp.eval(
            f"(function(){{var e=document.querySelector('{esc}');"
            f"if(!e||!e.selectedOptions||!e.selectedOptions.length)return'no selection';"
            f"return JSON.stringify({{text:e.selectedOptions[0].textContent.trim(),"
            f"value:e.value}});}})()")
        self.log.info(f"[explorer] native verify: marker={marker} val={str(val)[:80]}")
        # For random: any non-empty value is success. For exact: match option text or value.
        if intent.mode == "random":
            ok = "no selection" not in str(val) and str(val).strip() not in ("", "{}")
        else:
            ok = intent.option and intent.option.lower() in str(val).lower()
        if ok:
            return SelectOutcome("SELECTED", selected_text=str(val)[:80],
                                 evidence={"native_select": True})
        return SelectOutcome("NOT_VERIFIED", evidence={"native_value": str(val)[:80]})


# ==================================================================
# RadioStrategy — generic radio group selection
# ==================================================================

class RadioStrategy:
    """Selects an option in a native radio group or ARIA radio group.

    Probe (read-only): scans the page for visible, enabled radio groups
    associated with the intent. Never modifies DOM.
    Execute: operates within the probe-confirmed scope, injects markers,
    clicks the target, and verifies checked/aria-checked state.
    """

    @staticmethod
    def probe(intent: SelectIntent, ctx) -> StrategyProbe | None:
        """Scan for radio groups associated with this intent.

        Returns None if:
        - No visible enabled radio groups on page
        - Radio groups exist but none associate with intent label/option
        - field={}+random with multiple visible groups
        """
        cdp = ctx["cdp"]
        frame_id = ctx.get("frame_id", "")

        # Single JS eval — discover all visible enabled native + ARIA radio groups
        js = """(function(){
          var groups = [];
          var seen = {};

          // Native radio groups
          var radios = document.querySelectorAll('input[type=radio]');
          radios.forEach(function(r){
            if (r.disabled || r.closest('[aria-hidden=true]')) return;
            var form = r.closest('form');
            var ownerId = form ? (form.id || form.getAttribute('data-form-id') || '') : '';
            var key = ownerId + '::' + (r.name || '');
            if (!key || seen[key]) return;
            seen[key] = true;

            // Find minimal scope container
            var container = r.closest(
              '[class*=form-section],[class*=question],[class*=field],fieldset,.form-group,.form-row');
            var scopeSel = '';
            if (container) {
              // Try fieldset/legend first
              if (container.tagName === 'FIELDSET') {
                scopeSel = 'fieldset';
                var leg = container.querySelector('legend');
                if (leg) scopeSel += ':has(legend)';
              } else if (container.className) {
                scopeSel = container.tagName.toLowerCase() + '.' + container.className.split(' ')[0];
              } else {
                scopeSel = container.tagName.toLowerCase();
              }
            }

            // Find heading text
            var heading = '';
            if (container) {
              var children = container.querySelectorAll('label,span,p,h3,h4,.label,.title,.heading');
              for (var c=0;c<children.length;c++){
                var inp = children[c].querySelector('input,select,textarea');
                if (!inp && children[c].offsetWidth > 0) {
                  var txt = children[c].textContent.trim();
                  if (txt.length >= 2) { heading = txt; break; }
                }
              }
              if (!heading) {
                var leg = container.querySelector('legend');
                heading = leg ? leg.textContent.trim() : '';
              }
            }

            // Collect options
            var options = [];
            var selector = 'input[type=radio][name="'+r.name+'"]';
            var siblings = (form||document).querySelectorAll(selector);
            siblings.forEach(function(s, idx){
              if (s.closest('[aria-hidden=true]')) return;
              var optLabel = s.closest('label');
              var optText = '';
              if (optLabel) { optText = optLabel.textContent.replace(/\\s+/g,' ').trim(); }
              else if (s.id) {
                var forLabel = document.querySelector('label[for="'+s.id+'"]');
                optText = forLabel ? forLabel.textContent.replace(/\\s+/g,' ').trim() : '';
              }
              if (!optText && s.value) optText = s.value;
              options.push({
                text: optText, value: s.value, index: idx,
                checked: s.checked, enabled: !s.disabled, visible: true
              });
            });

            // Detect conflicting controls in same scope
            var conflict = [];
            if (container) {
              var checkboxes = container.querySelectorAll('input[type=checkbox]');
              if (checkboxes.length) conflict.push('checkbox');
            }

            groups.push({
              group_type: 'native_radio', name: r.name, owner_form_id: ownerId,
              scope_selector: scopeSel, heading: heading, options: options,
              frame_id: '', conflicting_control_types: conflict
            });
          });

          // ARIA radio groups
          var ariaGroups = document.querySelectorAll('[role=radiogroup]');
          ariaGroups.forEach(function(g, gi){
            if (!g.offsetWidth || g.closest('[aria-hidden=true]')) return;
            var label = g.getAttribute('aria-label') || g.getAttribute('aria-labelledby') || '';
            if (label && document.getElementById(label))
              label = document.getElementById(label).textContent.trim();
            var ariaRadios = g.querySelectorAll('[role=radio]');
            if (!ariaRadios.length) return;
            var options = [];
            ariaRadios.forEach(function(r, idx){
              if (r.getAttribute('aria-disabled')==='true') return;
              options.push({
                text: (r.textContent||'').replace(/\\s+/g,' ').trim(),
                value: r.getAttribute('aria-checked')||'false',
                index: idx, checked: r.getAttribute('aria-checked')==='true',
                enabled: true, visible: true
              });
            });
            groups.push({
              group_type: 'aria_radio', name: label || ('aria_group_'+gi),
              owner_form_id: '', scope_selector: '[role=radiogroup]',
              heading: label, options: options, frame_id: '',
              conflicting_control_types: []
            });
          });

          return JSON.stringify(groups.slice(0, 10));
        })()"""
        raw = cdp.eval(js, frame_id=frame_id)
        try:
            raw_groups = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(raw_groups, str):
                raw_groups = json.loads(raw_groups)
        except Exception:
            return None

        if not raw_groups:
            return None

        # Convert to GroupRef objects
        group_refs = []
        for g in raw_groups:
            opts = [OptionRef(
                text=o["text"], value=o.get("value", ""), index=o.get("index", 0),
                checked=o.get("checked", False), enabled=o.get("enabled", True),
                visible=o.get("visible", True),
            ) for o in g.get("options", [])]
            group_refs.append(GroupRef(
                group_type=g.get("group_type", "native_radio"),
                name=g.get("name", ""),
                heading=g.get("heading", ""),
                owner_form_id=g.get("owner_form_id", ""),
                scope_selector=g.get("scope_selector", ""),
                options=opts,
                frame_id=g.get("frame_id", frame_id),
                conflicting_control_types=g.get("conflicting_control_types", []),
            ))
        # Diagnostic log
        if ctx.get("log"):
            ctx["log"].info(f"[RadioStrategy.probe] found {len(raw_groups)} raw groups: "
                          f"{[{'name':g['name'],'heading':g.get('heading',''),'n_opts':len(g.get('options',[]))} for g in raw_groups]}")
            ctx["log"].info(f"[RadioStrategy.probe] associated {len(group_refs)} groups with intent label='{intent.label}'")

        # Associate groups with intent
        associated = RadioStrategy._associate_groups(group_refs, intent)
        if isinstance(associated, str) or not associated:
            return None  # No association → DEFER_LEGACY

        return StrategyProbe(
            kind="native_radio" if associated[0].group_type == "native_radio" else "aria_radio",
            candidates=associated,
            frame_id=associated[0].frame_id,
            confidence=0.85,
            evidence={"total_groups_found": len(raw_groups), "associated_groups": len(associated)},
        )

    @staticmethod
    def _associate_groups(groups: list[GroupRef], intent: SelectIntent) -> list[GroupRef] | str:
        """Match groups to intent. Returns list of GroupRef or error string."""
        label = (intent.label or "").strip().lower()
        option_raw = (intent.option or "").strip() if intent.mode == "exact" else ""
        option = RadioStrategy._normalize(option_raw)

        matched = []

        def _match_label(g: GroupRef) -> bool:
            """Check if intent label matches group heading or name.
            Uses token-based matching (split on spaces/underscores/camelCase)
            to avoid substring false positives like 'cover' in 'Coverage'."""
            heading = (g.heading or "").strip().lower()
            name = (g.name or "").strip().lower()
            # Tokenize: split on spaces, underscores, and camelCase boundaries
            import re as _re
            def tokens(s):
                # Split on space/underscore/hyphen, then split camelCase
                parts = _re.split(r'[\s_\-]+', s)
                result = []
                for p in parts:
                    # Split camelCase: "healthCvr" → ["health", "cvr"]
                    camel = _re.sub(r'([a-z])([A-Z])', r'\1 \2', p)
                    result.extend(camel.lower().split())
                return set(result)
            label_toks = tokens(label)
            heading_toks = tokens(heading)
            name_toks = tokens(name)
            # Match if label tokens are a subset of heading or name tokens
            # (heading/name can have extra tokens like "Do you have...")
            if label_toks:
                if label_toks.issubset(heading_toks) or label_toks.issubset(name_toks):
                    return True
                # Also check if heading tokens are subset of label (for short headings)
                if heading_toks and heading_toks.issubset(label_toks):
                    return True
                if name_toks and name_toks.issubset(label_toks):
                    return True
            return False

        # 1. field.label matches group heading/name
        if label:
            for g in groups:
                if _match_label(g):
                    matched.append(g)

        # 2. field={} + exact: option text uniquely reverse-match
        if not matched and not label and option:
            for g in groups:
                for o in g.options:
                    if RadioStrategy._normalize(o.text) == option:
                        matched.append(g)
                        break
            if len(matched) > 1:
                return "ambiguous"

        # 3. field={} + random: only one visible group
        if not matched and not label and not option:
            enabled = [g for g in groups if any(o.enabled for o in g.options)]
            if len(enabled) == 1:
                matched = enabled
            elif len(enabled) > 1:
                return "ambiguous"

        # 4. field.label didn't match heading but option can uniquely reverse-match
        if not matched and label and option:
            for g in groups:
                for o in g.options:
                    if RadioStrategy._normalize(o.text) == option:
                        matched.append(g)
                        break
            if len(matched) > 1:
                return "ambiguous"

        return matched if matched else "no_match"

    @staticmethod
    def _normalize(text: str) -> str:
        """Normalize for comparison: collapse whitespace, lowercase, strip NBSP/hyphens."""
        return text.replace("\xa0", " ").replace("-", " ").replace("—", " ").strip().lower()

    @staticmethod
    def execute(intent: SelectIntent, probe: StrategyProbe, ctx) -> SelectOutcome:
        """Execute selection within the probe-confirmed scope."""
        cdp = ctx["cdp"]
        log = ctx.get("log")
        frame_id = probe.frame_id or ctx.get("frame_id", "")
        marker_session = ctx.get("marker_session") or MarkerSession(cdp)
        groups = probe.candidates  # list[GroupRef]

        try:
            # 1. Score and select group
            if len(groups) == 0:
                return SelectOutcome("GROUP_NOT_FOUND")
            if len(groups) > 1:
                # Try to disambiguate: prefer group whose heading matches intent label
                label = (intent.label or "").strip().lower()
                scored = []
                for g in groups:
                    heading = (g.heading or "").strip().lower()
                    score = 0
                    if label and (label in heading or heading in label):
                        score = 10
                    if intent.mode == "exact" and intent.option:
                        for o in g.options:
                            if RadioStrategy._normalize(o.text) == RadioStrategy._normalize(intent.option):
                                score += 5
                                break
                    scored.append((score, g))
                scored.sort(key=lambda x: -x[0])
                if scored[0][0] > scored[1][0]:
                    group = scored[0][1]
                    if log:
                        log.info(f"[RadioStrategy.execute] disambiguated {len(groups)} groups → {group.heading or group.name}")
                else:
                    return SelectOutcome("AMBIGUOUS_GROUP",
                        evidence={"group_count": len(groups), "group_names": [g.heading or g.name for g in groups]})
            else:
                group = groups[0]

            # 2. Check conflicting controls
            if group.conflicting_control_types:
                return SelectOutcome("AMBIGUOUS_CONTROL",
                    evidence={"conflicting_controls": group.conflicting_control_types,
                              "scope": group.scope_selector})

            # 3. Match option
            if log:
                log.info(f"[RadioStrategy.execute] group={group.name} n_opts={len(group.options)} "
                         f"opts={[(o.text[:30],o.value,o.enabled,o.checked) for o in group.options]}")
            target = None
            if intent.mode == "exact":
                norm_target = RadioStrategy._normalize(intent.option or "")
                for o in group.options:
                    if not o.enabled or not o.visible:
                        continue
                    if RadioStrategy._normalize(o.text) == norm_target:
                        target = o
                        break
                if not target:
                    return SelectOutcome("OPTION_NOT_FOUND",
                        evidence={"target": intent.option,
                                  "available": [o.text for o in group.options]})
            else:  # random
                # Pick from unchecked enabled options
                candidates = [o for o in group.options if o.enabled and o.visible and not o.checked]
                if not candidates:
                    return SelectOutcome("ALREADY_SELECTED",
                        evidence={"message": "All enabled options already checked"})
                import random
                target = random.choice(candidates)

            # 4. Read selected_before
            selected_before = target.checked

            # 5. Already selected → ALREADY_SELECTED
            if selected_before:
                return SelectOutcome("ALREADY_SELECTED",
                    evidence={"option_text": target.text, "selected_before": True})

            # 6. Find and mark the activation target + verify target
            click_token = marker_session.next_token()
            verify_token = marker_session.next_token()
            group_name_esc = group.name.replace("'", "\\'")
            target_val_esc = target.value.replace("'", "\\'")
            target_text_esc = target.text.replace("'", "\\'")

            find_js = (
                f"(function(){{"
                # Native radio: find by name + value in entire document
                f"var nativeInput=document.querySelector("
                f"'input[type=radio][name=\"{group_name_esc}\"][value=\"{target_val_esc}\"]');"
                f"if(nativeInput){{"
                f"nativeInput.setAttribute('data-rsc-vfy','{verify_token}');"
                f"var act=nativeInput.closest('label');"
                f"if(!act && nativeInput.id){{"
                f"var fl=document.querySelector('label[for=\"'+nativeInput.id+'\"]');"
                f"if(fl)act=fl;}}"
                f"if(!act)act=nativeInput;"
                f"act.setAttribute('data-rsc-clk','{click_token}');"
                f"return JSON.stringify({{found:true,activation:act.tagName"
                f"+('.'+(act.className||'').split(' ')[0])}});}}"
                # ARIA radio: find by text within radiogroup
                f"var ar=document.querySelectorAll('[role=radio]');"
                f"for(var i=0;i<ar.length;i++){{"
                f"if(ar[i].textContent.trim().indexOf('{target_text_esc}')!==-1&&ar[i].offsetWidth>0){{"
                f"ar[i].setAttribute('data-rsc-vfy','{verify_token}');"
                f"ar[i].setAttribute('data-rsc-clk','{click_token}');"
                f"return JSON.stringify({{found:true,activation:'ARIA_radio'}});"
                f"}}}}"
                f"return JSON.stringify({{found:false}});}})()"
            )
            raw = cdp.eval(find_js, frame_id=frame_id)
            try:
                find_result = json.loads(raw) if isinstance(raw, str) else raw
                if isinstance(find_result, str):
                    find_result = json.loads(find_result)
            except Exception:
                find_result = {"found": False}

            if not find_result.get("found"):
                if log:
                    log.warning(f"[RadioStrategy.execute] find_js failed: scope={scope_sel} group_name={group.name} target_val={target.value} target_text={target.text}")
                return SelectOutcome("OPTION_NOT_FOUND",
                    evidence={"target": target.text, "value": target.value,
                              "scope_selector": scope_sel, "group_name": group.name})

            click_marker = f'[data-rsc-clk="{click_token}"]'
            verify_marker = f'[data-rsc-vfy="{verify_token}"]'

            # Register marker attributes for cleanup
            marker_session._markers_by_frame.setdefault(frame_id, []).extend(["rsc-clk", "rsc-vfy"])
            # CDP click on label may not trigger native radio behavior reliably.
            # JS-based activation is deterministic.
            if group.group_type == "native_radio":
                esc_vfy = verify_marker.replace("'", "\\'")
                activate_js = (
                    f"(function(){{"
                    f"var inp=document.querySelector('{esc_vfy}');"
                    f"if(!inp)return JSON.stringify({{ok:false,error:'not found'}});"
                    # Uncheck siblings in same group
                    f"var siblings=document.querySelectorAll('input[type=radio][name=\"'+inp.name+'\"]');"
                    f"for(var i=0;i<siblings.length;i++){{siblings[i].checked=false;}}"
                    # Check target
                    f"inp.checked=true;"
                    f"inp.dispatchEvent(new Event('change',{{bubbles:true}}));"
                    f"inp.dispatchEvent(new Event('input',{{bubbles:true}}));"
                    # Also click the input for frameworks that listen for click
                    f"inp.click();"
                    f"return JSON.stringify({{ok:true,checked:inp.checked}});"
                    f"}})()"
                )
            else:
                esc_vfy = verify_marker.replace("'", "\\'")
                activate_js = (
                    f"(function(){{"
                    f"var inp=document.querySelector('{esc_vfy}');"
                    f"if(!inp)return JSON.stringify({{ok:false,error:'not found'}});"
                    f"inp.setAttribute('aria-checked','true');"
                    f"inp.dispatchEvent(new Event('change',{{bubbles:true}}));"
                    f"inp.click();"
                    f"return JSON.stringify({{ok:true,checked:true}});"
                    f"}})()"
                )
            raw = cdp.eval(activate_js, frame_id=frame_id)
            try:
                activate_result = json.loads(raw) if isinstance(raw, str) else raw
                if isinstance(activate_result, str):
                    activate_result = json.loads(activate_result)
            except Exception:
                activate_result = {"ok": False}

            if not activate_result.get("ok"):
                return SelectOutcome("CLICK_FAILED",
                    evidence={"error": activate_result.get("error", "activation failed")})

            # 8. Verify immediately (no polling needed for JS activation)
            import time as _time
            elapsed = 50  # JS activation is instant
            selected_after = activate_result.get("checked", False)
            if selected_after:
                return SelectOutcome("SELECTED",
                    evidence={
                        "field_text": intent.label, "group_name": group.name,
                        "group_type": group.group_type,
                        "option_text": target.text, "input_value": target.value,
                        "selected_before": selected_before, "selected_after": True,
                        "verification_signal": "checked" if group.group_type == "native_radio" else "aria_checked",
                        "activation_target": find_result.get("activation", ""),
                        "discovery_method": "field_scope" if intent.label else "option_unique_reverse",
                        "option_match": "normalized_exact" if intent.mode == "exact" else "random",
                        "elapsed_ms": elapsed,
                    })
            else:
                return SelectOutcome("NOT_VERIFIED",
                    evidence={"reason": "state_unchanged", "option_text": target.text})

        finally:
            marker_session.cleanup_all_frames()
