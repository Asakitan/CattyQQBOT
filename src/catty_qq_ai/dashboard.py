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
  <h2>本次模型输入（最新 chat provider request）</h2>
  <div id="ctx-live">等待 cache_stats…</div>
</div>

<div class="section">
  <h2>当前会话窗口</h2>
  <div id="session-live">等待 session context…</div>
</div>

<div class="section">
  <h2>缓存效率</h2>
  <div id="cache-live">等待 cache_stats…</div>
  <div style="margin-top:8px; font-size:11px; color:#888;">
    DeepSeek 命中输入默认按未命中价格的 2% 估算；provider 前缀缓存是 best-effort，闲置后可能在数小时到数天内清理。
  </div>
</div>

<div class="section">
  <h2>Context Window (最近 5 个 scope)</h2>
  <div id="scope-list" class="scope-list">
    <div class="row" style="font-weight:bold; color:#555;">
      <span>scope (model)</span><span>tokens (R/C/I/O)</span><span>hit</span>
    </div>
    <div id="scope-rows">等待 cache_stats 推送…</div>
  </div>
  <div class="legend">
    <span><i style="background:#2ea44f"></i>cache_read (按 provider profile)</span>
    <span><i style="background:#f1c40f"></i>cache_create (provider 支持时)</span>
    <span><i style="background:#3498db"></i>input (100% 计费)</span>
    <span><i style="background:#e67e22"></i>output</span>
  </div>
</div>

<div class="section">
  <h2>当前流式接收 <span id="active-count" style="color:#888; font-weight:normal;"></span></h2>
  <div id="streams">无 active streams</div>
</div>

<div class="section">
  <h2>对话历史 <span id="dialog-count" style="color:#888; font-weight:normal;"></span>
    <span style="font-size:11px;color:#aaa;font-weight:normal;margin-left:8px;">新的在下面,旧的往上挤</span>
  </h2>
  <div id="dialog-stack" style="max-height:480px; overflow-y:auto; display:flex; flex-direction:column; gap:6px;">无对话历史</div>
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
const scopeMap = new Map();  // scope -> {model, read, create, input, output, hit, last_ts, total_context, billed_input_equiv, saved_pct}
const activeStreams = new Map();  // stream_id -> {text, model, started}
const dialogHistory = [];  // 对话堆栈: 新的 push 到末尾 (底部), 满 30 条从顶部 shift 丢弃
const DIALOG_MAX = 30;
const eventsBox = document.getElementById('events');
const statusEl = document.getElementById('status');
const CONTEXT_LIMIT = 0;
function fmt(value) {
  const n = Number(value || 0);
  if (!Number.isFinite(n)) return '0';
  return n >= 1000 ? (n / 1000).toFixed(1) + 'K' : (Number.isInteger(n) ? String(n) : n.toFixed(2));
}
function pct(value) { return (Number(value || 0) * 100).toFixed(1) + '%'; }
function inputTotal(s) { return Number(s.input_total || s.total_context || ((s.read || 0) + (s.create || 0) + (s.input || 0))); }

function modelBadgeStyle(model) {
  // DeepSeek 蓝紫, Claude Sonnet/Opus 紫红, 其他灰
  const m = (model || '').toLowerCase();
  if (m.includes('deepseek')) return 'background:#5b8dee;color:#fff';
  if (m.includes('opus')) return 'background:#9b59b6;color:#fff';
  if (m.includes('sonnet') || m.includes('claude')) return 'background:#7d3c98;color:#fff';
  if (m.includes('gpt') || m.includes('openai')) return 'background:#27ae60;color:#fff';
  return 'background:#95a5a6;color:#fff';
}

