"""Catty FastAPI Dashboard — context window 实时显示 + 流式接收预览 + cache 监控.

主人 2026-05-28 C6: 挂在 NoneBot 自带的 FastAPI app 上, 不引入额外 GUI 框架.
路由:
    GET  /dashboard          → HTML 主页 (内嵌简易 CSS/JS)
    GET  /dashboard/sse      → Server-Sent Events 实时推送 (cache_stats / stream_*)
    GET  /dashboard/state    → JSON snapshot 当前 active/completed streams

只 listen 127.0.0.1 不开放外网 (NoneBot config 决定 host).

数据源: dashboard_state.py 的 module-level state, 由 anthropic_native_client streaming
和 _log_native_usage push 更新.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator

logger = logging.getLogger("catty_qq_ai.dashboard")

_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>Catty Dashboard - Context Window</title>
<style>
* { box-sizing: border-box; }
body { font: 14px/1.5 ui-sans-serif, system-ui, sans-serif; margin: 0; padding: 16px; background: #f7f8fa; color: #222; }
h1 { font-size: 18px; margin: 0 0 12px; }
h2 { font-size: 14px; margin: 16px 0 8px; color: #555; }
.section { background: #fff; border: 1px solid #e1e4e8; border-radius: 6px; padding: 12px; margin-bottom: 12px; }
.bar { display: flex; height: 20px; border-radius: 4px; overflow: hidden; background: #eee; }
.bar > div { height: 100%; transition: width 0.3s; }
.bar .read   { background: #2ea44f; }
.bar .create { background: #f1c40f; }
.bar .input  { background: #3498db; }
.bar .output { background: #e67e22; }
.legend { display: flex; gap: 12px; font-size: 12px; margin-top: 4px; }
.legend span { display: inline-flex; align-items: center; gap: 4px; }
.legend i { width: 10px; height: 10px; border-radius: 2px; display: inline-block; }
.stream-preview { max-height: 200px; overflow-y: auto; background: #fafbfc; border: 1px solid #d1d5da; border-radius: 4px; padding: 8px; font-family: ui-monospace, monospace; font-size: 12px; white-space: pre-wrap; word-wrap: break-word; }
.scope-list { font-family: ui-monospace, monospace; font-size: 12px; }
.scope-list .row { display: grid; grid-template-columns: 1fr auto auto; gap: 12px; padding: 4px 0; border-bottom: 1px dashed #eee; }
.hit-bad { color: #e74c3c; font-weight: bold; }
.hit-mid { color: #f39c12; font-weight: bold; }
.hit-good { color: #27ae60; font-weight: bold; }
#status { padding: 4px 8px; border-radius: 12px; font-size: 12px; }
.status-on { background: #2ea44f; color: #fff; }
.status-off { background: #ccc; color: #555; }
</style>
</head>
<body>
<h1>🐾 Catty Dashboard <span id="status" class="status-off">SSE 未连接</span> <span id="build" style="font-size:11px;color:#aaa;font-weight:normal;">build=__BUILD_TS__</span></h1>
<div id="js-err" style="display:none;background:#ffe;border:1px solid #f0c;padding:6px;font:12px monospace;color:#c00;margin-bottom:8px;"></div>
<div id="diag" style="background:#eef;border:1px solid #99c;padding:6px;font:12px monospace;color:#039;margin-bottom:8px;">DIAG: HTML 加载完成, JS 未执行 — 如果一直停在这, 说明 script 块没跑或语法错</div>

<div class="section">
  <h2>Context Window (最近 5 个 scope)</h2>
  <div id="scope-list" class="scope-list">
    <div class="row" style="font-weight:bold; color:#555;">
      <span>scope (model)</span><span>tokens (R/C/I/O)</span><span>hit</span>
    </div>
    <div id="scope-rows">等待 cache_stats 推送…</div>
  </div>
  <div class="legend">
    <span><i style="background:#2ea44f"></i>cache_read (90% off)</span>
    <span><i style="background:#f1c40f"></i>cache_create (1.25x)</span>
    <span><i style="background:#3498db"></i>input (base)</span>
    <span><i style="background:#e67e22"></i>output</span>
  </div>
</div>

<div class="section">
  <h2>当前流式接收 <span id="active-count" style="color:#888; font-weight:normal;"></span></h2>
  <div id="streams">无 active streams</div>
</div>

<div class="section">
  <h2>最近事件 (实时滚动)</h2>
  <div id="events" class="stream-preview" style="max-height:180px;">等待…</div>
</div>

<script>
// 全局 JS 错误捕获 → 显示到页面 (诊断 cache / 加载问题)
window.onerror = function(msg, src, line, col, err) {
  const box = document.getElementById('js-err');
  if (box) {
    box.style.display = 'block';
    box.textContent = `JS ERROR: ${msg} @ ${src}:${line}:${col}`;
  }
  return false;
};
console.log('[catty dash] script start, build=__BUILD_TS__');
function diag(step) {
  const el = document.getElementById('diag');
  if (el) el.textContent = 'DIAG: ' + step;
  console.log('[catty dash] diag:', step);
}
diag('step1: script started');
const scopeMap = new Map();  // model -> {read, create, input, output, hit, last_ts}
const activeStreams = new Map();  // stream_id -> {text, model, started}
const eventsBox = document.getElementById('events');
const statusEl = document.getElementById('status');

function renderScopes() {
  const rows = [...scopeMap.entries()].sort((a,b) => b[1].last_ts - a[1].last_ts).slice(0, 5);
  if (rows.length === 0) {
    document.getElementById('scope-rows').innerHTML = '等待 cache_stats…';
    return;
  }
  document.getElementById('scope-rows').innerHTML = rows.map(([model, s]) => {
    const total = s.read + s.create + s.input + s.output;
    const pct = (n) => total > 0 ? (n / total * 100).toFixed(0) + '%' : '0%';
    const hitClass = s.hit < 0.1 ? 'hit-bad' : s.hit < 0.5 ? 'hit-mid' : 'hit-good';
    return `
      <div class="row">
        <div>
          <div><b>${model}</b></div>
          <div class="bar">
            <div class="read"   style="width:${pct(s.read)}"></div>
            <div class="create" style="width:${pct(s.create)}"></div>
            <div class="input"  style="width:${pct(s.input)}"></div>
            <div class="output" style="width:${pct(s.output)}"></div>
          </div>
        </div>
        <div>R ${s.read} / C ${s.create} / I ${s.input} / O ${s.output}</div>
        <div class="${hitClass}">${(s.hit*100).toFixed(1)}%</div>
      </div>`;
  }).join('');
}

function renderStreams() {
  document.getElementById('active-count').textContent = activeStreams.size > 0 ? `(${activeStreams.size} active)` : '';
  if (activeStreams.size === 0) {
    document.getElementById('streams').textContent = '无 active streams';
    return;
  }
  document.getElementById('streams').innerHTML = [...activeStreams.entries()].map(([sid, s]) => `
    <div style="margin-bottom:8px;">
      <div style="font-size:12px; color:#666;">${s.model} · ${sid} · ${((Date.now()/1000 - s.started)).toFixed(1)}s · ${s.text.length} 字</div>
      <div class="stream-preview">${(s.text.slice(-400) || '(等待 chunk…)').replace(/</g, '&lt;')}</div>
    </div>
  `).join('');
}

function appendEvent(line) {
  const ts = new Date().toLocaleTimeString();
  eventsBox.textContent = `[${ts}] ${line}\\n` + eventsBox.textContent.split('\\n').slice(0, 30).join('\\n');
}

function setConnected() {
  statusEl.textContent ='SSE 已连接';
  statusEl.className ='status-on';
}
function setReconnecting() {
  statusEl.textContent ='SSE 重连中…';
  statusEl.className ='status-off';
}
function connectSSE() {
  diag('step3: connectSSE called');
  statusEl.textContent ='SSE 连接中…';
  const es = new EventSource('/dashboard/sse');
  diag('step4: EventSource created, readyState=' + es.readyState);
  console.log('[catty dash] EventSource created, readyState=', es.readyState);
  es.onopen = () => { diag('step5: SSE OPEN'); console.log('[catty dash] SSE OPEN'); setConnected(); };
  es.onerror = (e) => {
    diag('step5x: SSE ERROR readyState=' + es.readyState);
    console.warn('[catty dash] SSE ERROR', es.readyState, e);
    setReconnecting();
  };
  es.onmessage = (e) => {
    // 兜底: 部分浏览器/中间链路下 onopen 不 fire, 第一次 onmessage 也算 connected
    if (statusEl.className !== 'status-on') setConnected();
    let payload;
    try { payload = JSON.parse(e.data); } catch (err) {
      console.warn('[catty dash] JSON parse fail', err, e.data.slice(0, 100));
      return;
    }
    if (payload.type === 'cache_stats') {
      scopeMap.set(payload.scope || 'unknown', {
        read: payload.cache_read, create: payload.cache_create,
        input: payload.input_tokens, output: payload.output_tokens,
        hit: payload.hit_ratio, last_ts: payload.ts,
      });
      renderScopes();
      appendEvent(`cache_stats ${payload.scope} hit=${(payload.hit_ratio*100).toFixed(0)}% R=${payload.cache_read} C=${payload.cache_create}`);
    } else if (payload.type === 'stream_start') {
      activeStreams.set(payload.stream_id, { text: '', model: payload.model, started: payload.ts });
      renderStreams();
      appendEvent(`stream_start ${payload.model} ${payload.stream_id}`);
    } else if (payload.type === 'stream_delta') {
      const s = activeStreams.get(payload.stream_id);
      if (s) { s.text += payload.delta_text || ''; renderStreams(); }
    } else if (payload.type === 'stream_end') {
      activeStreams.delete(payload.stream_id);
      renderStreams();
      appendEvent(`stream_end ${payload.stream_id} duration=${payload.duration_s.toFixed(1)}s text_len=${payload.text_len}`);
    }
  };
}
diag('step2: about to call connectSSE');
try {
  connectSSE();
} catch (err) {
  diag('step2x: connectSSE threw ' + err.message);
}
</script>
</body>
</html>
"""


