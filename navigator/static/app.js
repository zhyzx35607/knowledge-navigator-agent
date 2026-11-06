const $ = (selector) => document.querySelector(selector);
let documents = [], entries = [], editing = null;
let resolveConfirmation = null;
const selectedDocs = new Set(), selectedEntries = new Set();
async function api(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { 'Content-Type': 'application/json', ...options.headers } });
  if (response.status === 204) return null;
  const body = await response.json();
  if (!response.ok) {
    const error = new Error(typeof body.detail === 'string' ? body.detail : body.detail?.message || '请求数据无效，请检查输入。');
    error.trace = body.detail?.trace; throw error;
  }
  return body;
}
function node(tag, text, className) { const el = document.createElement(tag); if (text) el.textContent = text; if (className) el.className = className; return el; }
function confirmAction(message, action = '确认打开') {
  $('#confirm-message').textContent = message; $('#confirm-action').textContent = action;
  $('#confirm-dialog').returnValue = 'cancel'; $('#confirm-dialog').showModal();
  return new Promise(resolve => { resolveConfirmation = resolve; });
}
$('#confirm-dialog').addEventListener('close', () => {
  if (resolveConfirmation) resolveConfirmation($('#confirm-dialog').returnValue === 'confirmed');
  resolveConfirmation = null;
});
function checkbox(id, title, selected, onChange) {
  const label = node('label'), input = document.createElement('input'); input.type = 'checkbox'; input.value = id; input.checked = selected;
  input.addEventListener('change', () => onChange(input.checked)); label.append(input, node('span', title)); return label;
}
function renderLibrary() {
  $('#documents').replaceChildren();
  for (const doc of documents) {
    const row = node('div', '', 'document-row'); row.append(checkbox(doc.id, doc.title, selectedDocs.has(doc.id), value => value ? selectedDocs.add(doc.id) : selectedDocs.delete(doc.id)));
    const edit = node('button', '编辑', 'edit-doc'); edit.setAttribute('aria-label', `编辑 ${doc.title}`); edit.onclick = () => openEditor(doc); row.append(edit); $('#documents').append(row);
  }
  if (!documents.length) $('#documents').append(node('p', '还没有文档，点击 ＋ 添加。', 'muted compact'));
  $('#entries').replaceChildren();
  for (const entry of entries) $('#entries').append(checkbox(entry.id, entry.title, selectedEntries.has(entry.id), value => value ? selectedEntries.add(entry.id) : selectedEntries.delete(entry.id)));
  $('#document-count').textContent = `${documents.length} 份文档 · ${entries.length} 个工作入口`;
}
async function loadLibrary(initial = false) {
  [documents, entries] = await Promise.all([api('/api/documents'), api('/api/entries')]);
  if (initial) { documents.forEach(d => selectedDocs.add(d.id)); entries.forEach(e => selectedEntries.add(e.id)); }
  const existing = new Set(documents.map(d => d.id)); for (const id of selectedDocs) if (!existing.has(id)) selectedDocs.delete(id);
  renderLibrary();
}
function renderCitation(item, index) {
  const card = node('article', '', 'citation'), head = node('header'); head.append(node('span', `${index + 1}. ${item.title}`), node('small', `v${item.version}`));
  card.append(head, node('blockquote', item.quote || item.body));
  if (item.citation_id) card.append(node('small', `来源 ${item.document_id} · ${item.citation_id} · SHA256 ${item.digest.slice(0, 12)}`, 'source-meta'));
  return card;
}
function renderTrace(trace) {
  $('#trace').replaceChildren(...trace.map(item => node('li', item.detail + (Number.isFinite(item.model_ms) ? ` · 模型耗时 ${item.model_ms} ms` : ''))));
}
async function search(event) {
  event.preventDefault(); $('#error').hidden = true; $('#result').hidden = true; $('#welcome').hidden = true; $('#workspace').hidden = true;
  $('#search-button').disabled = true; $('#search-button').textContent = '检索中…';
  try {
    const result = await api('/api/search', { method: 'POST', body: JSON.stringify({ query: $('#query').value, mode: $('#mode').value, document_ids: [...selectedDocs], entry_ids: [...selectedEntries] }) });
    $('#answer').textContent = result.answer;
    $('#result-mode').textContent = result.model_called ? `Agent 检索 · ${result.model_calls} 步 · ${result.status === 'matched' ? '已校验引用' : '证据不足'}` : '真实本地检索 · 未调用模型';
    $('#citations').replaceChildren(...result.citations.map(renderCitation)); $('#recommendations').replaceChildren();
    if (result.entries.length) {
      $('#recommendations').append(node('p', '这些入口由当前引用文档支持：', 'muted compact'));
      for (const entry of result.entries) { const button = node('button', `${entry.title} ↗`, 'entry-button'); button.onclick = async () => { if (await confirmAction(`打开“${entry.title}”？\n${entry.description}`)) location.href = entry.path; }; $('#recommendations').append(button); }
    }
    renderTrace(result.trace); $('#result').hidden = false;
  } catch (error) {
    $('#error').textContent = error.message; $('#error').hidden = false;
    if (Array.isArray(error.trace)) {
      $('#answer').textContent = '本次未生成回答。以下为已执行的处理过程。'; $('#result-mode').textContent = 'Agent 检索失败';
      $('#citations').replaceChildren(); $('#recommendations').replaceChildren(); renderTrace(error.trace); $('#result').hidden = false;
    }
  }
  finally { $('#search-button').disabled = false; $('#search-button').textContent = '检索知识 ↗'; }
}
function openEditor(doc = null) {
  editing = doc; $('#dialog-title').textContent = doc ? '编辑文档' : '新增文档'; $('#document-title').value = doc?.title || ''; $('#document-body').value = doc?.body || '';
  $('#delete-document').hidden = !doc; $('#document-error').hidden = true;
  $('#document-entry-options').replaceChildren(...entries.map(entry => checkbox(entry.id, entry.title, doc?.entry_ids.includes(entry.id) || false, () => {})));
  $('#document-dialog').showModal(); $('#document-title').focus();
}
$('#document-form').onsubmit = async (event) => {
  event.preventDefault(); $('#save-document').disabled = true; $('#document-error').hidden = true;
  try {
    const value = { title: $('#document-title').value, body: $('#document-body').value, entry_ids: [...document.querySelectorAll('#document-entry-options input:checked')].map(e => e.value), expected_version: editing?.version || 0 };
    const saved = await api(editing ? `/api/documents/${editing.id}` : '/api/documents', { method: editing ? 'PUT' : 'POST', body: JSON.stringify(value) });
    selectedDocs.add(saved.id); await loadLibrary(); $('#document-dialog').close(); $('#result').hidden = true; $('#welcome').hidden = false;
  } catch (error) { $('#document-error').textContent = error.message; $('#document-error').hidden = false; }
  finally { $('#save-document').disabled = false; }
};
$('#delete-document').onclick = async () => {
  if (!editing || !await confirmAction(`删除“${editing.title}”？索引也会同步删除。`, '确认删除')) return;
  try { await api(`/api/documents/${editing.id}`, { method: 'DELETE' }); selectedDocs.delete(editing.id); await loadLibrary(); $('#document-dialog').close(); $('#result').hidden = true; $('#welcome').hidden = false; }
  catch (error) { $('#document-error').textContent = error.message; $('#document-error').hidden = false; }
};
$('#search-form').onsubmit = search; $('#add-doc').onclick = () => openEditor(); $('#close-dialog').onclick = () => $('#document-dialog').close();
$('#select-all').onclick = () => { documents.forEach(d => selectedDocs.add(d.id)); renderLibrary(); }; $('#select-none').onclick = () => { selectedDocs.clear(); renderLibrary(); };
document.querySelectorAll('[data-query]').forEach(button => button.onclick = () => { $('#query').value = button.dataset.query; $('#search-form').requestSubmit(); });
(async () => {
  try {
    const health = await api('/api/health'); $('#health').textContent = '本地索引已就绪';
    if (!health.model_configured) $('#mode option[value=model]').textContent = 'Agent 检索 · 尚未配置服务';
    await loadLibrary(true);
    if (location.pathname.startsWith('/workspace/')) {
      const id = location.pathname.split('/').pop(), data = await api(`/api/workspace/${encodeURIComponent(id)}`);
      $('#workspace-title').textContent = data.entry.title; $('#workspace-description').textContent = data.entry.description;
      $('#workspace-docs').replaceChildren(...data.documents.map(renderCitation)); $('#workspace').hidden = false; $('#welcome').hidden = true;
    }
  } catch (error) { $('#health').textContent = '连接失败'; $('#error').textContent = error.message; $('#error').hidden = false; }
})();
