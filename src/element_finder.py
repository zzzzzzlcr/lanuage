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
            js = (
                f"(function(){{var el=document.querySelectorAll('*');"
                f"for(var i=0;i<el.length;i++){{"
                f"if(el[i].textContent.trim().indexOf('{escaped}')!==-1"
                f"&&el[i].offsetWidth>0{tag_filter})"
                f"{{el[i].setAttribute('data-target','x');return'yes';}}}}"
                f"return'no';}})()"
            )
            result = self.cdp.eval(js, frame_id)
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
        """Check if element exists and is visible."""
        escaped = selector.replace("'", "\\'")
        js = (
            f"(function(){{var el=document.querySelector('{escaped}');"
            f"if(el&&el.offsetWidth>0)return'yes';return'no';}})()"
        )
        result = self.cdp.eval(js, frame_id)
        return "yes" in result
