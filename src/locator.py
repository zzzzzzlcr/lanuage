"""Semantic field locator - resolves field descriptions to DOM elements at runtime.

Never stores selectors/id in JSON. Operates on semantic field descriptions:
  {"label": "email", "type": "email", "context": "registration form"}

Strategies (ordered by reliability, highest first):
  1. aria-label / data-testid / name attribute exact match
  2. label[for] pointing to input id
  3. Adjacent text node matching (find label text near input)
  4. Type-based matching (type=email → input[type=email])
  5. Placeholder-based matching
  6. AI visual/snapshot fallback (most expensive, last resort)
"""

import json, re, time, logging
from typing import Optional, List, Dict, Any, Tuple


class LocatorResult:
    """Result of a locate() call."""
    def __init__(self, selector: str, strategy: str, confidence: float,
                 frame_id: str = "", candidates: List[Dict] = None,
                 alternatives: List['LocatorResult'] = None):
        self.selector = selector
        self.strategy = strategy
        self.confidence = confidence  # 0.0 - 1.0
        self.frame_id = frame_id
        self.candidates = candidates or []
        self.alternatives = alternatives or []  # other matches found


class LocatorError(Exception):
    """Structured error when all strategies fail."""
    def __init__(self, field_desc: dict, attempts: List[dict]):
        self.field_desc = field_desc
        self.attempts = attempts
        msg = f"Locator failed for {field_desc}. Attempts: {len(attempts)}"
        super().__init__(msg)


