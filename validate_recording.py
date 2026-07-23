"""Validate locator against recording: extract label → locator → compare DOM element.

Supports two formats:
- v1.0: {pages: [{url, events: [{ariaLabel, selector, inputType, tag, value}]}]}
- flat: {page_url, steps: [{field: {label_text, id, type, tag}, find: {selector}}]}
"""
import sys, json, os, time, random
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))
from common import CDPHelper
from locator import FieldLocator, LocatorError


def extract_fields(data):
    """Extract form fields from either recording format. Returns [(label, selector, type, tag, value, url)]."""
    fields = []

    # v1.0 format: pages[].events[]
    for page in data.get("pages", []):
        url = page["url"]
        for e in page.get("events", []):
            label = e.get("ariaLabel", "") or e.get("labelText", "")
            sel = (e.get("selector", {}) or {}).get("primary", "")
            tag = e.get("tag", "")
            etype = e.get("inputType", "")
            value = e.get("value", "")
            if label and sel and tag in ("input", "select", "textarea"):
                fields.append((label, sel, etype, tag, value, url))

    # Flat format: steps[] with field.label_text
    url = data.get("page_url", "")
    for step in data.get("steps", []):
        field = step.get("field", {}) or {}
        find = step.get("find", {}) or {}
        label = field.get("label_text", "")
        sel = find.get("selector", "")
        tag = field.get("tag", "")
        etype = field.get("type", "")
        value = step.get("value", "")
        if label and sel and tag in ("input", "select", "textarea"):
            fields.append((label, sel, etype, tag, value, url))

    # Deduplicate: keep last occurrence per selector
    seen = {}
    for f in fields:
        seen[f[1]] = f
    return list(seen.values())


def navigate_to_form(cdp, target_selectors, max_rounds=25):
    """Auto-click through pre-qual / quiz / interstitial until form fields appear."""
    for rnd in range(max_rounds):
        # Check if any target field is visible
        for sel in target_selectors:
            try:
                exists = cdp.eval(
                    f"(function(){{var e=document.querySelector('{sel}');"
                    f"return e&&e.offsetWidth>0?'yes':'no';}})()")
                if 'yes' in str(exists):
                    return True
            except Exception:
                pass
        # Strategy 1: click CTA buttons first (higher priority)
        cta = cdp.eval('''(function(){
        var bs=document.querySelectorAll("button,a");
        var ctas=[], opts=[];
        for(var i=0;i<bs.length;i++){
        var b=bs[i];var t=b.textContent.trim();
        if(!b.offsetWidth||t.length<2)continue;
        if(t.match(/qualify|start|get|begin|continue|next|submit|apply|find|check|yes|no/i))ctas.push(b);
        else opts.push(b);
        }
        if(ctas.length>0){ctas[0].click();return"cta";}
        // Strategy 2: random leaf div (quiz option)
        var all=document.querySelectorAll("div");
        for(var i=0;i<all.length;i++){
        var d=all[i];var t=d.textContent.trim();
        if(!d.offsetWidth||t.length<3||t.length>70)continue;
        if(d.querySelector("div"))continue;
        if(d.closest("nav,header,footer"))continue;
        var rect=d.getBoundingClientRect();
        if(rect.top<50||rect.bottom>window.innerHeight-50)continue;
        opts.push(d);
        }
        if(opts.length>0){opts[Math.floor(Math.random()*opts.length)].click();return"option";}
        return"none";
        })()''')
        time.sleep(1.5)
    return False


