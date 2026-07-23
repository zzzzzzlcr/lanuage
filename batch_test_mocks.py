"""Batch test all mock pages - auto-extract form fields and success conditions."""
import sys,os,time,json,re,urllib.request

BASE = "http://localhost:8080"

# Routes that are multi-page flows — skip for now
SKIP = {"/","/calculator","/account/checkout","/auth/login","/news-feed",
        "/tello/login","/tello/plan","/tello/register",
        "/spree/success","/spree/verify-gps","/ctm/health_quote_v4.jsp","/ctm/results",
        "/hub/auth/verify","/datewhirl","/reactapp/thank-you","/livebeam",
        "/agathaskyangel/thanks","/geminihealth/shop",
        "/connecthearing/thanks","/compareinsulation/thank-you",
        "/ace/home","/irspenalty/form",
        "/forms/entyrecare","/irspenalty/form","/nexaralai/contact"}

def get_routes():
    """Get all form page routes from mock server."""
    html = urllib.request.urlopen(f"{BASE}/").read().decode()
    # Just use the known list from app.py
    routes = []
    for line in open("/company/mock-server/app.py"):
        m = re.search(r"@app\.route\('([^']+)'\)", line)
        if m:
            r = m.group(1)
            if r not in SKIP and not r.startswith("/forms/entyrecare/step"):
                routes.append(r)
    return sorted(set(routes))

def analyze_page(route):
    """Extract form fields and success condition from mock page HTML."""
    url = f"{BASE}{route}"
    try:
        html = urllib.request.urlopen(url, timeout=5).read().decode()
    except Exception:
        return None

    # Find success text
    success_texts = []
    for pattern in [r'class="[^"]*success[^"]*"[^>]*>\s*<h[23][^>]*>([^<]+)',
                    r'id="[^"]*success[^"]*"[^>]*>\s*<h[23][^>]*>([^<]+)',
                    r'alert\([^)]*([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,4})',
                    r'"\+\s*\'([^\']+)\'\s*\+',
                    r'textContent\s*=\s*[\'"]([^\'"]{3,40})[\'"]',
                    r'textContent\s*=.*?[\'"]\s*\+\s*[\'"]([^\'"]{3,40})[\'"]',
                    r'>Thank you<', r'>Success<', r'>Complete<',
                    r'>Congratulations<', r'>Selected:<']:
        found = re.findall(pattern, html)
        success_texts.extend(found)

    # Find form fields (inputs with labels or placeholders)
    fields = []
    # Inputs with placeholders
    for ph in re.findall(r'placeholder="([^"]+)"', html):
        fields.append(ph)
    # Labels
    for label in re.findall(r'<label[^>]*>([^<]+)</label>', html):
        t = label.strip()
        if t and len(t) < 60:
            fields.append(t)
    # aria-labels
    for al in re.findall(r'aria-label="([^"]+)"', html):
        fields.append(al)

    # Determine success text
    success = ""
    priority = ["Scan Complete", "Registration Complete", "Thank you", "Terms accepted",
                "Congratulations", "Selected:", "form submitted", "Success", "Complete"]
    for p in priority:
        for s in success_texts:
            if p.lower() in s.lower() or (isinstance(s, str) and p.lower() in s.lower()):
                success = p
                break
        if success: break
    if not success and success_texts:
        success = str(success_texts[0])[:30]

    return {"route": route, "fields": fields[:8], "success": success, "has_form": len(fields) > 0}


def generate_description(info):
    """Generate NL description from page analysis."""
    if not info or not info["has_form"]:
        return None

    fields = info["fields"]
    route = info["route"]
    success = info["success"] or "success"

    steps = ["1. 等待2-3秒"]
    step_n = 2

    for f in fields:
        f_lower = f.lower()
        if any(w in f_lower for w in ['email','e-mail','mail']):
            steps.append(f"{step_n}. 填写{f}")
        elif any(w in f_lower for w in ['phone','tel','mobile','telephone']):
            steps.append(f"{step_n}. 填写{f}")
        elif any(w in f_lower for w in ['name','first','last']):
            steps.append(f"{step_n}. 填写{f}")
        elif any(w in f_lower for w in ['zip','postal','postcode']):
            steps.append(f"{step_n}. 填写{f}")
        elif any(w in f_lower for w in ['age','year','dob']):
            steps.append(f"{step_n}. 填写{f}")
        elif any(w in f_lower for w in ['state','province','select']):
            steps.append(f"{step_n}. 选择{f}（下拉框，选第一个）")
        elif any(w in f_lower for w in ['address','street','city','ssn']):
            steps.append(f"{step_n}. 填写{f}")
        elif any(w in f_lower for w in ['term','agree','accept','consent','privacy']):
            steps.append(f"{step_n}. 勾选{f}")
        elif any(w in f_lower for w in ['message','comment','note']):
            steps.append(f"{step_n}. 填写{f}")
        else:
            steps.append(f"{step_n}. 填写{f}")
        steps.append(f"{step_n+1}. 等待0.5秒")
        step_n += 2

    steps.append(f"{step_n}. 点击Submit")
    desc = f"页面URL: {BASE}{route}\n类型: newsletter\n成功: 页面出现 {success}\n\n操作:\n" + "\n".join(steps)
    return desc


def main():
    routes = get_routes()
    print(f"Found {len(routes)} form routes")

    results = []
    for r in routes:
        info = analyze_page(r)
        desc = generate_description(info)
        if desc:
            results.append((r, desc, info["success"]))
            print(f"  {r}: {len(info['fields'])} fields, success='{info['success']}'")

    print(f"\nGenerated {len(results)} descriptions")

    # Save descriptions for later testing
    with open("/tmp/mock_descriptions.json", "w") as f:
        json.dump([{"route": r, "desc": d, "success": s} for r, d, s in results], f, indent=2)

    print("Saved to /tmp/mock_descriptions.json")

    # Test batch: run each one and track pass/fail
    if len(sys.argv) > 1 and sys.argv[1] == "--run":
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))
        from common import CDPHelper
        from json_pipeline import JSONPipeline
        from openai import OpenAI

        ws = os.environ.get("WS_URL","ws://127.0.0.1:9222/devtools/browser/a650ef37-8c06-4bd1-a8a2-9b4c52a455f9")
        key = os.environ.get("OPENAI_API_KEY","")
        if not key:
            print("Set OPENAI_API_KEY")
            return

        cdp = CDPHelper(ws)
        llm = OpenAI(api_key=key, base_url=os.environ.get("OPENAI_BASE_URL","https://api.deepseek.com"))
        p = JSONPipeline(llm, cdp)

        passed = 0
        total = 0
        for route, desc, success in results:
            total += 1
            url = f"{BASE}{route}"
            try:
                cdp.eval("window.alert=function(){};window.confirm=function(){return true;};")
                cdp.eval(f"(function(){{window.location.href='{url}';}})()")
                time.sleep(2)
                cdp.eval("window.alert=function(){};window.confirm=function(){return true;};")
                config, result = p.run(desc.strip(), {'task_id': route}, url)
                ok = result.passed
                if ok: passed += 1
                print(f"{'✅' if ok else '❌'} {route} ({passed}/{total})")
            except Exception as e:
                print(f"❌ {route}: ERROR {e}")

        print(f"\n{'='*40}\nTotal: {passed}/{total} passed ({passed*100//total if total else 0}%)")


if __name__ == "__main__":
    main()
