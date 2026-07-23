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
                # Try advancing: click Next/Continue if visible (using CDP click)
                self.log.info("[JSON] No matching steps, trying Next/Continue")
                for btn in ["Next", "Continue", "Submit"]:
                    # Mark matching button then CDP click it
                    esc = btn.replace("'", "\\'")
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
                self.log.info(f"[JSON] field_exists {field.get('label','?')}: {ok} sel={loc.selector if ok else 'NONE'}")
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
                f"if(e.tagName==='INPUT'||e.tagName==='TEXTAREA'||e.tagName==='SELECT')continue;"
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
                      'Get','View','Submit','Continue','Next','See','Go','Check']

        if stype == "random":
            # Clean up any leftover markers from previous calls
            self.cdp.eval(
                "(function(){var e=document.querySelector('[data-sel]');if(e)e.removeAttribute('data-sel');})()",
                self._frame_id)
            # Randomly click any visible option-like element
            # Scope to container if specified, otherwise filter nav/CTA by position
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
                f"if(e.tagName==='INPUT'||e.tagName==='TEXTAREA'||e.tagName==='SELECT')continue;"
                # Exclude nav/header/footer elements (position-based, not class-based)
                f"if(e.closest('nav,header,footer'))continue;"
                f"var rect=e.getBoundingClientRect();"
                f"if(rect.top<50||rect.bottom>window.innerHeight-50)continue;"
                f"var bad=false;for(var s=0;s<skip.length;s++){{"
                f"if(t.indexOf(skip[s])!==-1){{bad=true;break;}}}}"
                f"if(!bad)r.push(i);}}"
                f"if(r.length>0){{"
                f"var pick=r[Math.floor(Math.random()*r.length)];"
                f"els[pick].setAttribute('data-sel','1');"
                f"return'clicked '+(r.length)+' opts';}}"
                f"return'none';"
            )
            result = self.cdp.eval(f"(function(){{{js}}})()", self._frame_id)
            if "clicked" in result:
                # JS .click() for onclick handlers (works for all element types)
                self.cdp.eval("(function(){var e=document.querySelector('[data-sel=\"1\"]');if(e)e.click();})()", self._frame_id)
                self.cdp.eval("(function(){var e=document.querySelector('[data-sel]');if(e)e.removeAttribute('data-sel');})()", self._frame_id)
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
        frame_hint = step.get("frame_url") or step.get("frame")
        if frame_hint:
            self._frame_id = self.locator._resolve_frame(frame_hint)
        # Also check field-level frame_url
        field = step.get("field", {}) or {}
        if not self._frame_id and field.get("frame_url"):
            self._frame_id = self.locator._resolve_frame(field["frame_url"])
        # Also check find-level
        find = step.get("find", {}) or {}
        if not self._frame_id and find.get("frame_url"):
            self._frame_id = self.locator._resolve_frame(find["frame_url"])

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

        # --- SELECT / COMBOBOX ---
        if select:
            if tag == 'SELECT':
                self.cdp.form(selector, select=select, frame_id=frame_id)
                return True
            else:
                # Custom select (MUI, React-Select, etc.): click to open, then click option
                # Always look for the visible combobox trigger — the native input may be visible
                # but clicking it won't open the dropdown
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
                    f"li[data-value],div[data-option-index]');"
                    f"for(var i=0;i<opts.length;i++){{"
                    f"if(opts[i].textContent.trim().indexOf('{esc_val}')!==-1&&opts[i].offsetWidth>0){{"
                    f"opts[i].click();return'clicked';}}}}"
                    f"return'not found';}})()"
                )
                result = self.cdp.eval(opt_js, frame_id)
                self.log.info(f"[JSON] smart_form: custom select option click → {result.strip()}")
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
