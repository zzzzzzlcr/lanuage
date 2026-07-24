"""Wizard Explorer: incremental page-by-page discovery for multi-step forms.

Replaces one-shot LLM generation for wizard pages.
Core loop: capture state → LLM picks one action → execute → verify transition → repeat.
"""
import json, time, hashlib, logging
from typing import Dict, List, Optional, Tuple


class WizardExplorer:
    """Explore multi-step wizard pages incrementally, building a state machine JSON."""

    MAX_ROUNDS = 30
    SUCCESS_POLL_SECONDS = 8
    SUCCESS_POLL_INTERVAL = 0.5
    STABLE_WAIT = 0.8

    def __init__(self, cdp, llm, log=None):
        self.cdp = cdp
        self.llm = llm
        self.log = log or logging.getLogger(__name__)
        self.visited_states = set()  # state fingerprints already seen
        self.trajectory = []         # confirmed (state, action, transition) tuples

    # ── Public API ────────────────────────────────────────────────

    def explore(self, url: str, success_desc: str, max_rounds: int = None) -> dict:
        """Explore a wizard page and return a state machine JSON config.

        Args:
            url: starting URL
            success_desc: natural language description of success (e.g. "页面出现 Thank You")
            max_rounds: override MAX_ROUNDS

        Returns:
            state machine JSON config that the direct executor can run
        """
        max_r = max_rounds or self.MAX_ROUNDS
        self.cdp.eval(f"(function(){{window.location.href='{url}';}})()")
        time.sleep(2)
        self.cdp.wait_page_stable(8)

        for rnd in range(max_r):
            state = self._capture_state()
            fp = state["fingerprint"]

            # Check if already succeeded
            if self._check_success(state, success_desc):
                self.log.info(f"[Explorer] Round {rnd}: success condition met!")
                return self._compile_config(state["url"], success_desc)

            # Check if stuck (same state without progress)
            if fp in self.visited_states:
                self.log.info(f"[Explorer] Round {rnd}: state already visited, trying alternatives")
            self.visited_states.add(fp)

            self.log.info(f"[Explorer] Round {rnd}: fp={fp[:12]}... heading={state.get('heading','?')}")

            # Ask LLM for one action
            action = self._decide_action(state, success_desc, rnd)
            if not action:
                self.log.warning(f"[Explorer] Round {rnd}: LLM returned no action, breaking")
                break

            # Execute the action
            ok, new_state = self._execute_and_verify(state, action)
            if ok:
                self.trajectory.append({
                    "state_fp": fp,
                    "action": action,
                    "new_fp": new_state["fingerprint"],
                })
                self.log.info(f"[Explorer] Round {rnd}: action ok, transition to {new_state['heading']}")
            else:
                self.log.warning(f"[Explorer] Round {rnd}: action failed or no transition")

        # Max rounds reached — compile what we have
        return self._compile_config(state.get("url", url), success_desc)

    # ── State Capture ─────────────────────────────────────────────

    def _capture_state(self) -> dict:
        """Capture a stable fingerprint of the current page state."""
        js = """(function(){
        var r = {url: window.location.href, title: document.title};
        // Main heading
        var h = document.querySelector('h1,h2,h3');
        r.heading = h ? h.textContent.trim().substring(0,80) : '';
        // Visible text fields
        var inputs = document.querySelectorAll('input:not([type=hidden]),select,textarea,[role=combobox]');
        r.fields = [];
        for (var i=0;i<inputs.length;i++) {
            var e=inputs[i];
            if (e.offsetWidth>0 || e.id) {
                r.fields.push({tag:e.tagName,type:e.type||'',id:e.id||'',name:e.name||'',
                    ph:e.placeholder||'',aria:e.getAttribute('aria-label')||'',
                    role:e.getAttribute('role')||'',ow:e.offsetWidth});
            }
        }
        // Visible buttons
        var btns = document.querySelectorAll('button,a,div[role=button],span[role=button]');
        r.buttons = [];
        for (var i=0;i<btns.length;i++) {
            var b=btns[i];
            if (b.offsetWidth>0 && b.textContent.trim()) {
                r.buttons.push({id:b.id||'',text:b.textContent.trim().substring(0,60),
                    tag:b.tagName,disabled:b.disabled||false});
            }
        }
        // Visible options (radio/checkbox labels, quiz choices)
        var labels = document.querySelectorAll('label');
        r.options = [];
        for (var i=0;i<labels.length;i++) {
            var l=labels[i];
            if (l.offsetWidth>0 && l.textContent.trim().length>1 && l.textContent.trim().length<80)
                r.options.push(l.textContent.trim().substring(0,60));
        }
        // Body text preview
        r.body = document.body ? document.body.innerText.substring(0,500) : '';
        return JSON.stringify(r);
        })()"""
        raw = self.cdp.eval(js)
        try:
            state = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            state = {"url": "", "title": "", "heading": "", "fields": [], "buttons": [], "options": [], "body": ""}
        state["fingerprint"] = self._fingerprint(state)
        return state

    def _fingerprint(self, state: dict) -> str:
        """Stable fingerprint: URL path + heading + fields + buttons + body preview."""
        key = (
            state.get("url", "").split("?")[0]
            + "|" + (state.get("heading") or "")
            + "|" + json.dumps([f.get("id","") or f.get("ph","") for f in state.get("fields", [])], sort_keys=True)
            + "|" + json.dumps([b.get("text","") for b in state.get("buttons", [])], sort_keys=True)
            + "|" + (state.get("body","")[:200])  # body changes when wizard steps advance
        )
        return hashlib.md5(key.encode()).hexdigest()[:16]

    # ── LLM Decision ──────────────────────────────────────────────

    def _decide_action(self, state: dict, success_desc: str, round_n: int) -> dict:
        """Pick ONE action based on current page state. Code rules first, LLM as fallback."""
        btns = [b for b in state.get("buttons",[]) if b["text"] not in ("MockServer","Privacy Policy","Terms of Service","Contact","©")]
        fields = [f for f in state.get("fields",[]) if f.get("ph") or f.get("id")]
        opts = state.get("options", [])
        body = state.get("body","") or ""
        btn_texts = [b["text"] for b in btns]
        action_btns = [t for t in btn_texts if t in ("Get Estimate","Record Request","Continue","Next","Submit","Get My Results","See the Full Breakdown","Get My Free Guide","UNLOCK HERE","Subscribe","Send Message")]

        # Rule 1: options visible + no trajectory → select random
        if opts and len(self.trajectory) == 0:
            return {"action": "select", "selection_strategy": {"type": "random"}}

        # Rule 2: visible form fields → fill them (one at a time)
        if fields:
            f = fields[0]
            label = f.get("ph") or f.get("id") or "name"
            fid = f.get("id", "")
            ftype = "text"
            if "email" in label.lower() or "email" in fid.lower(): ftype = "email"
            elif "zip" in label.lower() or "zip" in fid.lower() or "post" in label.lower(): ftype = "zip"
            elif "phone" in label.lower() or "phone" in fid.lower() or "tel" in fid.lower(): ftype = "tel"
            elif "password" in label.lower() or "pw" in fid.lower(): ftype = "password"
            elif "area" in label.lower(): ftype = "area"
            state["fields"] = fields[1:]
            return {"action": "form", "field": {"label": label, "id": fid, "type": ftype}}

        # Rule 3: action buttons → click the most relevant one
        if action_btns:
            return {"action": "click", "find": {"text": action_btns[0]}}

        # Rule 4: any visible button → click it
        if btns:
            return {"action": "click", "find": {"text": btns[0]["text"]}}

        # Rule 5: success check → done
        if "Estimate" in body or "Ready" in body or "Thank" in body or "Complete" in body:
            return {"action": "done"}

        # Rule 6: nothing else to do
        return {"action": "done"}

    # ── Execute & Verify ──────────────────────────────────────────

    def _execute_and_verify(self, old_state: dict, action: dict) -> Tuple[bool, dict]:
        """Execute one action and verify page changed."""
        atype = action.get("action", "")
        if atype == "done":
            return True, old_state

        try:
            if atype == "select":
                self._exec_select()
            elif atype == "click":
                find = action.get("find", {})
                text = find.get("text", "")
                self._exec_click(text)
            elif atype == "form":
                field = action.get("field", {})
                self._exec_form(field)
            elif atype == "wait":
                t = (action.get("min", 1) + action.get("max", 3)) / 2
                time.sleep(t)
                return True, old_state
            else:
                return False, old_state
        except Exception as e:
            self.log.warning(f"[Explorer] Execute failed: {e}")
            return False, old_state

        # Wait for page to stabilize
        time.sleep(self.STABLE_WAIT)
        self.cdp.wait_page_stable(5)

        # Capture new state
        new_state = self._capture_state()
        # Verify transition: data entry steps (select/form) don't require page change
        is_navigation = atype in ("click",)
        if is_navigation:
            changed = (new_state["fingerprint"] != old_state["fingerprint"]
                       or new_state.get("url") != old_state.get("url"))
        else:
            changed = True  # select/form always considered successful
        return changed, new_state

    def _exec_select(self):
        """Click a visible option: prefer radio labels, then quiz options."""
        # First try radio/checkbox labels (they toggle form controls)
        js = """(function(){
        var labels=document.querySelectorAll('label');
        var opts=[];
        for(var i=0;i<labels.length;i++){
        var l=labels[i];var t=l.textContent.trim();
        if(l.offsetWidth>0&&t.length>1&&t.length<60&&!l.querySelector('a')){
        opts.push(l);
        }}
        if(opts.length>0){opts[Math.floor(Math.random()*opts.length)].click();return'radio';}
        // Fallback: quiz option divs
        var divs=document.querySelectorAll('div');
        for(var i=0;i<divs.length;i++){
        var d=divs[i];var t=d.textContent.trim();
        if(!d.offsetWidth||t.length<2||t.length>70)continue;
        if(d.querySelector('div'))continue;
        if(d.closest('nav,header,footer'))continue;
        opts.push(d);
        }
        if(opts.length>0){opts[Math.floor(Math.random()*opts.length)].click();return'div';}
        return'none';
        })()"""
        self.cdp.eval(js)

    def _exec_click(self, text: str):
        """Click a button by visible text."""
        esc = text.replace("'", "\\'")
        self.cdp.eval(f"""(function(){{
        var bs=document.querySelectorAll('button,a');
        for(var i=0;i<bs.length;i++){{
        if(bs[i].textContent.trim().indexOf('{esc}')!==-1&&bs[i].offsetWidth>0){{
        bs[i].click();return;}}}}}})()""")

    def _exec_form(self, field: dict):
        """Fill a form field with appropriate test values."""
        from locator import FieldLocator, LocatorError
        loc = FieldLocator(self.cdp)
        try:
            result = loc.locate(field)
            label = (field.get("label") or "").lower()
            fid = (field.get("id") or "").lower()
            ftype = field.get("type", "text")
            if ftype == "email" or "email" in label or "email" in fid: value = "test@test.com"
            elif ftype == "zip" or "zip" in label or "zip" in fid: value = "90210"
            elif ftype == "tel" or "phone" in label or "phone" in fid: value = "1234567890"
            elif "area" in label: value = "1500"
            elif ftype == "number": value = "35"
            else: value = "John"
            self.cdp.form(result.selector, value=value)
        except LocatorError as e:
            self.log.warning(f"[Explorer] Form locate failed: {e}")

    # ── Success Detection ─────────────────────────────────────────

    def _check_success(self, state: dict, success_desc: str) -> bool:
        """Poll for success condition over several seconds."""
        # Extract keywords from Chinese description: "页面出现 XXX" → "XXX"
        keywords = []
        for phrase in success_desc.replace("页面出现", "").replace("URL包含", "").split("或"):
            kw = phrase.strip()
            if kw:
                keywords.append(kw)
        if not keywords:
            return False

        deadline = time.time() + self.SUCCESS_POLL_SECONDS
        hits = 0
        while time.time() < deadline:
            try:
                info = self.cdp.get_page_info()
                url = info.get("url", "")
                body = self.cdp.eval(
                    "(function(){return document.body?document.body.innerText.substring(0,2000):'';})()")
                for kw in keywords:
                    if kw.lower() in body.lower() or kw in url:
                        hits += 1
                        break
                if hits >= 2:  # two consecutive hits = confirmed
                    return True
            except Exception:
                pass
            time.sleep(self.SUCCESS_POLL_INTERVAL)
        return False

    # ── Compile Result ────────────────────────────────────────────

    def _compile_config(self, url: str, success_desc: str) -> dict:
        """Compile the exploration trajectory into a state machine JSON config."""
        keywords = [k.strip() for k in success_desc.replace("页面出现", "").replace("URL包含", "").split("或") if k.strip()]
        config = {
            "site": url.replace("http://", "").replace("https://", ""),
            "loop_until": {"any": [{"body_contains": keywords}]},
            "max_rounds": self.MAX_ROUNDS,
            "steps": [],
        }
        for t in self.trajectory:
            action = t["action"]
            atype = action.get("action", "")
            if atype == "done":
                continue
            step = {"when": {}}
            step.update(action)
            # Mark form steps as one-shot
            if atype == "form":
                step["id"] = f"step_{len(config['steps'])}"
            config["steps"].append(step)
        return config
