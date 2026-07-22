"""Web editor for operators — write NL descriptions, run tests, see results."""
import json, os, sys, logging, traceback
from pathlib import Path
from flask import Flask, render_template_string, request, jsonify

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

app = Flask(__name__)

EDITOR_HTML = r'''<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>运营描述编辑器</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font:14px/1.6 system-ui,sans-serif;background:#f5f5f5;display:flex;height:100vh}
.panel{flex:1;display:flex;flex-direction:column;padding:16px;gap:8px}
.panel textarea{flex:1;font:13px monospace;border:1px solid #ccc;border-radius:6px;padding:12px;resize:none}
.panel pre{flex:1;background:#1e1e1e;color:#d4d4d4;border-radius:6px;padding:12px;overflow:auto;font:12px monospace;white-space:pre-wrap}
h3{font-size:14px;color:#333}
.btn{display:inline-flex;align-items:center;gap:4px;padding:8px 16px;border:none;border-radius:6px;cursor:pointer;font-size:13px}
.btn-run{background:#2563eb;color:#fff}.btn-run:hover{background:#1d4ed8}.btn-run:disabled{opacity:.5}
.status{margin-left:8px;font-size:12px}.ok{color:#16a34a}.fail{color:#dc2626}
</style>
</head>
<body>
<div class="panel">
  <h3>📝 运营描述</h3>
  <textarea id="desc" placeholder="页面URL: http://localhost:8080/xxx
类型: newsletter
成功: URL包含 /success

操作:
1. 等待2-4秒
2. 填邮箱
3. 点击Submit
4. 等待5-8秒"></textarea>
  <div style="display:flex;align-items:center">
    <button class="btn btn-run" onclick="run()" id="runBtn">▶ 运行</button>
    <span class="status" id="status"></span>
  </div>
</div>
<div class="panel">
  <h3>📊 结果</h3>
  <pre id="output">等待运行…</pre>
</div>
<script>
async function run(){
  const btn=document.getElementById('runBtn'),out=document.getElementById('output'),st=document.getElementById('status');
  btn.disabled=true;st.textContent='运行中…';st.className='status';out.textContent='执行中…';
  try{
    const r=await fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({description:document.getElementById('desc').value})});
    const d=await r.json();
    st.textContent=d.passed?'✅ 通过':'❌ 失败';st.className=d.passed?'ok':'fail';
    out.textContent=JSON.stringify(d.report||d,d.report?undefined:null,2);
  }catch(e){
    st.textContent='❌ 错误';st.className='fail';out.textContent=e.message;
  }finally{btn.disabled=false}
}
</script>
</body>
</html>'''


@app.route('/')
def editor():
    return render_template_string(EDITOR_HTML)


@app.route('/api/run', methods=['POST'])
def api_run():
    data = request.get_json()
    desc = (data or {}).get('description', '')
    if not desc.strip():
        return jsonify({'passed': False, 'error': 'no description'})

    ws_url = os.environ.get('WS_URL', 'ws://127.0.0.1:9222/devtools/browser/acfeb9df-2d32-4b81-83d3-dd3ba14d3aa6')
    try:
        from common import CDPHelper
        from json_pipeline import JSONPipeline
        from openai import OpenAI

        cdp = CDPHelper(ws_url)
        llm = OpenAI(
            api_key=os.environ.get('OPENAI_API_KEY', ''),
            base_url=os.environ.get('OPENAI_BASE_URL', 'https://api.deepseek.com')
        )
        pipeline = JSONPipeline(llm, cdp)

        # Extract URL from first line
        url = 'http://localhost:8080/'
        for line in desc.split('\n'):
            if 'URL:' in line or 'http' in line:
                url = line.split('URL:')[-1].strip() if 'URL:' in line else line.strip()
                url = url.split()[0] if url else url
                break

        config, result = pipeline.run(desc, {'task_id': 'web_editor'}, url)

        return jsonify({
            'passed': result.passed,
            'success_steps': result.success_steps,
            'total_steps': result.total_steps,
            'fix_cycles': getattr(result, 'fix_cycles', 0),
            'report': {
                'outcome': {'passed': result.passed, 'success_steps': result.success_steps, 'total_steps': result.total_steps},
                'steps': [{'index': s.index, 'action': s.action, 'success': s.success, 'error': s.error}
                          for s in getattr(result, 'failed_steps', [])[:20]],
            }
        })
    except Exception as e:
        return jsonify({'passed': False, 'error': str(e), 'traceback': traceback.format_exc()})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