class FieldLocator:
    """Runtime field locator. Resolves semantic descriptions → DOM selectors.

    Usage:
        locator = FieldLocator(cdp)
        result = locator.locate({"label": "email", "type": "email"})
        cdp.form(result.selector, value="test@test.com", frame_id=result.frame_id)
    """

    MIN_CONFIDENCE = 0.3  # Below this, strategy is discarded as noise

    def __init__(self, cdp, ai_client=None, log=None):
        self.cdp = cdp
        self.ai = ai_client
        self.log = log or logging.getLogger(__name__)
        self._cache: Dict[str, LocatorResult] = {}  # session-level cache

    # ==================================================================
    # Public API
    # ==================================================================

    def locate(self, field: dict, frame_hint: str = "",
               scope: dict = None) -> LocatorResult:
        """Resolve a semantic field description to a DOM selector.

        Args:
            field: {"label": "email", "type": "email", "context": "..."}
            frame_hint: URL substring to identify correct iframe if needed.
            scope: optional narrowing {"group_label": "同行人", "group_index": 1}

        Returns:
            LocatorResult with selector, strategy, confidence, alternatives.

        Raises:
            LocatorError if all strategies fail.
        """
        cache_key = json.dumps(field, sort_keys=True) + frame_hint + json.dumps(scope or {}, sort_keys=True)
        if cache_key in self._cache:
            self.log.info(f"[Locator] Cache hit: {field}")
            return self._cache[cache_key]

        # Resolve frame first if needed
        frame_id = ""
        if frame_hint:
            frame_id = self._resolve_frame(frame_hint)

        # If scope is provided, narrow search to that container first
        container_sel = ""
        if scope:
            container_sel = self._resolve_scope(scope, frame_id)
            if not container_sel:
                raise LocatorError(field, [{'strategy': 'scope', 'error': f'Scope not found: {scope}'}])

        # Collect ALL candidates across strategies (not just first match)
        all_candidates = self._find_all_candidates(field, frame_id, container_sel)

        # Auto-detect: if no candidates and no frame hint, search all iframes
        if not all_candidates and not frame_hint:
            iframe_ids = self._list_iframes()
            for fid in iframe_ids:
                candidates_in_frame = self._find_all_candidates(field, fid, container_sel)
                if candidates_in_frame:
                    frame_id = fid
                    all_candidates = candidates_in_frame
                    self.log.info(f"[Locator] auto-detected iframe {fid} for '{field.get('label','')}'")
                    break

        # Retry without type constraint (handles type="select" for non-select elements like rating stars)
        if not all_candidates and field.get('type'):
            field_no_type = {k: v for k, v in field.items() if k != 'type'}
            all_candidates = self._find_all_candidates(field_no_type, frame_id, container_sel)
            if all_candidates:
                self.log.info(f"[Locator] retry without type='{field['type']}' → found {len(all_candidates)} candidates")

        if not all_candidates:
            raise LocatorError(field, [{'strategy': 'all', 'error': 'No candidates found'}])

        self.log.debug(f"[Locator] {len(all_candidates)} raw candidates for '{field.get('label','')}': {[(c['strategy'],c['confidence']) for c in all_candidates]}")

        # Filter: discard candidates with invalid/broken selectors
        valid = []
        for c in all_candidates:
            sel = c.get('selector','')
            # Must be non-empty, contain actual selector chars
            if not sel or sel == '#' or sel.endswith('#]') or sel == '#]':
                continue
            if len(sel) < 3:
                continue
            # Verify it actually points to a visible element
            if self._visible(sel, frame_id):
                valid.append(c)
            else:
                self.log.warning(f"[Locator] Discarding invisible: {sel}")

        if not valid:
            raise LocatorError(field, [{'strategy': 'all', 'error': f'No valid candidates from {len(all_candidates)} raw candidates'}])

        # Single candidate → use directly
        if len(valid) == 1:
            c = valid[0]
            result = LocatorResult(c['selector'], c['strategy'], c['confidence'], frame_id,
                                   alternatives=[])
            self._cache[cache_key] = result
            return result

        # Multiple candidates → try to narrow by scope/index
        if scope and scope.get("group_index") is not None:
            idx = scope["group_index"]
            if 0 <= idx < len(valid):
                c = valid[idx]
                result = LocatorResult(c['selector'], f"{c['strategy']}_scoped", c['confidence'],
                                       frame_id, alternatives=valid)
                self._cache[cache_key] = result
                return result

        # Still multiple → try position-based disambiguation
        # Only apply to type_match candidates — they're indexed in DOM order.
        # Merged-list indexing is unsafe because candidates from different
        # strategies (aria-label, placeholder, etc.) interleave unpredictably.
        label = field.get("label", "").lower() or field.get("name", "").lower()
        if label:
            pos_map = {
                "first": 0, "first name": 0, "given": 0, "名": 0,
                "last": 1, "last name": 1, "family": 1, "surname": 1, "姓": 1,
                "address": 2, "addr": 2, "street": 2, "地址": 2,
                "city": 3, "城市": 3,
                "state": 4, "province": 4, "州": 4, "省": 4,
                "zip": 5, "postal": 5, "postcode": 5, "邮编": 5, "邮政编码": 5,
                "ssn": 6, "social": 6, "手机号": 0, "phone": 0, "tel": 0, "电话": 0,
                "day": 2, "dob day": 2, "日": 2,
                "month": -1, "dob month": -1, "月": -1,
                "year": 3, "dob year": 3, "年": 3,
            }
            for kw, pos in pos_map.items():
                if kw in label:
                    tm = [c for c in valid if c.get('strategy','').startswith('type_match')]
                    if tm and 0 <= pos < len(tm):
                        c = tm[pos]
                        result = LocatorResult(c['selector'], f"{c['strategy']}_position", c['confidence'],
                                               frame_id, alternatives=valid)
                        self._cache[cache_key] = result
                        return result

        # Still multiple → pick best confidence, keep alternatives for debugging
        best = max(valid, key=lambda c: (c['confidence'], -len(c.get('selector',''))))
        alts = [LocatorResult(c['selector'], c['strategy'], c['confidence'], frame_id)
                for c in valid if c != best]

        result = LocatorResult(best['selector'], best['strategy'], best['confidence'],
                               frame_id, alternatives=alts)
        if len(alts) > 0:
            self.log.warning(f"[Locator] Multiple candidates for {field}: "
                           f"picked {best['strategy']} (conf={best['confidence']}), "
                           f"{len(alts)} alternatives: {[a.selector for a in alts]}")
        self._cache[cache_key] = result
        return result

    def clear_cache(self):
        self._cache.clear()

    # ==================================================================
    # Scope resolution (semantic grouping)
    # ==================================================================

    def _resolve_scope(self, scope: dict, frame_id: str) -> str:
        """Find container element by semantic scope description.

        scope: {"group_label": "同行人", "group_index": 1}
        Returns CSS selector for the container, or empty string.
        """
        group_label = scope.get("group_label", "")
        if not group_label:
            return ""

        escaped = group_label.replace("'", "\\'")
        # Find all containers with matching heading/label text
        js = (
            f"(function(){{var headings=document.querySelectorAll('h1,h2,h3,h4,h5,"
            f"fieldset legend,div[class*=group],div[class*=section],div[class*=item]');"
            f"var r=[];for(var i=0;i<headings.length;i++){{"
            f"if(headings[i].textContent.indexOf('{escaped}')!==-1&&headings[i].offsetWidth>0)"
            f"{{r.push(i);}}}}"
            f"if(r.length>0){{"
            f"var idx={scope.get('group_index',0)};"
            f"if(idx>=r.length)idx=0;"
            f"headings[r[idx]].setAttribute('data-scope','1');return'found '+(r.length)+' groups';}}"
            f"return'none';}})()"
        )
        result = self.cdp.eval(js, frame_id)
        if "found" in result:
            return '[data-scope="1"]'
        return ""

    # ==================================================================
    # Multi-candidate collection
    # ==================================================================

    def _find_all_candidates(self, field: dict, frame_id: str, container: str) -> List[dict]:
        """Collect ALL matching candidates across all strategies. Never return just one."""
        candidates = []

        # Strategy 1: exact attribute match
        for r in self._candidates_exact_attrs(field, frame_id, container):
            candidates.append(r)

        # Strategy 2: label[for]
        for r in self._candidates_label_for(field, frame_id, container):
            candidates.append(r)

        # Strategy 3: type match
        for r in self._candidates_type_match(field, frame_id, container):
            candidates.append(r)

        # Strategy 4: placeholder
        for r in self._candidates_placeholder(field, frame_id, container):
            candidates.append(r)

        # Strategy 5: adjacent text (heading, label, parent text near the element)
        for r in self._candidates_adjacent_text(field, frame_id, container):
            candidates.append(r)

        # Strategy 6: custom select (Ant Design, React Select) — label → form-item → trigger
        for r in self._candidates_custom_select(field, frame_id, container):
            candidates.append(r)

        return candidates

    def _candidates_exact_attrs(self, field, frame_id, container) -> List[dict]:
        """Find all inputs matching name/aria-label/data-testid."""
        name = field.get("label", "") or field.get("name", "") or field.get("id", "")
        if not name: return []
        results = []
        root = container or "document"
        esc = name.replace('"', '\\"')
        for attr in ['id', 'name', 'aria-label', 'data-testid']:
            js = (
                f"(function(){{var r=[];var els=document.querySelectorAll('input[{attr}*=\"{esc}\" i],select[{attr}*=\"{esc}\" i],textarea[{attr}*=\"{esc}\" i]');"
                f"for(var i=0;i<els.length;i++){{if((els[i].tagName!=='INPUT'||els[i].tabIndex!==-1)&&(els[i].offsetWidth>0||els[i].placeholder||els[i].name))r.push(i);}}"
                f"return JSON.stringify(r);}})()"
            )
            raw = self.cdp.eval(js, frame_id)
            try:
                parsed = json.loads(raw) if isinstance(raw, str) else raw
                if isinstance(parsed, str):
                    parsed = json.loads(parsed)
                indices = parsed
                if attr == 'id':
                    # ID should be unique — use bare selector, nth-of-type is meaningless for mixed tags
                    selector = f'[{attr}*="{esc}" i]'
                    results.append({'selector': selector, 'strategy': attr, 'confidence': 0.85})
                else:
                    for idx in indices:
                        selector = f'[{attr}*="{esc}" i]:nth-of-type({idx+1})' if len(indices) > 1 else f'[{attr}*="{esc}" i]'
                        results.append({'selector': selector, 'strategy': attr,
                                        'confidence': 1.0 if attr == 'name' else 0.85})
            except: pass
        return results

    def _candidates_label_for(self, field, frame_id, container) -> List[dict]:
        """Find all inputs with label[for] pointing to them."""
        label_text = field.get("label", "")
        if not label_text: return []
        esc = label_text.replace("'", "\\'")
        js = (
            f"(function(){{var r=[];var labels=document.querySelectorAll('label');"
            f"for(var i=0;i<labels.length;i++){{"
            f"if(labels[i].textContent.toLowerCase().indexOf('{esc.lower()}')!==-1&&labels[i].htmlFor){{"
            f"var inp=document.getElementById(labels[i].htmlFor);"
            f"if(inp&&inp.id&&(inp.offsetWidth>0||inp.type==='checkbox'||inp.type==='radio'||inp.placeholder||inp.name))r.push(inp.id);}}}}"
            f"return JSON.stringify(r);}})()"
        )
        raw = self.cdp.eval(js, frame_id)
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(parsed, str):
                parsed = json.loads(parsed)
            results = []
            for id in parsed:
                if id and id.strip():
                    results.append({'selector': f'#{id}', 'strategy': 'label_for', 'confidence': 0.95})
            return results
        except: return []

    def _candidates_type_match(self, field, frame_id, container) -> List[dict]:
        """Find all inputs matching type (email/tel/password).
        When multiple matches, generate indexed selectors + try label proximity.
        """
        ftype = field.get("type", "").lower()
        if not ftype: return []
        if ftype == 'textarea':
            selector = 'textarea'
        elif ftype == 'select':
            selector = 'select'
        elif ftype == 'rating':
            selector = '[class*=rating],[class*=-star]'
        else:
            selector = f'input[type="{ftype}"]'
            # textarea has no type attribute — include it when searching for text fields
            if ftype == 'text':
                selector += ',textarea'
        # Count how many visible matches
        js = (
            f"(function(){{var els=document.querySelectorAll('{selector}');var c=0;"
            f"for(var i=0;i<els.length;i++){{if(els[i].tabIndex!==-1&&(els[i].offsetWidth>0||els[i].placeholder||els[i].name))c++;}}"
            f"return c;}})()"
        )
        raw = self.cdp.eval(js, frame_id)
        try: count = int(raw) if isinstance(raw, (int, float)) else int(raw.strip().strip('"'))
        except: count = 0

        if count == 0:
            # Fallback: search by id or placeholder containing the type name
            fallback_sel = f'input[id*="{ftype}"],input[placeholder*="{ftype}"]'
            js2 = (
                f"(function(){{var els=document.querySelectorAll('{fallback_sel}');var c=0;"
                f"for(var i=0;i<els.length;i++){{if(els[i].offsetWidth>0||els[i].placeholder||els[i].name||els[i].id)c++;}}"
                f"return c;}})()"
            )
            raw2 = self.cdp.eval(js2, frame_id)
            try: count = int(raw2) if isinstance(raw2, (int, float)) else int(raw2.strip().strip('"'))
            except: count = 0
            if count == 0:
                return []
            # Use the fallback selector for the rest
            selector = fallback_sel
            return [{'selector': selector, 'strategy': 'type_match_fallback', 'confidence': 0.75}]
        if count == 1:
            return [{'selector': selector, 'strategy': 'type_match', 'confidence': 0.8}]

        # Multiple matches — mark each visible input and return indexed selectors
        label = field.get("label", "").lower()
        marker = f"tpm{hash(ftype)%10000}"
        js_mark = (
            f"(function(){{var els=document.querySelectorAll('{selector}');var n=0;"
            f"for(var i=0;i<els.length;i++){{if(els[i].tabIndex!==-1&&(els[i].offsetWidth>0||els[i].placeholder||els[i].name)){{"
            f"els[i].setAttribute('data-{marker}',String(n));n++;}}}}"
            f"return n;}})()"
        )
        raw = self.cdp.eval(js_mark, frame_id)
        try: actual_count = int(raw) if isinstance(raw, (int, float)) else int(raw.strip().strip('"'))
        except: actual_count = count

        results = []
        for n in range(actual_count):
            idx_sel = f'[data-{marker}=\"{n}\"]'
            conf = 0.8
            if label:
                esc = label.replace("'", "\\'")
                js_prox = (
                    f"(function(){{var e=document.querySelector('[data-{marker}=\"{n}\"]');if(!e)return'0';"
                    f"var p=e.parentElement;if(p&&p.textContent.toLowerCase().indexOf('{esc}')!==-1)return'1';"
                    f"var prev=e.previousElementSibling;if(prev&&prev.textContent.toLowerCase().indexOf('{esc}')!==-1)return'1';"
                    f"return'0';}})()"
                )
                prox_raw = self.cdp.eval(js_prox, frame_id)
                if '1' in prox_raw:
                    conf = 0.85
            results.append({'selector': idx_sel, 'strategy': 'type_match', 'confidence': conf})
        return results

    def _candidates_placeholder(self, field, frame_id, container) -> List[dict]:
        """Find all inputs matching placeholder text."""
        label = field.get("label", "")
        if not label: return []
        esc = label.replace("'", "\\'")
        js = (
            f"(function(){{var r=[];var ins=document.querySelectorAll('input[placeholder]');"
            f"for(var i=0;i<ins.length;i++){{"
            f"if(ins[i].placeholder.toLowerCase().indexOf('{esc.lower()}')!==-1&&(ins[i].offsetWidth>0||ins[i].placeholder||ins[i].name))"
            f"r.push(i);}}return JSON.stringify(r);}})()"
        )
        raw = self.cdp.eval(js, frame_id)
        try:
            indices = json.loads(raw) if isinstance(raw, str) else raw
            return [{'selector': f'input[placeholder*="{esc}" i]', 'strategy': 'placeholder',
                     'confidence': 0.85} for _ in indices]
        except: return []

    def _candidates_adjacent_text(self, field, frame_id, container) -> List[dict]:
        """Find inputs/selects near matching text (parent, sibling, heading).

        Confidence varies by match specificity:
        - sibling match: 0.7 (most specific — label/h2 directly before element)
        - heading match: 0.65 (h1-h4 in same container)
        - parent match: 0.5 (broad — parent text may contain multiple elements' labels)
        """
        label = field.get("label", "")
        if not label: return []
        esc = label.replace("'", "\\'")
        marker = f"at{abs(hash(label))%100000}"
        # JS returns JSON array of {idx, src} where src="s"|"p"|"h" (sibling/parent/heading)
        js = (
            f"(function(){{var ins=document.querySelectorAll('input,select,textarea,[role=combobox],[role=listbox],[role=checkbox],[role=radio],[role=switch],[role=slider],[contenteditable=true],[class*=select__control],[class*=select__wrapper],[id$=-wrapper],button,[class*=rating]');"

            f"var n=0;var seen=new Set();var results=[];"
            f"for(var i=0;i<ins.length;i++){{var e=ins[i];"
            # Allow offsetWidth=0 for: checkboxes, radios, and inputs with placeholder/name (SPA hidden steps)
            f"var isCheck=(e.type==='checkbox'||e.type==='radio');"
            f"var hasAttr=e.placeholder||e.name||e.getAttribute('aria-label')||e.getAttribute('data-testid');"
            f"if((!e.offsetWidth&&!isCheck&&!hasAttr)||seen.has(e))continue;"
            # Check previous sibling (only if sibling text is short — label-like, not a whole wrapper)
            f"var s=e.previousElementSibling;"
            f"if(s&&s.textContent.length<150&&s.textContent.toLowerCase().indexOf('{esc.lower()}')!==-1){{"
            f"e.setAttribute('data-{marker}',String(n));results.push({{i:n,src:'s'}});n++;seen.add(e);continue;}}"
            # Check next sibling (common for checkbox labels after input)
            f"var ns=e.nextElementSibling;"
            f"if(ns&&ns.textContent.length<150&&ns.textContent.toLowerCase().indexOf('{esc.lower()}')!==-1){{"
            f"e.setAttribute('data-{marker}',String(n));results.push({{i:n,src:'s'}});n++;seen.add(e);continue;}}"
            # Check parent text
            f"var p=e.parentElement;"
            f"if(p&&p.textContent&&p.textContent.length<1500&&p.textContent.toLowerCase().indexOf('{esc.lower()}')!==-1){{"
            f"e.setAttribute('data-{marker}',String(n));results.push({{i:n,src:'p'}});n++;seen.add(e);continue;}}"
            # Check nearby heading (h1-h4) in same container
            f"var h=e.closest('div,section,fieldset,main,form');"
            f"if(h){{var hd=h.querySelector('h1,h2,h3,h4');"
            f"if(hd&&hd.textContent.toLowerCase().indexOf('{esc.lower()}')!==-1){{"
            f"e.setAttribute('data-{marker}',String(n));results.push({{i:n,src:'h'}});n++;seen.add(e);continue;}}}}"
            # Check ancestor chain (up 4 levels) for label text (MUI: label outside input's subtree)
            # Skip ancestors that contain multiple visible inputs — too broad
            f"var a=e;"
            f"for(var lv=0;lv<4;lv++){{a=a.parentElement;if(!a)break;"
            f"if(a.textContent&&a.textContent.length<500&&a.textContent.toLowerCase().indexOf('{esc.lower()}')!==-1){{"
            f"var ac=0;var ais=a.querySelectorAll('input:not([type=hidden]),select,textarea,[role=combobox]');"
            f"for(var ai=0;ai<ais.length;ai++){{if(ais[ai].offsetWidth>0)ac++;}}"
            f"if(ac<=1){{e.setAttribute('data-{marker}',String(n));results.push({{i:n,src:'a'}});n++;seen.add(e);break;}}"
            f"}}}}"
            f"}}return JSON.stringify(results);}})()"
        )
        raw = self.cdp.eval(js, frame_id)
        try:
            results = json.loads(raw) if isinstance(raw, str) else raw
            conf_map = {'s': 0.7, 'h': 0.65, 'a': 0.55, 'p': 0.5}
            return [{'selector': f'[data-{marker}="{r["i"]}"]', 'strategy': 'adjacent_text',
                     'confidence': conf_map.get(r['src'], 0.6)} for r in results]
        except:
            return []

    def _candidates_custom_select(self, field, frame_id, container) -> List[dict]:
        """Find custom select triggers (Ant Design, React Select) by label→form-item→trigger."""
        label = field.get("label", "")
        if not label: return []
        esc = label.replace("'", "\\'")
        js = (
            f"(function(){{"
            f"var labels=document.querySelectorAll('label');"
            f"for(var i=0;i<labels.length;i++){{"
            f"if(labels[i].textContent.trim().toLowerCase().indexOf('{esc.lower()}')!==-1&&labels[i].offsetWidth>0){{"
            f"var item=labels[i].closest('.ant-form-item,[class*=form-item],fieldset');"
            f"if(!item)item=labels[i].parentElement;"
            f"var trigger=item.querySelector('.ant-select-selector,[role=combobox],select,[class*=select__control]');"
            f"if(trigger&&trigger.offsetWidth>0){{trigger.setAttribute('data-custom-trigger','1');return'trigger';}}"
            f"}}}}"
            f"return'none';}})()"
        )
        raw = self.cdp.eval(js, frame_id)
        if "trigger" in str(raw):
            return [{'selector': '[data-custom-trigger="1"]', 'strategy': 'custom_select', 'confidence': 0.9}]
        return []

    def _list_iframes(self) -> List[str]:
        """Return list of frame IDs for all iframes on the page."""
        try:
            snap = self.cdp.snapshot()
            data = json.loads(snap) if isinstance(snap, str) else snap
            frames = []
            for cf in data.get('childFrames', []):
                fid = cf.get('frame', {}).get('frameId', '')
                if fid:
                    frames.append(fid)
            return frames
        except Exception:
            return []

    def _resolve_frame(self, hint: str) -> str:
        """Find iframe by URL substring."""
        try:
            snap = self.cdp.snapshot()
            data = json.loads(snap) if isinstance(snap, str) else snap
        except Exception:
            return ""
        for cf in data.get("childFrames", []):
            url = cf.get("frame", {}).get("url", "")
            if hint in url:
                return cf.get("frame", {}).get("frameId", "")
        return ""

    def _try_exact_attrs(self, field: dict, frame_id: str) -> Optional[LocatorResult]:
        """Match by aria-label, data-testid, or name attribute."""
        name = field.get("label", "") or field.get("name", "")
        if not name:
            return None

        candidates = []

        # Try name attribute
        escaped = name.replace('"', '\\"')
        selector = f'[name="{escaped}"]'
        if self._visible(selector, frame_id):
            return LocatorResult(selector, "name_attr", 1.0, frame_id,
                                candidates)

        # Try aria-label
        selector = f'[aria-label*="{escaped}"]'
        if self._visible(selector, frame_id):
            return LocatorResult(selector, "aria_label", 0.9, frame_id,
                                candidates)

        # Try data-testid
        selector = f'[data-testid*="{escaped}"]'
        if self._visible(selector, frame_id):
            return LocatorResult(selector, "data_testid", 0.85, frame_id,
                                candidates)

        # Collect nearby candidates for debugging
        js = (
            f"(function(){{var r=[];var ins=document.querySelectorAll('input,select,textarea');"
            f"for(var i=0;i<ins.length;i++){{var e=ins[i];if(!e.offsetWidth)continue;"
            f"r.push({{n:e.name||'',id:e.id||'',aria:e.getAttribute('aria-label')||''}});}}"
            f"return JSON.stringify(r);}})()"
        )
        raw = self.cdp.eval(js, frame_id)
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
            candidates = parsed
        except Exception:
            pass

        return LocatorResult("", "exact_attrs", 0.0, frame_id, candidates)

    def _try_label_for(self, field: dict, frame_id: str) -> Optional[LocatorResult]:
        """Find <label for='xxx'> pointing to an input."""
        label_text = field.get("label", "")
        if not label_text:
            return None

        escaped = label_text.replace("'", "\\'")
        js = (
            f"(function(){{var labels=document.querySelectorAll('label');"
            f"for(var i=0;i<labels.length;i++){{"
            f"var t=labels[i].textContent.trim().toLowerCase();"
            f"if(t.indexOf('{escaped.lower()}')!==-1&&labels[i].htmlFor){{"
            f"var inp=document.getElementById(labels[i].htmlFor);"
            f"if(inp&&inp.offsetWidth>0){{inp.setAttribute('data-l4f','1');return'found';}}}}"
            f"return'none';}})()"
        )
        result = self.cdp.eval(js, frame_id)
        if "found" in result:
            # Cleanup
            self.cdp.eval(
                "(function(){var e=document.querySelector('[data-l4f]');"
                "if(e)e.removeAttribute('data-l4f');})()", frame_id)
            return LocatorResult(f'[data-l4f="1"]', "label_for", 0.95, frame_id)

        return None

    def _try_type_match(self, field: dict, frame_id: str) -> Optional[LocatorResult]:
        """Match by HTML input type (email, tel, password, etc.)."""
        ftype = field.get("type", "").lower()
        if not ftype:
            # Infer from label
            label = field.get("label", "").lower()
            if "email" in label or "邮箱" in label:
                ftype = "email"
            elif "phone" in label or "手机" in label or "电话" in label:
                ftype = "tel"
            elif "password" in label or "密码" in label:
                ftype = "password"
            elif "name" in label or "姓名" in label or "名字" in label:
                ftype = "text"
            else:
                return None

        selector = f'input[type="{ftype}"]'
        if self._visible(selector, frame_id):
            return LocatorResult(selector, "type_match", 0.8, frame_id)
        return None

    def _try_placeholder(self, field: dict, frame_id: str) -> Optional[LocatorResult]:
        """Match by placeholder text similarity."""
        label = field.get("label", "")
        if not label:
            return None

        escaped = label.replace("'", "\\'")
        js = (
            f"(function(){{var ins=document.querySelectorAll('input[placeholder]');"
            f"for(var i=0;i<ins.length;i++){{"
            f"var p=ins[i].placeholder.toLowerCase();"
            f"var l='{escaped.lower()}';"
            f"if(p.indexOf(l)!==-1||l.indexOf(p)!==-1){{"
            f"if(ins[i].offsetWidth>0){{ins[i].setAttribute('data-ph','1');return'found';}}}}"
            f"return'none';}})()"
        )
        result = self.cdp.eval(js, frame_id)
        if "found" in result:
            self.cdp.eval(
                "(function(){var e=document.querySelector('[data-ph]');"
                "if(e)e.removeAttribute('data-ph');})()", frame_id)
            return LocatorResult('[data-ph="1"]', "placeholder", 0.7, frame_id)
        return None

    def _try_adjacent_text(self, field: dict, frame_id: str) -> Optional[LocatorResult]:
        """Find input near matching text (sibling label, parent text)."""
        label = field.get("label", "")
        if not label:
            return None

        escaped = label.replace("'", "\\'")
        js = (
            f"(function(){{var ins=document.querySelectorAll('input,select,textarea,[role=combobox],[role=listbox],[role=checkbox],[role=radio],[role=switch],[role=slider],[contenteditable=true],[class*=select__control],[class*=select__wrapper],[id$=-wrapper],button,[class*=rating]');"

            f"for(var i=0;i<ins.length;i++){{"
            f"var e=ins[i];if(!e.offsetWidth)continue;"
            # Check parent text
            f"var p=e.parentElement;"
            f"if(p&&p.textContent.toLowerCase().indexOf('{escaped.lower()}')!==-1){{"
            f"e.setAttribute('data-adj','1');return'found';}}"
            # Check previous sibling
            f"var s=e.previousElementSibling;"
            f"if(s&&s.textContent.toLowerCase().indexOf('{escaped.lower()}')!==-1){{"
            f"e.setAttribute('data-adj','1');return'found';}}"
            f"}}"
            f"return'none';}})()"
        )
        result = self.cdp.eval(js, frame_id)
        if "found" in result:
            self.cdp.eval(
                "(function(){var e=document.querySelector('[data-adj]');"
                "if(e)e.removeAttribute('data-adj');})()", frame_id)
            return LocatorResult('[data-adj="1"]', "adjacent_text", 0.6, frame_id)
        return None

    def _try_ai_fallback(self, field: dict, frame_id: str) -> Optional[LocatorResult]:
        """AI visual/snapshot fallback — most expensive, last resort."""
        if not self.ai:
            return None
        try:
            # Use CDP snapshot + visible text as input (cheaper than screenshot)
            snap = self.cdp.snapshot()
            data = json.loads(snap) if isinstance(snap, str) else snap

            # Extract visible inputs from snapshot
            def walk(node, depth=0):
                if depth > 30: return []
                results = []
                tag = node.get('tag','')
                attr = node.get('attr',{})
                children = node.get('children',[])
                text = node.get('text','')[:80]
                if tag in ('INPUT','SELECT','TEXTAREA'):
                    results.append({
                        'tag':tag,'id':attr.get('id',''),'name':attr.get('name',''),
                        'type':attr.get('type',''),'placeholder':attr.get('placeholder',''),
                        'aria':attr.get('aria-label',''),'near_text':text
                    })
                for c in children:
                    results.extend(walk(c, depth+1))
                return results

            inputs = walk(data.get('frame',{}).get('body',{}))

            prompt = (
                f"Find the input matching: {json.dumps(field)}\n"
                f"Available inputs: {json.dumps(inputs[:20], ensure_ascii=False)}\n"
                f"Return only the index number (0-based) of the best match, or -1 if none."
            )
            response = self.ai.chat.completions.create(
                model="deepseek-v4-flash",
                messages=[{'role':'user','content':prompt}],
                temperature=0, max_tokens=10
            )
            idx = int(response.choices[0].message.content.strip())
            if 0 <= idx < len(inputs):
                inp = inputs[idx]
                sel = f'[name="{inp["name"]}"]' if inp['name'] else f'#{inp["id"]}'
                if inp['name'] or inp['id']:
                    return LocatorResult(sel, "ai_snapshot", 0.5, frame_id)
        except Exception as e:
            self.log.warning(f"[Locator] AI fallback failed: {e}")
        return None

    def _visible(self, selector: str, frame_id: str) -> bool:
        """Check if element exists and is visible. Accepts hidden form controls
        (custom widgets, MUI native inputs) that have semantic attributes."""
        esc = selector.replace("'", "\\'")
        js = (
            f"(function(){{var e=document.querySelector('{esc}');if(!e)return'no';"
            # Visible: offsetWidth > 0
            f"if(e.offsetWidth>0)return'yes';"
            # Hidden but semantically meaningful — check ancestors aren't display:none
            f"if(e.name||e.id||e.placeholder||e.getAttribute('data-testid')||e.getAttribute('aria-label')||e.getAttribute('data-value')){{"
            f"var p=e;for(var lv=0;lv<5;lv++){{p=p.parentElement;if(!p)break;"
            f"if(p.style&&p.style.display==='none')return'no';"
            f"var cs=window.getComputedStyle(p);if(cs&&cs.display==='none')return'no';}}}}"
            # Has a visible label pointing to it
            f"var labels=document.querySelectorAll('label[for]');"
            f"for(var i=0;i<labels.length;i++){{if(labels[i].htmlFor===e.id&&labels[i].offsetWidth>0)return'yes';}}"
            f"return'no';}})()"
        )
        result = self.cdp.eval(js, frame_id)
        return "yes" in result
