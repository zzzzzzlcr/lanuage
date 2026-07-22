"""Diagnose form component patterns across sites. Reports locator pass/fail per field."""
import sys, json, time, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))
from common import CDPHelper
from locator import FieldLocator, LocatorError

SITES = [
    "https://everloaf.com/pages/aunt-OB",
    "https://smarteradvisors.co/7secrets-nav",
    "https://seniorbathupgrade.com/",
    "https://www.womensfitnessandstyle.com/5-best-dishwasher-detergents-avoid-gut-health-issues/",
    "https://nexaralai.com/",
]

def diagnose(cdp, url):
    print(f"\n{'='*60}\n{url}\n{'='*60}")
    cdp.navigate(url)
    time.sleep(6)
    cdp.wait_page_stable(timeout=10)

    # Get all form elements with their DOM context
    js = r'''(function(){
    var r=[];
    var all=document.querySelectorAll('input:not([type=hidden]),select,textarea,[role=combobox],[role=checkbox],[role=radio],[role=switch],[role=slider]');
    for(var i=0;i<all.length;i++){
    var e=all[i];
    var v={
    idx:i, tag:e.tagName, type:e.type||'', id:e.id||'', name:e.name||'',
    placeholder:e.placeholder||'', aria:e.getAttribute('aria-label')||'',
    testid:e.getAttribute('data-testid')||'',
    role:e.getAttribute('role')||'',
    ow:e.offsetWidth, oh:e.offsetHeight,
    isNative:(e.tagName=='INPUT'||e.tagName=='SELECT'||e.tagName=='TEXTAREA')&&!e.getAttribute('role'),
    isMUI:e.className&&(e.className.toString().indexOf('Mui')!==-1),
    };
    // Find associated label
    var label=e.closest('div,fieldset,form');
    if(label){
    var ls=label.querySelectorAll('label');
    for(var j=0;j<ls.length;j++){
    if(ls[j].textContent.trim()&&ls[j].offsetWidth>0){
    v.labelText=ls[j].textContent.trim().substring(0,60);
    v.labelFor=ls[j].htmlFor||'';
    break;
    }
    }
    }
    // Check parent class for framework hints
    var p=e.parentElement;
    if(p){v.parentCls=(p.className||'').toString().substring(0,60);}
    r.push(v);
    }
    return JSON.stringify(r);
    })()'''
    elements = cdp.eval(js)

    if not elements:
        print("  No form elements found")
        return

    locator = FieldLocator(cdp)
    results = {"pass": 0, "fail": 0, "details": []}

    for el in elements:
        tag = el.get('tag','')
        label_text = el.get('labelText','') or el.get('placeholder','') or el.get('aria','') or el.get('name','')
        framework = 'MUI' if el.get('isMUI') else 'native'
        comp_type = 'text'
        if el.get('type') == 'email': comp_type = 'email'
        elif el.get('type') == 'tel': comp_type = 'tel'
        elif el.get('role') == 'combobox' or tag == 'SELECT': comp_type = 'select'
        elif el.get('role') == 'checkbox' or el.get('type') == 'checkbox': comp_type = 'checkbox'

        field = {"label": label_text} if label_text else {"name": el.get('name','')}
        if comp_type in ('email','tel'): field["type"] = comp_type

        try:
            result = locator.locate(field)
            status = "PASS"
            results["pass"] += 1
            strat = result.strategy
        except LocatorError as e:
            status = "FAIL"
            results["fail"] += 1
            strat = f"FAILED: {len(e.attempts)} attempts"

        detail = f"  [{status}] {framework}:{comp_type} | label='{label_text[:40]}' | {strat}"
        results["details"].append(detail)
        print(detail)

    print(f"\n  Summary: {results['pass']}/{results['pass']+results['fail']} passed")
    return results


def main():
    ws_url = sys.argv[1] if len(sys.argv) > 1 else os.environ.get('WS_URL', 'ws://127.0.0.1:9222/devtools/browser/acfeb9df-2d32-4b81-83d3-dd3ba14d3aa6')
    cdp = CDPHelper(ws_url)
    all_results = {}
    for url in SITES:
        try:
            all_results[url] = diagnose(cdp, url)
        except Exception as e:
            print(f"  ERROR: {e}")
    total_pass = sum(r["pass"] for r in all_results.values() if r)
    total_fail = sum(r["fail"] for r in all_results.values() if r)
    print(f"\n{'='*60}\nTOTAL: {total_pass}/{total_pass+total_fail} fields passed across {len(SITES)} sites")

if __name__ == '__main__':
    main()