function renderContextLive() {
  const rows = [...scopeMap.entries()].sort((a,b) => b[1].last_ts - a[1].last_ts);
  const inputEl = document.getElementById('ctx-live');
  const sessionEl = document.getElementById('session-live');
  const cacheEl = document.getElementById('cache-live');
  if (rows.length === 0) {
    inputEl.textContent = '等待 cache_stats…';
    sessionEl.textContent = '等待 session context…';
    cacheEl.textContent = '等待 cache_stats…';
    return;
  }
  const [scope, s] = rows[0];
  const total = inputTotal(s);
  const read = Number(s.read || 0);
  const create = Number(s.create || 0);
  const fresh = Number(s.input || 0);
  const output = Number(s.output || 0);
  const actual = s.actual_hit_rate == null ? Number(s.hit || 0) : Number(s.actual_hit_rate);
  const normalized = s.normalized_cache_kpi;
  const rollingActual = Number(s.rolling_actual_hit_rate || 0);
  const rollingNormalized = s.rolling_normalized_cache_kpi;
  const c = s.session_context || {};
  const modelLimit = Number(c.model_context_tokens || CONTEXT_LIMIT);
  const retained = Number(c.retained_input_tokens || 0);
  const retainedPct = modelLimit > 0 ? (retained / modelLimit * 100).toFixed(2) + '%' : 'N/A';
  const totalForBar = total || 1;
  const createBilling = create > 0 ? ` · create ×${fmt(s.cache_create_billing_multiplier)}` : '';
  inputEl.innerHTML = `
    <div><b>scope</b>: ${scope} · <b>model</b>: ${s.model || '(unknown)'}</div>
    <div style="margin-top:6px;"><b>Model input</b>: ${fmt(total)} tokens <span style="font-size:11px;color:#666;">(input denominator; output excluded)</span></div>
    <div class="bar" style="margin-top:3px;"><div class="read" style="width:${Math.min(read / totalForBar * 100, 100)}%"></div><div class="create" style="width:${Math.min(create / totalForBar * 100, 100)}%"></div><div class="input" style="width:${Math.min(fresh / totalForBar * 100, 100)}%"></div></div>
    <div style="margin-top:5px;font-size:12px;">cached ${fmt(read)} · create ${fmt(create)} · uncached ${fmt(fresh)} · output ${fmt(output)} <b>(not in input total)</b></div>
  `;
  sessionEl.innerHTML = `
    <div><b>Retained input</b>: ${fmt(retained)} / ${modelLimit > 0 ? fmt(modelLimit) : 'N/A'} (${retainedPct})</div>
    <div style="margin-top:5px;"><b>History</b>: ${fmt(c.history_tokens)} tokens · ${fmt(c.history_turns)} turns · ${fmt(c.history_messages)} messages</div>
    <div style="margin-top:5px;"><b>Target / watermark / headroom</b>: ${fmt(c.target_context_tokens)} / ${fmt(c.history_high_watermark_tokens)} / ${fmt(c.headroom_tokens)}</div>
    <div style="margin-top:5px;"><b>Trim</b>: epoch ${fmt(c.trim_epoch)} · count ${fmt(c.trim_count)} · request removed ${fmt(c.request_trimmed_messages)}</div>
    <div style="margin-top:5px;"><b>Request</b>: ${s.request_route || '-'} / ${s.request_kind || c.request_kind || '-'} · ${s.logical_turn_id || c.logical_turn_id || '-'}</div>
  `;
  const hot99Status = s.hot99_status || 'N/A';
  const hot99Rate = s.hot99_rate == null ? 'N/A' : pct(s.hot99_rate);
  cacheEl.innerHTML = `
    <div><b>Actual hit</b>: <span class="${actual < 0.3 ? 'hit-bad' : actual < 0.6 ? 'hit-mid' : 'hit-good'}">${pct(actual)}</span> · <b>Normalized cacheable KPI</b>: ${normalized == null ? 'N/A' : pct(normalized)}</div>
    <div style="margin-top:5px;"><b>Rolling (${fmt(s.rolling_n)} events)</b>: actual ${pct(rollingActual)} · normalized ${rollingNormalized == null ? 'N/A' : pct(rollingNormalized)}</div>
    <div style="margin-top:5px;"><b>Hot99</b>: ${hot99Status} · raw ${hot99Rate} · eligible n=${fmt(s.hot99_eligible_count)}</div>
    <div style="margin-top:5px;"><b>Billing profile</b>: ${s.billing_profile || '-'} · cached input ×${fmt(s.cache_read_billing_multiplier)}${createBilling} · ${fmt(s.billed_input_equiv)} equivalent input · saved ${Number(s.saved_pct || 0).toFixed(1)}%</div>
  `;
}
function renderScopes() {
  const rows = [...scopeMap.entries()].sort((a,b) => b[1].last_ts - a[1].last_ts).slice(0, 5);
  if (rows.length === 0) {
    document.getElementById('scope-rows').innerHTML = '等待 cache/session 推送…';
    return;
  }
  document.getElementById('scope-rows').innerHTML = rows.map(([scope, s]) => {
    const total = inputTotal(s) || 1;
    const actual = s.actual_hit_rate == null ? Number(s.hit || 0) : Number(s.actual_hit_rate);
    const normalized = s.normalized_cache_kpi;
    const pctWidth = n => Math.min(Number(n || 0) / total * 100, 100).toFixed(0) + '%';
    const hitClass = actual < 0.1 ? 'hit-bad' : actual < 0.5 ? 'hit-mid' : 'hit-good';
    return `
      <div class="row">
        <div><div><b>${scope}</b> <span style="font-size:11px;color:#666;">${s.model || ''}</span></div><div class="bar"><div class="read" style="width:${pctWidth(s.read)}"></div><div class="create" style="width:${pctWidth(s.create)}"></div><div class="input" style="width:${pctWidth(s.input)}"></div></div></div>
        <div>R ${fmt(s.read)} / C ${fmt(s.create)} / I ${fmt(s.input)}<br><span style="font-size:11px;color:#666;">O ${fmt(s.output)} excluded</span></div>
        <div class="${hitClass}">${pct(actual)}<br><span style="font-size:11px;font-weight:normal;color:#666;">norm ${normalized == null ? 'N/A' : pct(normalized)} · roll ${pct(s.rolling_actual_hit_rate)}</span></div>
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

function appendDialog(item) {
  // 主人 2026-05-28: 对话堆栈, 新的进来 push 到末尾(底部), 旧的位置不变
  // 超过 DIALOG_MAX 从顶部丢弃 -> 视觉上是"旧的被挤上去 → 顶部消失"
  dialogHistory.push(item);
  while (dialogHistory.length > DIALOG_MAX) dialogHistory.shift();
  renderDialogStack();
}

function renderDialogStack() {
  const el = document.getElementById('dialog-stack');
  const cnt = document.getElementById('dialog-count');
  cnt.textContent = dialogHistory.length > 0 ? `(${dialogHistory.length}/${DIALOG_MAX})` : '';
  if (dialogHistory.length === 0) {
    el.innerHTML = '无对话历史';
    return;
  }
  // 时间正序: 旧在上, 新在下 (符合"挤上去"语义 + 聊天窗口风格)
  el.innerHTML = dialogHistory.map(d => {
    const ts = new Date((d.ended_ts || d.started || 0) * 1000).toLocaleTimeString();
    const dur = (d.duration_s || 0).toFixed(1);
    const preview = (d.text || '(空)').replace(/</g, '&lt;');
    return `
      <div style="border:1px solid #e1e4e8; border-radius:4px; padding:6px 8px; background:#fafbfc;">
        <div style="font-size:11px; color:#666; margin-bottom:4px; display:flex; align-items:center; gap:6px;">
          <span style="${modelBadgeStyle(d.model)}; padding:1px 6px; border-radius:3px; font-size:10px;">${d.model || '?'}</span>
          <span style="color:#999;">${ts}</span>
          <span>· ${dur}s · ${(d.text || '').length} 字</span>
        </div>
        <div style="font-family:ui-monospace,monospace; font-size:12px; white-space:pre-wrap; word-wrap:break-word; max-height:140px; overflow-y:auto; color:#222;">${preview}</div>
      </div>
    `;
  }).join('');
  // 自动滚到底部, 让最新对话可见
  el.scrollTop = el.scrollHeight;
}

function applySnapshot(snapshot) {
  scopeMap.clear();
  Object.entries(snapshot.scope_state || {}).forEach(([scope, state]) => {
    const latest = state.latest_cache_stats || {};
    scopeMap.set(scope, {
      model: latest.model || state.model || '',
      read: latest.cache_read || 0, create: latest.cache_create || 0,
      input: latest.input_tokens || 0, output: latest.output_tokens || 0,
      hit: latest.hit_ratio || 0, actual_hit_rate: latest.actual_hit_rate,
      normalized_cache_kpi: latest.normalized_cache_kpi,
      rolling_actual_hit_rate: (state.cache_rolling || {}).rolling_actual_hit_rate || 0,
      rolling_normalized_cache_kpi: (state.cache_rolling || {}).rolling_normalized_cache_kpi,
      rolling_n: (state.cache_rolling || {}).rolling_n || 0,
      input_total: latest.input_total || latest.total_context || 0,
      total_context: latest.total_context || 0,
      billed_input_equiv: latest.billed_input_equiv || 0,
      saved_pct: latest.saved_pct || 0,
      billing_profile: latest.billing_profile || '',
      cache_read_billing_multiplier: latest.cache_read_billing_multiplier || 0,
      cache_create_billing_multiplier: latest.cache_create_billing_multiplier || 0,
      request_route: latest.request_route || '', request_kind: latest.request_kind || '', logical_turn_id: latest.logical_turn_id || '',
      hot99_status: latest.hot99_status || 'N/A', hot99_rate: latest.hot99_rate,
      hot99_eligible_count: latest.hot99_eligible_count || 0,
      session_context: state.session_context || {},
      last_ts: latest.ts || state.updated_at || 0,
    });
  });
  activeStreams.clear();
  (snapshot.active_streams || []).forEach(stream => activeStreams.set(stream.stream_id, { text: stream.text_preview || '', model: stream.model || '', started: stream.started_at || Date.now() / 1000 }));
  renderContextLive();
  renderScopes();
  renderStreams();
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
    if (payload.type === 'snapshot') {
      applySnapshot(payload.data || {});
    } else if (payload.type === 'cache_stats') {
      if (payload.request_class === 'auxiliary') {
        appendEvent(`cache_stats auxiliary ${payload.request_route || payload.request_kind || '-'} input=${fmt(payload.input_total || payload.total_context)}`);
        return;
      }
      const existing = scopeMap.get(payload.scope || 'unknown') || {};
      scopeMap.set(payload.scope || 'unknown', {
        ...existing,
        model: payload.model || existing.model || '',
        read: payload.cache_read || 0, create: payload.cache_create || 0,
        input: payload.input_tokens || 0, output: payload.output_tokens || 0,
        hit: payload.hit_ratio || 0, actual_hit_rate: payload.actual_hit_rate,
        normalized_cache_kpi: payload.normalized_cache_kpi,
        rolling_actual_hit_rate: payload.rolling_actual_hit_rate || 0,
        rolling_normalized_cache_kpi: payload.rolling_normalized_cache_kpi,
        rolling_n: payload.rolling_n || 0,
        input_total: payload.input_total || payload.total_context || 0,
        total_context: payload.total_context || 0,
        billed_input_equiv: payload.billed_input_equiv || 0,
        saved_pct: payload.saved_pct || 0,
        billing_profile: payload.billing_profile || '',
        cache_read_billing_multiplier: payload.cache_read_billing_multiplier || 0,
        cache_create_billing_multiplier: payload.cache_create_billing_multiplier || 0,
        request_route: payload.request_route || '', request_kind: payload.request_kind || '', logical_turn_id: payload.logical_turn_id || '',
        hot99_status: payload.hot99_status || 'N/A', hot99_rate: payload.hot99_rate,
        hot99_eligible_count: payload.hot99_eligible_count || 0,
        session_context: payload.session_context || existing.session_context || {},
        last_ts: payload.ts,
      });
      renderContextLive();
      renderScopes();
      appendEvent(`cache_stats ${payload.scope} actual=${pct(payload.actual_hit_rate == null ? payload.hit_ratio : payload.actual_hit_rate)} input=${fmt(payload.input_total || payload.total_context)}`);
    } else if (payload.type === 'session_context') {
      const existing = scopeMap.get(payload.scope || 'unknown') || {};
      scopeMap.set(payload.scope || 'unknown', { ...existing, model: payload.model || existing.model || '', session_context: payload.session_context || {}, last_ts: payload.ts || existing.last_ts || 0 });
      renderContextLive();
      renderScopes();
      appendEvent(`session_context ${payload.scope} retained=${fmt((payload.session_context || {}).retained_input_tokens)}`);
    } else if (payload.type === 'stream_start') {
      activeStreams.set(payload.stream_id, { text: '', model: payload.model, started: payload.ts });
      renderStreams();
      appendEvent(`stream_start ${payload.model} ${payload.stream_id}`);
    } else if (payload.type === 'stream_delta') {
      const s = activeStreams.get(payload.stream_id);
      if (s) { s.text += payload.delta_text || ''; renderStreams(); }
    } else if (payload.type === 'stream_end') {
      const s = activeStreams.get(payload.stream_id);
      if (s) {
        // 把这条对话沉淀到 dialog 堆栈, 旧的会被挤上去
        appendDialog({
          model: s.model || '',
          stream_id: payload.stream_id,
          started: s.started,
          ended_ts: payload.ts,
          duration_s: payload.duration_s || 0,
          text: s.text || '',
        });
      }
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