_JS_TEST_HTML = (
    "<!DOCTYPE html><html><head><meta charset=utf-8>"
    "<title>JS test</title></head><body>"
    "<h1 id=t style='font-family:monospace;color:#c00'>JS 未跑 (如果一直停在这, 浏览器或中间设备屏蔽了 inline script)</h1>"
    "<script>document.getElementById('t').textContent='JS 跑了 OK';document.getElementById('t').style.color='#0a0';</script>"
    "<p>EventSource API 检测:</p>"
    "<p id=es>未检测</p>"
    "<script>const es_ok=typeof EventSource!=='undefined';document.getElementById('es').textContent='EventSource API: '+(es_ok?'可用 OK':'不可用');document.getElementById('es').style.color=es_ok?'#0a0':'#c00';</script>"
    "<p>实际尝试连 SSE:</p>"
    "<p id=sse>未尝试</p>"
    "<script>try{const e=new EventSource('/dashboard/sse');e.onopen=()=>{document.getElementById('sse').textContent='SSE: onopen OK';document.getElementById('sse').style.color='#0a0';};e.onerror=()=>{document.getElementById('sse').textContent='SSE: onerror readyState='+e.readyState;document.getElementById('sse').style.color='#c00';};e.onmessage=(m)=>{document.getElementById('sse').textContent='SSE: 收到 = '+m.data.slice(0,80);document.getElementById('sse').style.color='#0a0';};}catch(err){document.getElementById('sse').textContent='SSE: 构造异常 '+err.message;}</script>"
    "</body></html>"
)


