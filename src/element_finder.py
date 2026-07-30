"""Convert JSON find specs to CSS selectors for CDP operations."""
import json
import logging


class ElementFinder:
    """Resolve find specs to CSS selectors via CDP eval visibility checks.

    Priority: id > name > text(+tag) > selector > xpath
    """

    def __init__(self, cdp, log=None):
        self.cdp = cdp
        self.log = log or logging.getLogger(__name__)

    def find(self, find_spec: dict, frame: dict = None) -> str | None:
        """Resolve find_spec to a CSS selector. Returns None if no match."""
        frame_id = frame.get("frame_id", "") if frame else ""

        # 1. By id — fastest
        if "id" in find_spec:
            selector = f"#{find_spec['id']}"
            if self._visible(selector, frame_id):
                return selector
            return None

        # 2. By name attribute
        if "name" in find_spec:
            escaped = find_spec["name"].replace('"', '\\"')
            selector = f'[name="{escaped}"]'
            if self._visible(selector, frame_id):
                return selector
            # If name not found, fall through to next priority

        # 3. By text content + optional tag filter
        if "text" in find_spec:
            tag = find_spec.get("tag", "")
            escaped = find_spec["text"].replace("'", "\\'")
            tag_filter = f"&&el[i].tagName==='{tag.upper()}'" if tag else ""
            # Exclude HTML and BODY — they contain all page text and are not clickable
            no_html_body = "&&el[i].tagName!=='HTML'&&el[i].tagName!=='BODY'" if not tag else ""
            js = (
                f"(function(){{"
                # Clean up previous marker so CDP click targets the right element
                f"var prev=document.querySelector('[data-target]');if(prev)prev.removeAttribute('data-target');"
                f"var el=document.querySelectorAll('*');"
                f"var best=null,bestLen=999999,bestIsInteractive=false;"
                f"for(var i=0;i<el.length;i++){{"
                f"if(el[i].textContent.trim().toLowerCase().indexOf('{escaped.lower()}')!==-1"
                f"&&el[i].offsetWidth>0{tag_filter}{no_html_body}){{"
                f"var tlen=el[i].textContent.trim().length;"
                f"var tag=el[i].tagName;"
                f"var isInteractive=(tag==='A'||tag==='BUTTON'||tag==='LABEL'||tag==='INPUT');"
                # Prefer interactive elements; among equals, prefer shortest text
                f"if(isInteractive&&!bestIsInteractive){{best=el[i];bestLen=tlen;bestIsInteractive=true;}}"
                f"else if(isInteractive==bestIsInteractive&&tlen<bestLen){{best=el[i];bestLen=tlen;}}"
                f"}}}}"
                f"if(best){{best.setAttribute('data-target','x');return'yes';}}"
                f"return'no';}})()"
            )
            result = self.cdp.eval(js, frame_id=frame_id)
            if "yes" in result:
                return '[data-target="x"]'
            return None

        # 4. CSS selector — returned directly (caller's responsibility)
        if "selector" in find_spec:
            return find_spec["selector"]
        # 4b. "css" is an alias for "selector"
        if "css" in find_spec:
            return find_spec["css"]

        # 5. XPath — convert to JS click via eval
        if "xpath" in find_spec:
            # Not implemented yet — use eval action instead
            return None

        return None

    def _visible(self, selector: str, frame_id: str) -> bool:
        """Check if element exists. Accepts hidden elements with semantic attributes."""
        escaped = selector.replace("'", "\\'")
        js = (
            f"(function(){{var el=document.querySelector('{escaped}');"
            f"if(!el)return'no';"
            f"if(el.offsetWidth>0)return'yes';"
            f"if(el.id||el.getAttribute('data-testid')||el.getAttribute('aria-label')){{"
            f"var p=el;for(var lv=0;lv<5;lv++){{p=p.parentElement;if(!p)break;"
            f"if(p.style&&p.style.display==='none')return'no';"
            f"var cs=window.getComputedStyle(p);if(cs&&cs.display==='none')return'no';}}}}"
            f"return'no';}})()"
        )
        result = self.cdp.eval(js, frame_id=frame_id)
        return "yes" in result
