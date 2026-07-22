"""Validate locator against recording: feed ariaLabel → locator → compare selector."""
import sys, json, os, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))
from common import CDPHelper
from locator import FieldLocator, LocatorError

def validate(recording_path, ws_url):
    with open(recording_path) as f:
        data = json.load(f)

    cdp = CDPHelper(ws_url)
    results = {"pass": [], "fail": [], "skip": []}

    for page in data.get("pages", []):
        url = page["url"]
        events = page.get("events", [])
        if not events:
            continue

        # Only analyze pages with form events
        form_events = [e for e in events if e["type"] in ("change", "click")
                       and e.get("ariaLabel") and e.get("tag") in ("input", "select", "textarea")]
        if not form_events:
            continue

        print(f"\n=== {url} ===")
        cdp.navigate(url)
        time.sleep(3)

        # Deduplicate: only last event per selector
        seen = {}
        for e in form_events:
            sel = e["selector"]["primary"]
            # Keep the last event (final value)
            seen[sel] = e

        loc = FieldLocator(cdp)
        for sel, event in seen.items():
            aria = event["ariaLabel"]
            input_type = event.get("inputType", "text")
            tag = event["tag"]

            # Build semantic field description from recording
            field = {"label": aria}
            if input_type in ("email", "tel", "number"):
                field["type"] = input_type
            elif tag == "select":
                field["type"] = "select"

            try:
                result = loc.locate(field)
                our_sel = result.selector
                strategy = result.strategy

                # Verify by DOM element identity: do both selectors point to same element?
                esc = our_sel.replace("'", "\\'")
                same = cdp.eval(
                    f"(function(){{var a=document.querySelector('{sel}');"
                    f"var b=document.querySelector('{esc}');"
                    f"return a&&b&&a===b?'yes':'no';}})()")
                match = 'yes' in str(same)

                # Also try filling and reading back
                fill_ok = False
                test_value = event.get("value", "")
                if test_value and match:
                    try:
                        cdp.form(our_sel, value=test_value)
                        time.sleep(0.2)
                        actual = cdp.eval(
                            f"(function(){{var e=document.querySelector('{esc}');"
                            f"return e?e.value:'';}})()")
                        fill_ok = test_value in str(actual)
                    except Exception:
                        pass

                if match:
                    fill_str = "✅ fill" if fill_ok else ""
                    results["pass"].append({
                        "field": aria, "recorded": sel, "ours": our_sel,
                        "strategy": strategy, "fill_ok": fill_ok, "url": url
                    })
                    print(f"  ✅ {aria}: {sel} → {our_sel} ({strategy}) {fill_str}")
                else:
                    results["fail"].append({
                        "field": aria, "recorded": sel, "ours": our_sel,
                        "strategy": strategy, "url": url
                    })
                    print(f"  ❌ {aria}: recorded={sel} ours={our_sel} ({strategy})")
            except LocatorError as e:
                results["fail"].append({
                    "field": aria, "recorded": sel, "ours": f"FAILED: {e}",
                    "strategy": "none", "url": url
                })
                print(f"  ❌ {aria}: {sel} → LOCATOR FAILED: {e}")

    # Summary
    total = len(results["pass"]) + len(results["fail"])
    print(f"\n{'='*50}")
    print(f"Pass: {len(results['pass'])}/{total} ({len(results['pass'])*100//total if total else 0}%)")
    if results["fail"]:
        print(f"Failed:")
        for f in results["fail"]:
            print(f"  {f['field']}: recorded={f['recorded']} ours={f['ours']}")

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