async def dashboard_html(test_mode: bool = False) -> str:
    """GET /dashboard - HTML 主页. 注入 build_ts 帮主人区分浏览器是否拿旧 cache.

    test_mode: 如果环境变量 CATTY_DASH_TEST=1 → 返回极简 JS test page.
    这是 hot-reload 友好的开关 (老 closure 不带 query param 也能用).
    """
    import os
    import datetime
    if test_mode or os.environ.get("CATTY_DASH_TEST") == "1":
        return _JS_TEST_HTML
    build_ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return _DASHBOARD_HTML.replace("__BUILD_TS__", build_ts)


async def dashboard_state() -> dict:
    """GET /dashboard/state - JSON snapshot"""
    from . import dashboard_state as _ds
    return _ds.get_state_snapshot()


async def dashboard_sse_stream() -> AsyncIterator[str]:
    """SSE generator. dashboard 前端 EventSource 订阅."""
    from . import dashboard_state as _ds
    queue = _ds.subscribe()
    try:
        # 进入时推一次 initial snapshot (含历史 streams)
        try:
            yield f"data: {json.dumps({'type': 'snapshot', 'data': _ds.get_state_snapshot()})}\n\n"
        except Exception:  # noqa: BLE001
            pass
        while True:
            try:
                payload = await asyncio.wait_for(queue.get(), timeout=20.0)
            except asyncio.TimeoutError:
                # heartbeat keep connection alive
                yield ": heartbeat\n\n"
                continue
            try:
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            except Exception as exc:  # noqa: BLE001
                logger.debug("SSE serialize failed: %s", exc)
    finally:
        _ds.unsubscribe(queue)


