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
        max_rounds = self.config.get("max_rounds", 30)
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
                # Try advancing: click Next/Continue if visible
                self.log.info("[JSON] No matching steps, trying Next/Continue")
                for btn in ["Next", "Continue", "Submit"]:
                    js = (
                        f"(function(){{var bs=document.querySelectorAll('button');"
                        f"for(var i=0;i<bs.length;i++){{"
                        f"if(bs[i].textContent.trim()==='{btn}'&&bs[i].offsetWidth>0)"
                        f"{{bs[i].click();return'clicked';}}}}return'none';}})()"
                    )
                    r = self.cdp.eval(js, self._frame_id)
                    if "clicked" in r:
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
                loc = self.locator.locate(field, frame_hint=frame_hint)
                ok = bool(loc and loc.selector)
                self.log.info(f"[JSON] field_exists {field.get('label','?')}: {ok} sel={loc.selector if ok else 'NONE'}")
                return ok
            except LocatorError as e:
                self.log.info(f"[JSON] field_exists {field.get('label','?')}: LocatorError {e}")
                return False

        # body_contains
        if "body_contains" in cond:
            patterns = cond["body_contains"]
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
                f"var r=[];var els=root.querySelectorAll('button,a,label,li,[role=button],[role=option]');"
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
                      'Arbitration','Manage','Policy','Disclaimer']

        if stype == "random":
            # Randomly click any visible option-like element
            js = (
                f"var skip={json.dumps(skip_words)};"
                f"var r=[];var els=document.querySelectorAll('button,a,label,li,[role=button],[role=option]');"
                f"for(var i=0;i<els.length;i++){{"
                f"var e=els[i];var t=e.textContent.trim();"
                f"if(!e.offsetWidth||t.length<2||t.length>60)continue;"
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
                try: self.cdp.click('[data-sel="1"]', self._frame_id)
                except: pass
                self.cdp.eval("(function(){var e=document.querySelector('[data-sel]');if(e)e.removeAttribute('data-sel');})()", self._frame_id)
                return True
            return False

        elif stype == "match_text":
            target = strategy.get("value", "")
            fallback = strategy.get("fallback", "first")
            # Find element with matching text
            js = (
                f"var t='{target.replace(chr(39),chr(92)+chr(39))}';"
                f"var els=document.querySelectorAll('button,a,label,li,[role=button],[role=option]');"
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
                f"var els=document.querySelectorAll('button,a,label,li,[role=button],[role=option]');"
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
        frame_spec = step.get("frame")
        retry = step.get("retry", 1)
        optional = step.get("optional", False)

        # Resolve frame_id from frame spec (cached if same as last)
        if frame_spec:
            self._frame_id = self._resolve_frame(frame_spec)

        for attempt in range(retry):
            try:
                if action == "wait":
                    t = random.uniform(step.get("min", 0.3), step.get("max", 1.5))
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
                        try:
                            loc = self.locator.locate(field, frame_hint=frame_hint)
                            selector = loc.selector
                            if loc.frame_id:
                                self._frame_id = loc.frame_id
                            self.log.info(f"[JSON] click: located '{field}' via {loc.strategy} ({loc.confidence})")
                        except LocatorError as e:
                            self.log.warning(f"[JSON] click: cannot locate field: {e}")
                            if optional: return True
                            return False
                    else:
                        selector = self.finder.find(find, fctx)
                    if not selector:
                        if optional:
                            return True
                        self.log.warning(f"[JSON] click: element not found: {find or field}")
                        if attempt + 1 < retry:
                            time.sleep(random.uniform(2, 4))
                            continue
                        return False
                    self.cdp.click(selector, self._frame_id)
                    if "wait_after" in step:
                        time.sleep(random.uniform(step["wait_after"][0], step["wait_after"][1]))
                    return True

                elif action == "form":
                    find = step.get("find", {})
                    field = step.get("field")
                    fctx = {"frame_id": self._frame_id} if self._frame_id else None
                    if field:
                        frame_hint = field.get("frame_url", "")
                        try:
                            loc = self.locator.locate(field, frame_hint=frame_hint)
                            selector = loc.selector
                            if loc.frame_id:
                                self._frame_id = loc.frame_id
                            self.log.info(f"[JSON] form: located '{field}' via {loc.strategy} ({loc.confidence})")
                        except LocatorError as e:
                            self.log.warning(f"[JSON] form: cannot locate field: {e}")
                            return False
                    else:
                        selector = self.finder.find(find, fctx)
                    if not selector:
                        self.log.warning(f"[JSON] form: element not found: {find or field}")
                        return False
                    value = step.get("value")
                    check = step.get("check")
                    select = step.get("select")
                    if check is not None:
                        check = "true" if check else "false"
                    self.cdp.form(selector, value=value, check=check, select=select, frame_id=self._frame_id)
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
        if "body_contains" in cond:
            patterns = cond["body_contains"]
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
