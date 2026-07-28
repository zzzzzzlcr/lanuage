"""JSON-driven form fill engine. Executes declarative step configs."""
import json
import random
import time
import logging
from variable_resolver import VariableResolver
from element_finder import ElementFinder
from locator import FieldLocator, LocatorError


class JSONExecutor:
    """Execute a JSON form fill config deterministically."""

    MAX_STEPS = 100  # safety limit
    DEFAULT_TIMEOUT = 30

    def __init__(self, config: dict, profile: dict, cdp, log=None, report_fn=None):
        self.config = config
        self.profile = profile
        self.cdp = cdp
        self.log = log or logging.getLogger(__name__)
        self.report_fn = report_fn or (lambda c, t, s, l: None)
        self.var = VariableResolver(profile, self.log)
        self.finder = ElementFinder(cdp, self.log)
        self.locator = FieldLocator(cdp, log=self.log)
        self._steps_run = 0
        self._frame_id = ""

    def run(self) -> bool:
        """Execute steps. Uses state machine if loop_until is set, linear otherwise."""
        if self.config.get("loop_until"):
            return self._run_stateful()
        return self._run_linear()

    def _run_linear(self) -> bool:
        """Original linear execution (backward compat)."""
        steps = self.config.get("steps", [])
        max_steps = min(len(steps), self.MAX_STEPS)

        for i in range(max_steps):
            step = steps[i]
            self.log.info(f"[JSON] Step {i+1}/{max_steps}: {step.get('action')}")

            step = self.var.resolve_dict(step)
            self._run_pre()

            ok = self._execute_step(step)
            # Throttle CDP calls to prevent connection issues
            time.sleep(0.2)
            if not ok and not step.get("optional"):
                self.log.error(f"[JSON] Step {i+1} failed: {step}")
                self.report_fn(self.cdp, self.profile.get("task_id", ""), "step_failed", self.log)
                return False

            self._steps_run += 1
            # Clear locator cache after each step (page may change)
            self.locator.clear_cache()

            if self._check_success():
                self.report_fn(self.cdp, self.profile.get("task_id", ""), "success", self.log)
                return True

        if self._check_success():
            self.report_fn(self.cdp, self.profile.get("task_id", ""), "success", self.log)
            return True

        if self._steps_run > 0:
            self.report_fn(self.cdp, self.profile.get("task_id", ""), "max_steps", self.log)
        return False

    def _run_stateful(self) -> bool:
        """State machine execution: snapshot → eval conditions → exec matching → repeat.

        Steps have: id, when (condition), action, field/find, selection_strategy.
        loop_until at config level defines the terminal condition.
        """
        steps = self.config.get("steps", [])
        loop_until = self.config.get("loop_until", {})
        # Global frame_url: resolve once, use for all steps
        global_frame = self.config.get("frame_url", "")
        if isinstance(global_frame, dict):
            global_frame = global_frame.get("url_contains", "")
        if global_frame:
            self._frame_id = self.locator._resolve_frame(global_frame)
        # Normalize loop_until format (LLM generates various shapes)
        if "or" in loop_until and "any" not in loop_until:
            loop_until = {"any": loop_until["or"]}
        elif "any" not in loop_until and "url_contains" in loop_until or "body_contains" in loop_until:
            # Flat format: wrap in any
            loop_until = {"any": [loop_until]}
        max_rounds = self.config.get("max_rounds", 50)
        executed_ids = set()

        for rnd in range(max_rounds):
            self.log.info(f"[JSON] Round {rnd+1}/{max_rounds}")

            # Clear locator cache each round (page may have changed)
            self.locator.clear_cache()

            # Check terminal condition
            if self._eval_condition(loop_until):
                self.log.info("[JSON] loop_until matched — done")
                self.report_fn(self.cdp, self.profile.get("task_id", ""), "success", self.log)
                return True

            # Check success as fallback
            if self._check_success():
                self.report_fn(self.cdp, self.profile.get("task_id", ""), "success", self.log)
                return True

            # Evaluate which steps' when conditions match current page
            matched = False
            for step in steps:
                sid = step.get("id", "")
                when = step.get("when", {})

                # Skip already-executed non-repeatable steps
                if sid and sid in executed_ids:
                    continue

                # Evaluate condition
                if when and not self._eval_condition(when):
                    self.log.debug(f"[JSON]   skip {sid}: when not met")
                    continue

                # Execute
                resolved = self.var.resolve_dict(step)
                self.log.info(f"[JSON]   exec {sid}: {resolved.get('action')}")
                ok = self._execute_step(resolved)
                if ok:
                    if sid:
                        executed_ids.add(sid)
                    matched = True
                    self._steps_run += 1
                    time.sleep(random.uniform(0.5, 1.5))
                elif not step.get("optional"):
                    self.log.warning(f"[JSON]   {sid} failed (non-optional)")

            if not matched:
                # Try advancing: click common wizard buttons (case-insensitive substring match)
                self.log.info("[JSON] No matching steps, trying auto-advance")
                advance_btns = ["Next", "Continue", "Submit", "Get estimate", "Get Estimate",
                    "Start", "Begin", "Get Started", "Find out", "Yes", "YES", "No", "NO",
                    "Record Request", "Get My", "See", "Go", "Apply", "Check", "Qualify",
                    "Download", "Subscribe", "UNLOCK", "Send"]
                for btn_text in advance_btns:
                    esc = btn_text.replace("'", "\\'")
                    js_mark = (
                        f"(function(){{var bs=document.querySelectorAll('button');"
                        f"for(var i=0;i<bs.length;i++){{"
                        f"if(bs[i].textContent.trim().toLowerCase().indexOf('{esc.lower()}')!==-1&&bs[i].offsetWidth>0)"
                        f"{{bs[i].setAttribute('data-auto-advance','1');return'found';}}}}"
                        f"return'none';}})()"
                    )
                    # Mark matching button then CDP click it
                    esc = btn_text.replace("'", "\\'")
                    js_mark = (
                        f"(function(){{var bs=document.querySelectorAll('button');"
                        f"for(var i=0;i<bs.length;i++){{"
                        f"if(bs[i].textContent.trim()==='{esc}'&&bs[i].offsetWidth>0)"
                        f"{{bs[i].setAttribute('data-auto-advance','1');return'found';}}}}"
                        f"return'none';}})()"
                    )
                    r = self.cdp.eval(js_mark, self._frame_id)
                    if "found" in r:
                        try:
                            self.cdp.click('[data-auto-advance=\"1\"]', self._frame_id)
                        except Exception:
                            pass
                        self.cdp.eval(
                            "(function(){var e=document.querySelector('[data-auto-advance]');"
                            "if(e)e.removeAttribute('data-auto-advance');})()", self._frame_id)
                        self.log.info(f"[JSON]   clicked {btn}")
                        time.sleep(random.uniform(2, 4))
                        break

            # Add delay for page transitions (quiz labels have setTimeout)
            if matched:
                time.sleep(random.uniform(1.5, 3))
            else:
                time.sleep(random.uniform(0.5, 1))

        return bool(executed_ids)

    def _eval_condition(self, cond: dict) -> bool:
        """Evaluate a when/loop_until condition."""
        if not cond:
            return True  # no condition = always match

        info = self.cdp.get_page_info()
        url = info.get("url", "")
        body = self.cdp.eval(
            "(function(){return document.body?document.body.innerText.substring(0,2000):'';})()",
            self._frame_id)

        # field_exists: check if locator can find this field
        if "field_exists" in cond:
            self.log.info("[JSON] DEBUG: field_exists branch entered")
            try:
                field = cond["field_exists"]
                frame_hint = field.get("frame_url", "")
                if isinstance(frame_hint, dict):
                    frame_hint = frame_hint.get("url_contains", "")
                ftype = field.get("type", "").lower()

                # Button detection: search by visible text, not locator
                if ftype == "button":
                    label = field.get("label", "")
                    frame_id = self.locator._resolve_frame(frame_hint) if frame_hint else self._frame_id
                    ok = self._find_visible_button(label, frame_id)
                    self.log.info(f"[JSON] field_exists button '{label}': {ok}")
                    return ok

                loc = self.locator.locate(field, frame_hint=frame_hint)
                ok = bool(loc and loc.selector)
                if ok:
                    esc = loc.selector.replace("'", "\\'")
                    vis = self.cdp.eval(
                        "(function(){var e=document.querySelector('" + esc + "');"
                        "return e&&e.offsetWidth>0?'yes':'no';})()", self._frame_id)
                    ok = 'yes' in str(vis)
                self.log.info("[JSON] field_exists %s: exist=%s vis=%s",
                    field.get('label','?'), bool(loc and loc.selector), 'yes' if ok else 'no')
                return ok
            except LocatorError as e:
                self.log.info(f"[JSON] field_exists {field.get('label','?')}: LocatorError {e}")
                return False

        # body_contains (also accept text_contains as alias)
        body_key = "body_contains" if "body_contains" in cond else ("text_contains" if "text_contains" in cond else None)
        if body_key:
            patterns = cond[body_key]
            if not isinstance(patterns, list): patterns = [patterns]
            if any(p in body for p in patterns):
                return True

        # url_contains
        if "url_contains" in cond:
            patterns = cond["url_contains"]
            if not isinstance(patterns, list): patterns = [patterns]
            if any(p in url for p in patterns):
                return True

        # page_matches (looser body match for loop_until)
        if "page_matches" in cond:
            if cond["page_matches"] in body:
                return True

        return False

    def _find_visible_button(self, label: str, frame_id: str = "") -> bool:
        """Check if a button/link with given text is visible on the page."""
        esc = label.replace("'", "\\'")
        js = (
            f"(function(){{var els=document.querySelectorAll('button,a,div,li,label,span[role=button],[role=button],[role=option]');"
            f"for(var i=0;i<els.length;i++){{"
            f"if(els[i].offsetWidth>0&&els[i].textContent.trim().indexOf('{esc}')!==-1)"
            f"return'yes';}}"
            f"return'no';}})()"
        )
        result = self.cdp.eval(js, frame_id)
        return "yes" in result

    def _resolve_frame(self, frame_spec: dict) -> str:
        """Resolve a frame spec to a CDP frameId via snapshot."""
        if not frame_spec:
            return ""
        try:
            import json as _j
            snap = self.cdp.snapshot()
            data = _j.loads(snap) if isinstance(snap, str) else snap
        except Exception:
            return ""
        url_pat = frame_spec.get("url_contains", "")
        for cf in data.get("childFrames", []):
            furl = cf.get("frame", {}).get("url", "")
            if url_pat in furl:
                return cf.get("frame", {}).get("frameId", "")
        return ""

    def _quiz_loop(self, step: dict) -> bool:
        """Quiz loop: randomly click visible options until stop condition met.

        step config:
          - stop_when: find spec for element to stop (e.g. {"name": "email"})
          - scope: CSS selector to limit search (e.g. ".reg-form"), optional
          - max_rounds: max iterations (default 20)
        """
        stop_when = step.get("stop_when", {})
        scope = step.get("scope", "")
        max_rounds = step.get("max_rounds", 20)
        skip_words = ['About','Terms','Privacy','Cookie','Sign In','Contact',
                      'Arbitration','Manage','Policy','Disclaimer','Copyright']

        for rnd in range(max_rounds):
            self.log.info(f"[JSON] quiz_loop round {rnd+1}/{max_rounds}")

            # Check stop condition first
            if stop_when:
                sel = self.finder.find(stop_when, {"frame_id": self._frame_id})
                if sel:
                    self.log.info(f"[JSON] quiz_loop: stop condition met")
                    return True

            # Check for forms FIRST — fill inputs before looking for quiz options
            has_inputs = self.cdp.eval(
                "(function(){var ins=document.querySelectorAll('input:not([type=hidden]),textarea,select');"
                "for(var i=0;i<ins.length;i++){if(ins[i].offsetWidth>0)return'yes';}return'no';})()",
                self._frame_id)
            if "yes" in has_inputs:
                self.log.info(f"[JSON] quiz_loop: form page detected, filling fields")
                # Try to fill each internal form step
                for sub_step in step.get('steps', []):
                    if sub_step.get('action') in ('form', 'click') and sub_step.get('optional'):
                        resolved = self.var.resolve_dict(sub_step)
                        ok = self._execute_step(resolved)
                        if ok:
                            self.log.info(f"[JSON] quiz_loop form: {sub_step.get('action')} OK")
                            time.sleep(random.uniform(0.5, 1))
                # After filling, try clicking Submit or Next
                for btn in ["Submit", "Next", "Continue"]:
                    js = (f"(function(){{var bs=document.querySelectorAll('button');"
                          f"for(var i=0;i<bs.length;i++){{if(bs[i].textContent.trim()==='{btn}'&&bs[i].offsetWidth>0)"
                          f"{{bs[i].click();return'clicked';}}}}return'none';}})()")
                    r = self.cdp.eval(js, self._frame_id)
                    if "clicked" in r:
                        self.log.info(f"[JSON] quiz_loop form: clicked {btn}")
                        time.sleep(random.uniform(3, 6))
                        break
                # Also wait for page to fully render after navigation
                time.sleep(random.uniform(1, 2))
                continue

            # Find all visible quiz-like options (short text, clickable, not footer)
            # If scope is given, only search within that container
            container = f"document.querySelector('{scope}')" if scope else "document"
            js = (
                f"var skip={json.dumps(skip_words)};"
                f"var root={container};if(!root)return'no container';"
                f"var r=[];var els=root.querySelectorAll('button,a,label,li,div,[role=button],[role=option]');"
                f"for(var i=0;i<els.length;i++){{"
                f"var e=els[i];var t=e.textContent.trim();"
                f"if(!e.offsetWidth||t.length<2||t.length>60)continue;"
                f"if(e.tagName==='LABEL'&&e.htmlFor)continue;"
                f"if(e.tagName==='TEXTAREA'||e.tagName==='SELECT')continue;"
                f"if(e.tagName==='INPUT'&&e.type!=='radio'&&e.type!=='checkbox')continue;"
                f"var bad=false;"
                f"for(var s=0;s<skip.length;s++){{if(t.indexOf(skip[s])!==-1){{bad=true;break;}}}}"
                f"if(!bad)r.push(i);}}"
                f"if(r.length>0){{"
                f"var pick=r[Math.floor(Math.random()*r.length)];"
                f"els[pick].setAttribute('data-qz','1');"
                f"return'clicked '+(r.length)+' opts';}}"
                f"return'no options';"
            )
            result = self.cdp.eval(
                f"(function(){{{js}}})()", self._frame_id)
            self.log.info(f"[JSON] quiz_loop: {result.strip()}")

            if "clicked" in result:
                # CDP-click the marked element
                try:
                    self.cdp.click('[data-qz="1"]', self._frame_id)
                except Exception:
                    pass
                # Clean up marker
                self.cdp.eval(
                    "(function(){var e=document.querySelector('[data-qz]');"
                    "if(e)e.removeAttribute('data-qz');})()", self._frame_id)
                time.sleep(random.uniform(1, 2))

                # Try click Next/Continue
                for btn_text in ["Next", "Continue"]:
                    js_find = (
                        f"(function(){{var bs=document.querySelectorAll('button');"
                        f"for(var i=0;i<bs.length;i++){{"
                        f"if(bs[i].textContent.trim()==='{btn_text}'&&bs[i].offsetWidth>0)"
                        f"{{bs[i].click();return'clicked {btn_text}';}}}}"
                        f"return'none';}})()"
                    )
                    r = self.cdp.eval(js_find, self._frame_id)
                    if "clicked" in r:
                        self.log.info(f"[JSON] quiz_loop: {r.strip()}")
                        time.sleep(random.uniform(2, 4))
                        break
            else:
                # No options found in main frame — try searching within same-origin iframes
                iframe_found = False
                try:
                    import json as _j
                    snap = self.cdp.snapshot()
                    data = _j.loads(snap) if isinstance(snap, str) else snap
                    for cf in data.get("childFrames", []):
                        fid = cf.get("frame", {}).get("frameId", "")
                        furl = cf.get("frame", {}).get("url", "")
                        if not fid: continue
                        # Search within this iframe for options
                        r = self.cdp.eval(f"(function(){{{js}}})()", fid)
                        self.log.info(f"[JSON] quiz_loop iframe({furl[:40]}): {r.strip()}")
                        if "clicked" in r:
                            try: self.cdp.click('[data-qz="1"]', fid)
                            except: pass
                            self.cdp.eval("(function(){var e=document.querySelector('[data-qz]');if(e)e.removeAttribute('data-qz');})()", fid)
                            time.sleep(random.uniform(1, 2))
                            # Try Next/Continue in iframe
                            for btn_text in ["Next", "Continue"]:
                                js_find = (
                                    f"(function(){{var bs=document.querySelectorAll('button');"
                                    f"for(var i=0;i<bs.length;i++){{"
                                    f"if(bs[i].textContent.trim()==='{btn_text}'&&bs[i].offsetWidth>0)"
                                    f"{{bs[i].click();return'clicked {btn_text}';}}}}"
                                    f"return'none';}})()"
                                )
                                rr = self.cdp.eval(js_find, fid)
                                if "clicked" in rr:
                                    self.log.info(f"[JSON] quiz_loop iframe: {rr.strip()}")
                                    time.sleep(random.uniform(2, 4))
                                    break
                            # Also save frame_id for form steps later
                            self._frame_id = fid
                            iframe_found = True
                            break
                except Exception as ex:
                    self.log.warning(f"[JSON] quiz_loop iframe search error: {ex}")

                if not iframe_found:
                    # Still no options — try Next/Continue to advance
                    for btn_text in ["Next", "Continue"]:
                        js_find = (
                            f"(function(){{var bs=document.querySelectorAll('button');"
                            f"for(var i=0;i<bs.length;i++){{"
                            f"if(bs[i].textContent.trim()==='{btn_text}'&&bs[i].offsetWidth>0)"
                            f"{{bs[i].click();return'clicked {btn_text}';}}}}"
                            f"return'none';}})()"
                        )
                        r = self.cdp.eval(js_find, self._frame_id)
                        if "clicked" in r:
                            self.log.info(f"[JSON] quiz_loop: {r.strip()}")
                            time.sleep(random.uniform(2, 4))
                            break

                time.sleep(random.uniform(1, 2))

        # Final check
        if stop_when:
            sel = self.finder.find(stop_when, {"frame_id": self._frame_id})
            if sel:
                return True
        return True  # quiz_loop always succeeds (optional by nature)

    def _select_option(self, step: dict) -> bool:
        """Select an option using semantic strategy (match_text, random, first)."""
        strategy = step.get("selection_strategy", {})
        stype = strategy.get("type", "random")
        skip_words = ['About','Terms','Privacy','Cookie','Sign In','Contact',
                      'Arbitration','Manage','Policy','Disclaimer',
                      'Get','View','Submit','Continue','Next','See','Go','Check',
                      'Previous','Back','Skip','Reset','Clear All']

        if stype == "random":
            token = f"qs{int(time.time()*1000)%100000}"  # unique per call, prevents Q1→Q2 pollution
            # Auto-detect quiz scope: search for active question container with radio/checkbox
            container = step.get("container", "")
            if not container:
                detect_js = (
                    "(function(){var old=document.querySelector('[data-quiz-scope]');if(old)old.removeAttribute('data-quiz-scope');"
                    "var q=document.querySelector('.sv-question.active,fieldset:not([disabled]),[role=radiogroup],[role=group]');"
                    "if(!q)return'none';"
                    "var ctls=q.querySelectorAll('input[type=checkbox],input[type=radio],[role=checkbox],[role=radio]');"
                    "if(!ctls.length)return'none';"
                    "var t=(q.textContent||'').toLowerCase();"
                    "if(/agree|terms|privacy|consent|subscribe|marketing|policy/.test(t))return'consent';"
                    "q.setAttribute('data-quiz-scope','1');return'scope';})()"
                )
                r = self.cdp.eval(detect_js, self._frame_id)
                if "scope" in str(r):
                    container = '[data-quiz-scope="1"]'

            if container:
                # Scoped label search with unique token + post-click verification
                scope_expr = f"document.querySelector('{container}')"
                js = (
                    f"(function(){{"
                    # Clean ALL old markers first
                    f"document.querySelectorAll('[data-sel],[data-quiz-token]').forEach(function(e){{e.removeAttribute('data-sel');e.removeAttribute('data-quiz-token');}});"
                    f"var scope={scope_expr};if(!scope)return'no-scope';"
                    f"var labels=Array.from(scope.querySelectorAll('label')).filter(function(l){{"
                    f"var inp=l.querySelector('input[type=checkbox],input[type=radio]');"
                    f"return inp&&!inp.disabled&&l.offsetWidth>0;}});"
                    f"if(!labels.length)return'none';"
                    f"var label=labels[Math.floor(Math.random()*labels.length)];"
                    f"var inp=label.querySelector('input[type=checkbox],input[type=radio]');"
                    f"label.setAttribute('data-sel','{token}');"
                    f"return JSON.stringify({{picked:true,token:'{token}',label:label.textContent.trim().substring(0,30)}});}})()"
                )
                result = self.cdp.eval(js, self._frame_id)
                result_str = str(result)
                if "picked" in result_str:
                    sel = f'[data-sel=\"{token}\"]'
                    # Use CDP click limited to the quiz scope
                    scope_css = container
                    scoped_sel = f'{scope_css} {sel}'
                    self.cdp.click(scoped_sel, self._frame_id)
                    time.sleep(0.3)
                    # Verify checkbox/radio is now checked
                    verify = self.cdp.eval(
                        f"(function(){{var e=document.querySelector('{sel}');"
                        f"if(!e)return'no elem';var inp=e.querySelector('input[type=checkbox],input[type=radio]');"
                        f"return inp&&inp.checked?'checked':'not checked';}})()",
                        self._frame_id)
                    self.log.info(f"[JSON] quiz scoped select: {result_str[:80]} verify: {str(verify).strip()}")
                    return "checked" in str(verify)

            # control_types mode: scoped search within quiz container
            ctl_types = step.get("control_types", [])
            if ctl_types:
                container = step.get("container", "document")
                scope_expr = f"document.querySelector('{container}')" if container != "document" else "document"
                js = (
                    f"(function(){{var scope={scope_expr};if(!scope)return'no-scope';"
                    f"var labels=Array.from(scope.querySelectorAll('label')).filter(function(l){{"
                    f"var inp=l.querySelector('input[type=checkbox],input[type=radio]');"
                    f"return inp&&!inp.disabled&&l.offsetWidth>0;}});"
                    f"if(!labels.length)return'none';"
                    f"var label=labels[Math.floor(Math.random()*labels.length)];"
                    f"var inp=label.querySelector('input[type=checkbox],input[type=radio]');"
                    f"label.setAttribute('data-sel','{token}');"
                    f"return JSON.stringify({{picked:true,token:'{token}',label:label.textContent.trim().substring(0,30)}});}})()"
                )
                result = self.cdp.eval(js, self._frame_id)
                result_str = str(result)
                if "picked" in result_str:
                    self.cdp.click(f'[data-sel=\"{token}\"]', self._frame_id)
                    time.sleep(0.2)
                    # Verify checked
                    verify = self.cdp.eval(
                        f"(function(){{var e=document.querySelector('[data-sel=\"{token}\"]');"
                        f"if(!e)return'no elem';var inp=e.querySelector('input[type=checkbox],input[type=radio]');"
                        f"return inp&&inp.checked?'checked':'not checked';}})()",
                        self._frame_id)
                    self.log.info(f"[JSON] quiz select: {result_str[:80]} verify: {str(verify).strip()}")
                    return "checked" in str(verify)

            # Clean up any leftover markers from previous calls
            self.cdp.eval(
                "(function(){var e=document.querySelector('[data-sel]');if(e)e.removeAttribute('data-sel');})()",
                self._frame_id)
            # Global random: cleanup old markers, use unique token
            self.cdp.eval(
                "(function(){document.querySelectorAll('[data-sel]').forEach(function(e){e.removeAttribute('data-sel');});})()",
                self._frame_id)
            # Scope to container if specified
            container = step.get("container", "")
            scope = container if container else "document"
            # Prefer labels/divs over buttons (buttons are usually CTAs, not options)
            js = (
                f"var skip={json.dumps(skip_words)};"
                f"var r=[];var els=({scope}).querySelectorAll('button,a,label,li,div,[role=button],[role=option]');"
                f"for(var i=0;i<els.length;i++){{"
                f"var e=els[i];var t=e.textContent.trim();"
                f"if(!e.offsetWidth||t.length<2||t.length>60)continue;"
                f"if(e.tagName==='LABEL'&&e.htmlFor)continue;"
                f"if(e.tagName==='TEXTAREA'||e.tagName==='SELECT')continue;"
                f"if(e.tagName==='INPUT'&&e.type!=='radio'&&e.type!=='checkbox')continue;"
                # Exclude nav/header/footer elements (position-based, not class-based)
                f"if(e.closest('nav,header,footer'))continue;"
                f"var rect=e.getBoundingClientRect();"
                f"if(rect.top<50||rect.bottom>window.innerHeight-50)continue;"
                f"var bad=false;for(var s=0;s<skip.length;s++){{"
                f"if(t.indexOf(skip[s])!==-1){{bad=true;break;}}}}"
                f"if(!bad)r.push(i);}}"
                f"if(r.length>0){{"
                f"var pick=r[Math.floor(Math.random()*r.length)];"
                f"els[pick].setAttribute('data-sel','{token}');"
                f"return'clicked_{token} '+(r.length)+' opts';}}"
                f"return'none';"
            )
            result = self.cdp.eval(f"(function(){{{js}}})()", self._frame_id)
            if "clicked" in result:
                # Extract unique token from result "clicked_qs12345 5 opts"
                parts = result.split("_")
                if len(parts) > 1:
                    token = parts[1].split(" ")[0]
                    sel = f'[data-sel=\"{token}\"]'
                else:
                    sel = '[data-sel]'
                self.cdp.click(sel, self._frame_id)
                time.sleep(0.2)
                self.cdp.eval("(function(){document.querySelectorAll('[data-sel]').forEach(function(e){e.removeAttribute('data-sel');});})()", self._frame_id)
                return True
            return False

        elif stype == "match_text":
            target = strategy.get("value", "")
            fallback = strategy.get("fallback", "first")
            # Find element with matching text
            js = (
                f"var t='{target.replace(chr(39),chr(92)+chr(39))}';"
                f"var els=document.querySelectorAll('button,a,label,li,div,[role=button],[role=option]');"
                f"for(var i=0;i<els.length;i++){{"
                f"var tx=els[i].textContent.trim();"
                f"if(tx.indexOf(t)!==-1&&els[i].offsetWidth>0){{"
                f"els[i].click();return'clicked';}}}}"
                f"return'none';"
            )
            result = self.cdp.eval(f"(function(){{{js}}})()", self._frame_id)
            if "clicked" in result:
                return True
            # Fallback
            if fallback == "first":
                return self._select_option({"selection_strategy": {"type": "first"}})
            elif fallback == "random":
                return self._select_option({"selection_strategy": {"type": "random"}})
            return False

        elif stype == "first":
            js = (
                f"var skip={json.dumps(skip_words)};"
                f"var els=document.querySelectorAll('button,a,label,li,div,[role=button],[role=option]');"
                f"for(var i=0;i<els.length;i++){{"
                f"var e=els[i];var t=e.textContent.trim();"
                f"if(!e.offsetWidth||t.length<2||t.length>60)continue;"
                f"var bad=false;for(var s=0;s<skip.length;s++){{"
                f"if(t.indexOf(skip[s])!==-1){{bad=true;break;}}}}"
                f"if(!bad){{e.click();return'clicked';}}}}"
                f"return'none';"
            )
            result = self.cdp.eval(f"(function(){{{js}}})()", self._frame_id)
            return "clicked" in result

        return False

    def _execute_step(self, step: dict) -> bool:
        """Execute a single step. Returns True on success."""
        action = step.get("action", "")
        retry = step.get("retry", 1)
        optional = step.get("optional", False)

        # Resolve frame_id: supports "frame_url" (LLM) and "frame" (legacy) keys
        # Normalize: LLM may generate frame_url as string or dict {"url_contains":"..."}
        frame_hint = step.get("frame_url") or step.get("frame")
        if isinstance(frame_hint, dict):
            frame_hint = frame_hint.get("url_contains", "")
        if frame_hint:
            self._frame_id = self.locator._resolve_frame(frame_hint)
        # Also check field-level frame_url
        field = step.get("field", {}) or {}
        field_frame = field.get("frame_url", "")
        if isinstance(field_frame, dict):
            field_frame = field_frame.get("url_contains", "")
        if not self._frame_id and field_frame:
            self._frame_id = self.locator._resolve_frame(field_frame)
        # Also check find-level
        find = step.get("find", {}) or {}
        find_frame = find.get("frame_url", "")
        if isinstance(find_frame, dict):
            find_frame = find_frame.get("url_contains", "")
        if not self._frame_id and find_frame:
            self._frame_id = self.locator._resolve_frame(find_frame)

        for attempt in range(retry):
            try:
                if action in ("wait", "delay"):
                    # delay uses "time" in ms, wait uses min/max in seconds
                    if "time" in step:
                        t = step["time"] / 1000.0
                    else:
                        t = random.uniform(step.get("min", 0.3), step.get("max", 1.5))
                    # Conditional early exit: if page is ready earlier, stop waiting
                    check_url = step.get("or_until_url")
                    check_body = step.get("or_until_body")
                    early_exit = check_url or check_body
                    if early_exit:
                        deadline = time.time() + t
                        while time.time() < deadline:
                            time.sleep(min(1, deadline - time.time()))
                            try:
                                info = self.cdp.get_page_info()
                                url = info.get("url", "")
                                body = self.cdp.eval(
                                    "(function(){return document.body?document.body.innerText.substring(0,2000):'';})()",
                                    self._frame_id)
                                if check_url and check_url in url:
                                    self.log.info(f"[JSON] wait early exit: URL matched '{check_url}'")
                                    return True
                                if check_body and check_body.lower() in body.lower():
                                    self.log.info(f"[JSON] wait early exit: body matched '{check_body}'")
                                    return True
                            except Exception:
                                pass
                        return True
                    else:
                        time.sleep(t)
                        return True

                elif action == "scroll":
                    px = random.randint(step.get("min", 100), step.get("max", 500))
                    self.cdp.eval(f"(function(){{window.scrollBy(0,{px});}})()", self._frame_id)
                    return True

                elif action == "eval":
                    self.cdp.eval(f"(function(){{{step['script']}}})()", self._frame_id)
                    return True

                elif action == "report":
                    self.report_fn(self.cdp, self.profile.get("task_id", ""), step["step"], self.log)
                    return True

                elif action == "click":
                    find = step.get("find", {})
                    field = step.get("field")
                    fctx = {"frame_id": self._frame_id} if self._frame_id else None
                    if field:
                        # Semantic field: locate at runtime, resolve frame fresh each time
                        frame_hint = field.get("frame_url", "")
                        selector = None
                        for retry_i in range(3):
                            try:
                                loc = self.locator.locate(field, frame_hint=frame_hint)
                                selector = loc.selector
                                if loc.frame_id:
                                    self._frame_id = loc.frame_id
                                self.log.info(f"[JSON] click: located '{field}' via {loc.strategy} ({loc.confidence})")
                                break
                            except LocatorError as e:
                                if retry_i < 2:
                                    wait_s = (retry_i + 1) * 2
                                    self.log.info(f"[JSON] click: retry {retry_i+1}/3 in {wait_s}s — {e}")
                                    self.cdp.wait_page_stable(timeout=wait_s)
                                    self.locator.clear_cache()
                                else:
                                    self.log.warning(f"[JSON] click: cannot locate field after 3 retries: {e}")
                                    if optional: return True
                                    return False
                    else:
                        selector = self.finder.find(find, fctx)
                        # Auto-detect iframe: if not found in main frame, search all iframes
                        if not selector and not self._frame_id:
                            for fid in self.locator._list_iframes():
                                sel2 = self.finder.find(find, {"frame_id": fid})
                                if sel2:
                                    self._frame_id = fid
                                    selector = sel2
                                    self.log.info(f"[JSON] click: auto-detected iframe {fid} for '{find.get('text','')}'")
                                    break
                    # Fallback: LLM may use "selector" field directly
                    if not selector and step.get("selector"):
                        sel = step["selector"]
                        selector = sel.get("primary", sel) if isinstance(sel, dict) else sel
                    if not selector:
                        if optional:
                            return True
                        self.log.warning(f"[JSON] click: element not found: {find or field}")
                        if attempt + 1 < retry:
                            time.sleep(random.uniform(2, 4))
                            continue
                        return False
                    self.cdp.click(selector, self._frame_id)
                    # For <a> links: CDP click navigates href=# instead of onclick, JS .click() fixes it
                    esc = selector.replace("'", "\\'")
                    self.cdp.eval(
                        f"(function(){{var e=document.querySelector('{esc}');if(e&&e.tagName==='A')e.click();}})()",
                        self._frame_id)
                    # Auto-wait for page to stabilize after click (buttons often trigger navigation/DOM changes)
                    self.cdp.wait_page_stable(timeout=15)
                    if "wait_after" in step:
                        time.sleep(random.uniform(step["wait_after"][0], step["wait_after"][1]))
                    return True

                elif action == "form":
                    find = step.get("find", {})
                    field = step.get("field")
                    fctx = {"frame_id": self._frame_id} if self._frame_id else None
                    if field:
                        frame_hint = field.get("frame_url", "")
                        selector = None
                        for retry_i in range(3):  # retry with backoff for slow page loads
                            try:
                                loc = self.locator.locate(field, frame_hint=frame_hint)
                                selector = loc.selector
                                if loc.frame_id:
                                    self._frame_id = loc.frame_id
                                self.log.info(f"[JSON] form: located '{field}' via {loc.strategy} ({loc.confidence})")
                                break
                            except LocatorError as e:
                                if retry_i < 2:
                                    wait_s = (retry_i + 1) * 3
                                    self.log.info(f"[JSON] form: retry {retry_i+1}/3 in {wait_s}s — {e}")
                                    self.cdp.wait_page_stable(timeout=wait_s * 2)
                                    self.locator.clear_cache()
                                else:
                                    # If select is set, try finding by text match as fallback
                                    sel_text = step.get("select", "")
                                    label_text = (step.get("field", {}) or {}).get("label", "")
                                    find_text = sel_text if (sel_text and sel_text != "__random__") else label_text
                                    if find_text:
                                        selector = self.finder.find({"text": find_text}, fctx)
                                        if selector:
                                            self.log.info(f"[JSON] form: fallback find by text '{find_text}' → {selector}")
                                            break
                                    self.log.warning(f"[JSON] form: cannot locate field after 3 retries: {e}")
                                    return False
                    else:
                        selector = self.finder.find(find, fctx)
                    if not selector and step.get("selector"):
                        sel = step["selector"]
                        selector = sel.get("primary", sel) if isinstance(sel, dict) else sel
                    if not selector:
                        self.log.warning(f"[JSON] form: element not found: {find or field}")
                        return False
                    value = step.get("value")
                    check = step.get("check")
                    select = step.get("select")
                    # Phase 1: Route dropdown intent to SelectExplorer
                    if select and not value and not check:
                        classification = self._classify_select_intent(step, selector)
                        if classification == "DROPDOWN":
                            from select_explorer import SelectExplorer, SelectIntent, CandidateRef
                            if not hasattr(self, '_select_explorer'):
                                self._select_explorer = SelectExplorer(self.cdp, self.log)
                            candidates = self._normalize_explorer_candidates(loc)
                            intent = SelectIntent(
                                label=field.get("label", ""),
                                mode="random" if select == "__random__" else "exact",
                                option=None if select == "__random__" else select,
                            )
                            outcome = self._select_explorer.execute(intent, candidates)
                            self.log.info(f"[JSON] explorer: {outcome.status}")
                            if outcome.ok:
                                return True
                            if outcome.status in ("NOT_VERIFIED", "OPTION_NOT_FOUND",
                                                   "OPEN_FAILED", "NO_SAFE_TRIGGER"):
                                return False  # fail closed
                    # Quiz-group detection: __random__ on text anchor near radio/checkbox
                    if select == "__random__" and not value and not check:
                        if self._try_quiz_group(selector, self._frame_id):
                            return True
                    ok = self._smart_form(selector, value=value, check=check, select=select,
                                          frame_id=self._frame_id)
                    if not ok:
                        return False
                    return True

                elif action == "wait_for":
                    timeout = step.get("timeout", 30)
                    start = time.time()
                    while time.time() - start < timeout:
                        if "find" in step:
                            selector = self.finder.find(step["find"])
                            if selector:
                                return True
                        if "url_contains" in step:
                            info = self.cdp.get_page_info()
                            if step["url_contains"] in info.get("url", ""):
                                return True
                        time.sleep(0.5)
                    self.log.warning(f"[JSON] wait_for timed out after {timeout}s")
                    return False

                elif action == "if":
                    cond = self._eval_condition(step)
                    branch = step["then"] if cond else step.get("else", [])
                    for s in branch:
                        s = self.var.resolve_dict(s)
                        self._execute_step(s)
                    return True

                elif action in ("goto", "navigate"):
                    # Navigation step: just change URL
                    target = step.get("url", "")
                    if target:
                        self.cdp.eval(f"(function(){{window.location.href='{target}';}})()")
                        time.sleep(2)
                    return True

                elif action == "select":
                    return self._select_option(step)

                elif action == "quiz_loop":
                    return self._quiz_loop(step)

                else:
                    self.log.warning(f"[JSON] Unknown action: {action}")
                    return False

            except Exception as e:
                self.log.error(f"[JSON] Step error: {e}")
                if attempt + 1 < retry:
                    time.sleep(random.uniform(2, 4))
                    continue
                return False

        return False

    def _classify_select_intent(self, step: dict, selector: str) -> str:
        """Classify form+select intent: DROPDOWN | CHOICE_GROUP | UNKNOWN."""
        if step.get("action") != "form" or "select" not in step:
            return "NOT_APPLICABLE"
        esc = selector.replace("'", "\\'")
        js = (
            f"(function(){{var a=document.querySelector('{esc}');if(!a)return'{{}}';"
            f"var area=a.closest('[class*=form-item],fieldset,div');"
            f"if(!area)area=a.parentElement;"
            f"var radios=area.querySelectorAll('input[type=radio],input[type=checkbox]');"
            f"var vis=0;for(var i=0;i<radios.length;i++){{if(radios[i].offsetWidth>0)vis++;}}"
            f"var sel=area.querySelector('select');"
            f"var cb=area.querySelector('[role=combobox]');"
            f"return JSON.stringify({{has_native_select:!!sel,has_aria_combobox:!!cb,"
            f"has_visible_choice_group:vis>=2,has_single_trigger:!vis||vis<2}});}})()"
        )
        raw = self.cdp.eval(js)
        try:
            probe = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            probe = {}
        if probe.get("has_native_select") or probe.get("has_aria_combobox"):
            return "DROPDOWN"
        if probe.get("has_visible_choice_group"):
            return "CHOICE_GROUP"
        if probe.get("has_single_trigger"):
            return "DROPDOWN"
        return "UNKNOWN"

    def _normalize_explorer_candidates(self, loc) -> list:
        """Convert LocatorResult to list of CandidateRef for SelectExplorer."""
        from select_explorer import CandidateRef
        refs = [CandidateRef(
            selector=loc.selector,
            frame_id=loc.frame_id,
            source=loc.strategy,
            confidence=loc.confidence
        )]
        for alt in (loc.alternatives or []):
            refs.append(CandidateRef(
                selector=alt.selector,
                frame_id=alt.frame_id or loc.frame_id,
                source=alt.strategy,
                confidence=alt.confidence
            ))
        return refs

    def _detect_quiz_scope(self, selector: str, frame_id: str = "") -> str | None:
        """Detect if selector is a text anchor near radio/checkbox quiz options.
        Returns container CSS selector if found, None otherwise."""
        esc = selector.replace("'", "\\'")
        js = (
            f"(function(){{var anchor=document.querySelector('{esc}');if(!anchor)return'none';"
            f"var box=anchor.closest('fieldset,section,[role=group],[role=radiogroup]');"
            f"if(!box)box=anchor.closest('form,main,div[class*=question],div[class*=quiz],div[class*=card]');"
            f"if(!box)return'none';"
            f"var controls=box.querySelectorAll('input[type=checkbox],input[type=radio],[role=checkbox],[role=radio]');"
            f"if(!controls.length)return'none';"
            f"var text=box.textContent.toLowerCase();"
            f"if(/\\b(agree|terms|privacy|consent|subscribe|marketing|policy)\\b/.test(text))return'consent';"
            f"box.setAttribute('data-quiz-scope','1');"
            f"return'scope';}})()"
        )
        result = self.cdp.eval(js, frame_id)
        result = str(result).strip().strip('"')
        if result == 'scope':
            return '[data-quiz-scope="1"]'
        return None

    def _try_quiz_group(self, selector: str, frame_id: str = "") -> bool:
        """Detect quiz option group from text anchor and route accordingly."""
        import json as _json
        # Clean up stale markers from previous steps
        self.cdp.eval(
            "(function(){var e=document.querySelector('[data-quiz-scope]');if(e)e.removeAttribute('data-quiz-scope');"
            "var s=document.querySelector('[data-sel]');if(s)s.removeAttribute('data-sel');})()",
            frame_id)
        esc = selector.replace("'", "\\'")
        js = (
            "(function(){var anchor=document.querySelector('"+esc+"');"
            "var q=anchor?anchor.closest('.sv-question,fieldset,[role=group],[role=radiogroup]'):null;"
            "if(!q)q=document.querySelector('.sv-question.active,fieldset:not([disabled]),[role=radiogroup]');"
            "if(!q)return JSON.stringify({matched:false});"
            "var ctls=Array.from(q.querySelectorAll('input[type=\"checkbox\"],input[type=\"radio\"],[role=\"checkbox\"],[role=\"radio\"]')).filter(function(e){return !e.disabled&&e.getAttribute('aria-disabled')!=='true';});"
            "if(!ctls.length)return JSON.stringify({matched:false});"
            "var t=(q.textContent||'').toLowerCase();"
            "if(/agree|terms|privacy|consent|subscribe|marketing|policy/.test(t))return JSON.stringify({matched:false,reason:'consent'});"
            "q.setAttribute('data-quiz-scope','1');"
            "return JSON.stringify({matched:true,count:ctls.length,type:ctls[0].type||ctls[0].getAttribute('role')||''});})()"
        )
        raw = self.cdp.eval(js, frame_id)
        try:
            if isinstance(raw, dict):
                result = raw
            else:
                result = _json.loads(raw)
                if isinstance(result, str): result = _json.loads(result)
        except Exception as e:
            self.log.info(f"[JSON] quiz detect error: {e} raw={str(raw)[:80]}")
            return False
        if not result.get("matched"):
            self.log.info(f"[JSON] quiz detect: no match, result={result}")
            return False
        self.log.info(f"[JSON] quiz-group: {result.get('count')} {result.get('type')} controls")
        return self._select_option({"container":'[data-quiz-scope="1"]',"control_types":["checkbox","radio"],"selection_strategy":{"type":"random"}})

    def _smart_form(self, selector: str, value: str = None, check: str = None,
                    select: str = None, frame_id: str = "") -> bool:
        """Handle form interaction for any element type (native or custom widget).

        Detects the element type and uses the appropriate interaction:
        - Native <select> → cdp.form --select
        - Custom combobox (div[role=combobox], MUI Select) → click open + click option
        - Range slider → set value + dispatch input/change events
        - Checkbox/radio → click to toggle
        - Text input → cdp.form --value
        """
        esc = selector.replace("'", "\\'")
        js_info = (
            f"(function(){{var e=document.querySelector('{esc}');if(!e)return'null';"
            f"return JSON.stringify({{tag:e.tagName,type:e.type||'',"
            f"role:e.getAttribute('role')||'',aria:e.getAttribute('aria-expanded')||'',"
            f"isContentEditable:e.isContentEditable||e.contentEditable==='true'}});}})()"
        )
        raw = self.cdp.eval(js_info, frame_id)
        try:
            if isinstance(raw, dict):
                info = raw
            else:
                info = json.loads(raw)
                if isinstance(info, str):
                    info = json.loads(info)
        except Exception:
            info = {}
        tag = (info.get('tag', '') or '').upper()
        role = info.get('role', '') or ''
        etype = (info.get('type', '') or '').lower()
        is_ce = info.get('isContentEditable', False)

        self.log.info(f"[JSON] smart_form: tag={tag} role={role} type={etype}")

        # --- RANGE / SLIDER (random) ---
        if select == '__random__' and (etype == 'range' or role == 'slider'):
            random_js = (
                f"(function(){{var e=document.querySelector('{esc}');if(!e)return'fail';"
                f"var mn=parseFloat(e.min)||10000;var mx=parseFloat(e.max)||100000;"
                f"var v=mn+Math.random()*(mx-mn);e.value=v;"
                f"e.dispatchEvent(new Event('input',{{bubbles:true}}));"
                f"e.dispatchEvent(new Event('change',{{bubbles:true}}));"
                f"return JSON.stringify({{value:v}});}})()"
            )
            raw = self.cdp.eval(random_js, frame_id)
            raw_str = str(raw) if not isinstance(raw, str) else raw
            self.log.info(f"[JSON] smart_form: random range → {raw_str[:120]}")
            return 'fail' not in raw_str

        # --- RADIO / CHIP / CARD (select=value on non-select elements) ---
        if select and tag != 'SELECT' and etype != 'select-one':
            esc_val = select.replace("'", "\\'")
            # Random: pick a random button/card in the group
            if select == '__random__':
                # Mark a random candidate, then CDP-click it (real mouse events for React)
                mark_js = (
                    f"(function(){{var e=document.querySelector('{esc}');if(!e)return'fail';"
                    f"var p=e.parentElement;if(!p)return'fail';"
                    f"var btns=p.querySelectorAll('button,[role=radio],[role=option],.chip,.pic,label,input[type=radio],input[type=checkbox]');"
                    f"for(var lv=0;lv<3&&!btns.length;lv++){{p=p.parentElement;if(!p)break;btns=p.querySelectorAll('button,[role=radio],[role=option],.chip,.pic,label,input[type=radio],input[type=checkbox]');}}"
                    f"var vis=[];for(var i=0;i<btns.length;i++){{var el=btns[i];var t=el.textContent.trim().toLowerCase();if(!t&&(el.type==='radio'||el.type==='checkbox')&&el.parentElement&&el.parentElement.tagName==='LABEL'){{el=el.parentElement;t=el.textContent.trim().toLowerCase();}}if(t&&el.offsetWidth>0&&!el.disabled&&t!=='reset'&&t!=='back'&&t!=='clear'&&t!=='next'&&t!=='previous'&&t!=='continue'&&t!=='submit')vis.push(el);}}"
                    f"if(!vis.length){{var sec=e.closest('section,div[class*=card],div[class*=container],main,form');if(sec){{btns=sec.querySelectorAll('button,[role=radio],[role=option],.chip,.pic,label,input[type=radio],input[type=checkbox]');for(var i=0;i<btns.length;i++){{var el=btns[i];var t=el.textContent.trim().toLowerCase();if(!t&&(el.type==='radio'||el.type==='checkbox')&&el.parentElement&&el.parentElement.tagName==='LABEL'){{el=el.parentElement;t=el.textContent.trim().toLowerCase();}}if(t&&el.offsetWidth>0&&!el.disabled&&t!=='reset'&&t!=='back'&&t!=='clear'&&t!=='next'&&t!=='previous'&&t!=='continue'&&t!=='submit')vis.push(el);}}}}}}"
                    f"if(!vis.length)return'fail';var pick=vis[Math.floor(Math.random()*vis.length)];"
                    f"pick.setAttribute('data-rnd-pick','1');return JSON.stringify({{text:pick.textContent.trim().substring(0,30)}});}})()"
                )
                raw = self.cdp.eval(mark_js, frame_id)
                raw_str = str(raw) if not isinstance(raw, str) else raw
                if 'fail' in raw_str:
                    self.log.info(f"[JSON] smart_form: random radio/chip → no candidates")
                    return False
                self.cdp.click('[data-rnd-pick=\"1\"]', frame_id)
                time.sleep(0.3)
                self.log.info(f"[JSON] smart_form: random radio/chip → {raw_str[:120]}")
                return True
            # Specific value: find and click the matching radio/chip/card
            # Mark the matching element, then CDP-click (real mouse events, works on all pages)
            mark_js = (
                f"(function(){{var v='{esc_val.lower()}';"
                f"var labels=document.querySelectorAll('label');"
                f"for(var i=0;i<labels.length;i++){{"
                f"if(labels[i].textContent.trim().toLowerCase().indexOf(v)!==-1&&labels[i].offsetWidth>0){{"
                f"labels[i].setAttribute('data-rnd-pick','1');return'clicked';}}}}"
                f"var radios=document.querySelectorAll('input[type=radio][value=\"{esc_val}\" i]');"
                f"if(radios.length>0){{radios[0].setAttribute('data-rnd-pick','1');return'clicked';}}"
                f"var chips=document.querySelectorAll('[data-value],.chip,.pic,[class*=rating],[class*=-star]');"
                f"for(var i=0;i<chips.length;i++){{"
                f"var dv=chips[i].getAttribute('data-value')||'';"
                f"if(dv.toLowerCase()===v&&chips[i].offsetWidth>0){{chips[i].setAttribute('data-rnd-pick','1');return'clicked';}}}}"
                f"for(var i=0;i<chips.length;i++){{"
                f"var tc=chips[i].textContent.trim();"
                f"if(tc.toLowerCase()===v&&chips[i].offsetWidth>0){{chips[i].setAttribute('data-rnd-pick','1');return'clicked';}}}}"
                f"for(var i=0;i<chips.length;i++){{"
                f"var tc=chips[i].textContent.trim();"
                f"if(tc.length<20&&tc.toLowerCase().indexOf(v)!==-1&&chips[i].offsetWidth>0){{"
                f"chips[i].setAttribute('data-rnd-pick','1');return'clicked';}}}}"
                f"return'not found';}})()"
            )
            result = self.cdp.eval(mark_js, frame_id)
            result = str(result) if not isinstance(result, str) else result
            if 'clicked' in result:
                self.cdp.click('[data-rnd-pick=\"1\"]', frame_id)
                time.sleep(0.2)
                self.log.info(f"[JSON] smart_form: radio/chip select '{select}' → CDP-clicked")
                return True
            # Failed — if this was a radio element, we're done. For other elements, fall through to select.

        # --- SELECT / COMBOBOX ---
        if select:
            # Random select: open dropdown → randomly pick a visible option
            if select == '__random__':
                if tag == 'SELECT':
                    random_js = (
                        f"(function(){{var e=document.querySelector('{esc}');if(!e)return'fail';"
                        f"var opts=e.querySelectorAll('option');var vis=[];"
                        f"for(var i=0;i<opts.length;i++){{if(opts[i].value&&opts[i].textContent.trim())vis.push(opts[i]);}}"
                        f"if(!vis.length)return'fail';var pick=vis[Math.floor(Math.random()*vis.length)];"
                        f"e.value=pick.value;e.dispatchEvent(new Event('change',{{bubbles:true}}));"
                        f"return JSON.stringify({{value:pick.value,text:pick.textContent.trim()}});}})()"
                    )
                    raw = self.cdp.eval(random_js, frame_id)
                    self.log.info(f"[JSON] smart_form: random select → {str(raw)[:120]}")
                    return 'fail' not in str(raw)
                else:
                    # Custom select: click to open, randomly pick visible option
                    self.cdp.click(selector, frame_id)
                    time.sleep(0.5)
                    random_js = (
                        f"(function(){{var opts=document.querySelectorAll("
                        f"'[role=option],[class*=select__option],[class*=-option],.css-select__option,.MuiMenuItem-root');"
                        f"var vis=[];for(var i=0;i<opts.length;i++){{if(opts[i].offsetWidth>0)vis.push(opts[i]);}}"
                        f"if(!vis.length)return'fail';var pick=vis[Math.floor(Math.random()*vis.length)];"
                        f"pick.click();return JSON.stringify({{text:pick.textContent.trim()}});}})()"
                    )
                    raw = self.cdp.eval(random_js, frame_id)
                    self.log.info(f"[JSON] smart_form: random custom select → {str(raw)[:120]}")
                    time.sleep(0.3)
                    return 'fail' not in str(raw)

            if tag == 'SELECT':
                self.cdp.form(selector, select=select, frame_id=frame_id)
                return True
            else:
                # Ant Design / custom select: click trigger, find option, click, verify
                esc_val = select.replace("'", "\\'")
                ant_js = (
                    f"(function(){{var root=document.querySelector('{esc}');if(!root)return'no root';"
                    f"var trigger=root.matches('.ant-select-selector')?root:root.querySelector('.ant-select-selector');"
                    f"if(!trigger)trigger=root;trigger.click();"
                    f"var t0=Date.now();var found=null;"
                    f"function scan(){{"
                    f"var opts=document.querySelectorAll('.ant-select-item,.ant-select-item-option,[role=option],[class*=select__option]');"
                    f"for(var i=0;i<opts.length;i++){{if(opts[i].textContent.trim().indexOf('{esc_val}')!==-1&&opts[i].offsetWidth>0){{found=opts[i];return true;}}}}"
                    f"return false;}}"
                    f"if(scan()){{found.click();return'clicked';}}"
                    f"var iv=setInterval(function(){{if(scan()||Date.now()-t0>2000){{clearInterval(iv);if(found){{found.click();setTimeout(function(){{return'clicked';}},100);}}}}}},100);"
                    f"setTimeout(function(){{clearInterval(iv);if(found)found&&found.click();return found?'clicked':'not found';}},1500);}})()"
                )
                result = self.cdp.eval(ant_js, frame_id)
                if "clicked" in str(result):
                    time.sleep(0.3)
                    # Verify selection: check if display text or hidden input matches
                    verify_js = (
                        f"(function(){{var root=document.querySelector('{esc}');if(!root)return'no root';"
                        f"var formItem=root.closest('[class*=form-item],.ant-form-item,fieldset');"
                        f"var display=formItem?formItem.querySelector('[class*=selection-item],[class*=select-selection],[class*=display],[id*=display]'):null;"
                        f"if(display)return display.textContent.trim();"
                        f"var sel=formItem?formItem.querySelector('input[type=hidden]'):null;"
                        f"return sel?sel.value:'no display';}})()"
                    )
                    verify = self.cdp.eval(verify_js, frame_id)
                    self.log.info(f"[JSON] smart_form: ant select '{select}' → verify: {str(verify).strip()}")
                    if str(verify).strip() == select:
                        return True

                # Fall through: try CDP natively
                result = self.cdp.form(selector, select=select, frame_id=frame_id)
                if 'Error' not in result and 'error' not in result.lower():
                    self.log.info(f"[JSON] smart_form: cdp.form custom select OK")
                    return True
                self.log.info(f"[JSON] smart_form: cdp.form failed ({result.strip()[:100]}), trying wrapper")
                # Try parent wrapper (React Select: hidden input is inside a wrapper div with id)
                parent_js = (
                    f"(function(){{var e=document.querySelector('{esc}');if(!e)return'';"
                    f"var p=e.parentElement;var best='';"
                    f"while(p&&p!==document.body){{"
                    f"if(p.id&&p.offsetWidth>0){{best='#'+p.id;"
                    f"if(p.id.indexOf('wrapper')!==-1||p.id.indexOf('Wrapper')!==-1)return best;}}"
                    f"p=p.parentElement;}}"
                    f"return best;}})()"
                )
                parent_sel = self.cdp.eval(parent_js, frame_id)
                parent_sel = (parent_sel or '').strip().strip('"')
                if parent_sel and not parent_sel.startswith('Error'):
                    self.log.info(f"[JSON] smart_form: retrying with wrapper {parent_sel}")
                    result2 = self.cdp.form(parent_sel, select=select, frame_id=frame_id)
                    if 'Error' not in result2 and 'error' not in result2.lower():
                        self.log.info(f"[JSON] smart_form: cdp.form wrapper OK")
                        return True
                # Fallback: manual click to open, then click option
                trigger = None
                find_js = (
                    f"(function(){{var e=document.querySelector('{esc}');"
                    # Check if element itself is the combobox
                    f"if(e&&e.getAttribute('role')==='combobox'&&e.offsetWidth>0){{e.setAttribute('data-cb-trigger','1');return'ref';}}"
                    # First, try closest ancestor with role=combobox
                    f"var cb=e?e.closest('[role=combobox]'):null;"
                    f"if(cb&&cb.getAttribute('data-cb-trigger')!=='1'&&cb.offsetWidth>0){{cb.setAttribute('data-cb-trigger','1');return'ref';}}"
                    # Try prev/next sibling combobox
                    f"var sib=e?e.previousElementSibling:null;"
                    f"if(sib&&sib.getAttribute('role')==='combobox'&&sib.offsetWidth>0){{sib.setAttribute('data-cb-trigger','1');return'ref';}}"
                    f"var nsib=e?e.nextElementSibling:null;"
                    f"if(nsib&&nsib.getAttribute('role')==='combobox'&&nsib.offsetWidth>0){{nsib.setAttribute('data-cb-trigger','1');return'ref';}}"
                    # Fallback: any combobox on the page near the label text
                    f"return'none';}})()"
                )
                result = self.cdp.eval(find_js, frame_id)
                result = (result or '').strip().strip('"')
                if result == 'ref':
                    trigger = '[data-cb-trigger="1"]'
                    self.log.info(f"[JSON] smart_form: found combobox trigger for custom select")
                else:
                    # Last resort: click the element itself
                    trigger = selector
                    self.log.info(f"[JSON] smart_form: no combobox found, clicking selector directly")
                self.cdp.click(trigger, frame_id)
                time.sleep(0.6)
                esc_val = select.replace("'", "\\'")
                # Search for dropdown options (many different implementations)
                opt_js = (
                    f"(function(){{var opts=document.querySelectorAll("
                    f"'[role=option],[role=listbox] li,[role=listbox] div,"
                    f"ul[role=listbox] li,div[role=presentation] li,"
                    f".MuiMenuItem-root,.MuiAutocomplete-option,"
                    f"li[data-value],div[data-option-index],"
                    f"[class*=select__option],[class*=-option],.css-select__option');"
                    f"for(var i=0;i<opts.length;i++){{"
                    f"if(opts[i].textContent.trim().indexOf('{esc_val}')!==-1&&opts[i].offsetWidth>0){{"
                    f"opts[i].click();return'clicked';}}}}"
                    f"return'not found';}})()"
                )
                result = self.cdp.eval(opt_js, frame_id)
                self.log.info(f"[JSON] smart_form: custom select option click → {result.strip()}")
                # Close any open dropdown menus (multi-select doesn't auto-close)
                close_js = (
                    f"(function(){{"
                    f"document.querySelectorAll('[class*=menu],[class*=dropdown],[class*=popup]').forEach("
                    f"function(m){{m.classList.remove('css-select__menu--open');}});"
                    f"}})()"
                )
                self.cdp.eval(close_js, frame_id)
                time.sleep(0.3)
                return True

        # --- RANGE / SLIDER ---
        if etype == 'range' or role == 'slider':
            if value:
                esc_val = value.replace("'", "\\'")
                self.cdp.eval(
                    f"(function(){{var e=document.querySelector('{esc}');if(e){{"
                    f"e.value={esc_val};e.dispatchEvent(new Event('input',{{bubbles:true}}));"
                    f"e.dispatchEvent(new Event('change',{{bubbles:true}}));}}}})()",
                    frame_id)
                return True

        # --- CHECKBOX / RADIO / TOGGLE ---
        if check is not None or etype in ('checkbox', 'radio') or role in ('checkbox', 'radio', 'switch'):
            # For native checkboxes, use cdp.form; for custom, click
            if etype in ('checkbox', 'radio'):
                self.cdp.form(selector, check="true" if check != "false" else "false",
                             frame_id=frame_id)
            else:
                self.cdp.click(selector, frame_id)
            return True

        # --- CONTENTEDITABLE ---
        if is_ce:
            if value:
                esc_val = value.replace("'", "\\'")
                self.cdp.eval(
                    f"(function(){{var e=document.querySelector('{esc}');if(e){{"
                    f"e.textContent='{esc_val}';e.dispatchEvent(new Event('input',{{bubbles:true}}));}}}})()",
                    frame_id)
                return True

        # --- TEXT INPUT (default) ---
        self.cdp.form(selector, value=value, check=check, frame_id=frame_id)
        return True

    def _check_success(self) -> bool:
        """Check all success conditions. Returns True if any match."""
        succ = self.config.get("success", {})
        if not succ:
            return False

        info = self.cdp.get_page_info()
        url = info.get("url", "")
        body = self.cdp.eval(
            "(function(){return document.body?document.body.innerText.substring(0,2000):'';})()",
            self._frame_id
        )

        conditions = succ if isinstance(succ, list) else [succ]
        # Support both {"url_contains": [...]} and {"any": [{...}, {...}]}
        if "any" in succ:
            conditions = succ["any"]
        elif isinstance(succ, dict) and "any" not in succ:
            conditions = [succ]

        for cond in conditions:
            if self._match_condition(cond, url, body):
                return True
        return False

    def _match_condition(self, cond: dict, url: str, body: str) -> bool:
        """Check a single success condition."""
        if "url_contains" in cond:
            patterns = cond["url_contains"]
            if not isinstance(patterns, list):
                patterns = [patterns]
            if any(p in url for p in patterns):
                return True
        body_key = "body_contains" if "body_contains" in cond else ("text_contains" if "text_contains" in cond else None)
        if body_key:
            patterns = cond[body_key]
            if not isinstance(patterns, list):
                patterns = [patterns]
            bl = body.lower()
            if any(p.lower() in bl for p in patterns):
                return True
        if "element_visible" in cond:
            selector = self.finder.find(cond["element_visible"])
            if selector:
                # Actually check visibility via CDP
                esc = selector.replace("'", "\\'")
                vis = self.cdp.eval(
                    f"(function(){{var e=document.querySelector('{esc}');"
                    f"return e&&e.offsetWidth>0?'yes':'no';}})()",
                    self._frame_id)
                if "yes" in vis:
                    return True
        return False

    def _run_pre(self):
        """Execute pre-step dismissal (cookies, popups)."""
        for step in self.config.get("pre", []):
            step = self.var.resolve_dict(step)
            self._execute_step(step)