def mount_dashboard_routes() -> bool:
    """在 NoneBot 的 FastAPI app 上挂 dashboard 路由. 失败返回 False."""
    try:
        from nonebot import get_app
        from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
        app = get_app()
    except Exception as exc:  # noqa: BLE001
        logger.warning("dashboard: fastapi not available (%s), skip mount", exc)
        return False

    @app.get("/dashboard/_js_test", response_class=HTMLResponse)
    async def _dash_js_test():
        # 极简 inline script 测试 — 如果浏览器/中间设备过滤 inline JS, 这个也跑不起来
        html = (
            "<!DOCTYPE html><html><head><meta charset=utf-8>"
            "<title>JS test</title></head><body>"
            "<h1 id=t style='font-family:monospace;color:#c00'>JS 未跑 (如果一直停在这, 浏览器或中间设备屏蔽了 inline script)</h1>"
            "<script>document.getElementById('t').textContent='JS 跑了 OK';document.getElementById('t').style.color='#0a0';</script>"
            "<p>另外测一下 EventSource 是否可用:</p>"
            "<p id=es>EventSource API: 未检测</p>"
            "<script>const es_ok=typeof EventSource!=='undefined';document.getElementById('es').textContent='EventSource API: '+(es_ok?'可用 OK':'不可用 (浏览器禁了)');document.getElementById('es').style.color=es_ok?'#0a0':'#c00';</script>"
            "<p>实际尝试连 SSE:</p>"
            "<p id=sse>SSE: 未尝试</p>"
            "<script>try{const e=new EventSource('/dashboard/sse');e.onopen=()=>{document.getElementById('sse').textContent='SSE: onopen 触发 OK';document.getElementById('sse').style.color='#0a0';};e.onerror=()=>{document.getElementById('sse').textContent='SSE: onerror readyState='+e.readyState;document.getElementById('sse').style.color='#c00';};e.onmessage=(m)=>{document.getElementById('sse').textContent='SSE: 收到 msg = '+m.data.slice(0,80);document.getElementById('sse').style.color='#0a0';};}catch(err){document.getElementById('sse').textContent='SSE: 构造异常 '+err.message;}</script>"
            "</body></html>"
        )
        return HTMLResponse(
            content=html,
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    @app.get("/dashboard", response_class=HTMLResponse)
    async def _dash_html(test: str = ""):
        # 主人 2026-05-28: dashboard HTML 加 no-cache, 避免浏览器拿旧版导致 JS bug 修了不生效
        # ?test=1 → 极简 JS test page (诊断浏览器是否阻断 inline script)
        return HTMLResponse(
            content=await dashboard_html(test_mode=bool(test)),
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )

    @app.get("/dashboard/state", response_class=JSONResponse)
    async def _dash_state():
        return JSONResponse(content=await dashboard_state())

    @app.get("/dashboard/sse")
    async def _dash_sse():
        return StreamingResponse(
            dashboard_sse_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    logger.info("dashboard mounted: /dashboard /dashboard/state /dashboard/sse")
    return True


__all__ = ["mount_dashboard_routes"]