def replay_and_validate(data, cdp):
    """Replay recording steps: execute clicks, verify form fields against locator."""
    results = {"pass": [], "fail": [], "steps": 0}
    steps = data.get("steps", [])
    page_url = data.get("page_url", "")

    if page_url:
        try:
            cdp.navigate(page_url)
            time.sleep(4)
        except Exception:
            cdp.eval(f"window.location.href='{page_url}'")
            time.sleep(5)

    # Also handle v1.0 format
    for page in data.get("pages", []):
        steps.extend(page.get("events", []))

    # Gather all unique form field tests (before navigation)
    form_fields = {}
    for s in steps:
        field = s.get("field", {}) or {}
        label = field.get("label_text", "")
        sel = (s.get("find", {}) or {}).get("selector", "")
        if label and sel:
            form_fields[sel] = {"label": label, "type": field.get("type", "text"),
                                "tag": field.get("tag", "input"),
                                "value": s.get("value", ""),
                                "selector": sel}

    # Auto-navigate past pre-qual/quiz if form not immediately visible
    form_sels = [f["selector"] for f in form_fields.values() if f["selector"].startswith("#")]
    if form_sels:
        ok = navigate_to_form(cdp, form_sels)
        if not ok:
            print("  ⚠️ Could not reach form (pre-qual not passed)")

    loc = FieldLocator(cdp)
    tested = set()
    rnd = 0
    for s in steps:
        action = s.get("action", "")
        find = s.get("find", {}) or {}
        sel = find.get("selector", "")

        if action == "click" and sel:
            # Execute navigation clicks (buttons, CTAs)
            try:
                cdp.eval(
                    f"(function(){{var e=document.querySelector('{sel}');"
                    f"if(e&&e.offsetWidth>0){{e.click();return'ok';}}"
                    f"return'skip';}})()")
                time.sleep(2)
                cdp.wait_page_stable(timeout=8)
                rnd += 1
            except Exception:
                pass

        elif action == "form" and sel in form_fields and sel not in tested:
            f = form_fields[sel]
            tested.add(sel)
            field = {"label": f["label"]}
            if f["type"] in ("email", "tel", "number"):
                field["type"] = f["type"]
            elif f["tag"] == "select":
                field["type"] = "select"

            try:
                result = loc.locate(field)
                our_sel = result.selector
                esc = our_sel.replace("'", "\\'")
                same = cdp.eval(
                    f"(function(){{var a=document.querySelector('{sel}');"
                    f"var b=document.querySelector('{esc}');"
                    f"return a&&b&&a===b?'yes':'no';}})()")
                match = 'yes' in str(same)

                fill_ok = False
                if match and f["value"] and "{{" not in f["value"]:
                    try:
                        cdp.form(our_sel, value=f["value"])
                        time.sleep(0.2)
                        actual = cdp.eval(
                            f"(function(){{var e=document.querySelector('{esc}');"
                            f"return e?e.value:'';}})()")
                        fill_ok = f["value"] in str(actual)
                    except Exception:
                        pass

                if match:
                    fill_str = "✅ fill" if fill_ok else ""
                    results["pass"].append(f)
                    print(f"  ✅ {f['label']}: {our_sel} ({result.strategy}) {fill_str}")
                else:
                    results["fail"].append(f)
                    print(f"  ❌ {f['label']}: rec={sel} ours={our_sel} ({result.strategy})")
                results["steps"] += 1
            except LocatorError as e:
                results["fail"].append(f)
                print(f"  ❌ {f['label']}: {sel} → FAILED: {e}")
                results["steps"] += 1

        elif action in ("select", "wait"):
            # Select random, wait — handled by recording replay
            pass

    return results


def validate(recording_path, ws_url):
    with open(recording_path) as f:
        data = json.load(f)

    fields = extract_fields(data)
    if not fields:
        print("No form fields found in recording")
        return

    cdp = CDPHelper(ws_url)
    print(f"\n=== {data.get('page_url', data.get('pages',[{}])[0].get('url','unknown'))} ===")

    results = replay_and_validate(data, cdp)

    total = len(results["pass"]) + len(results["fail"])
    pct = len(results["pass"]) * 100 // total if total else 0
    print(f"\n{'='*50}")
    print(f"Result: {len(results['pass'])}/{total} passed ({pct}%)")
    if results["fail"]:
        print("Failures:")
        for f in results["fail"]:
            print(f"  {f['label']}: rec={f.get('selector','?')}")
    return results


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("recording", help="Recording JSON file")
    p.add_argument("--ws", default=os.environ.get("WS_URL",
        "ws://127.0.0.1:9222/devtools/browser/acfeb9df-2d32-4b81-83d3-dd3ba14d3aa6"),
        help="WebSocket URL")
    args = p.parse_args()
    validate(args.recording, args.ws)
