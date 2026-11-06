const $ = (selector) => document.querySelector(selector);
let documents = [], entries = [], collections = [], editing = null, pendingFiles = [];
let activeRecord = null, resolveConfirmation = null, continuationParentId = null, searchRevision = 0;
let viewRevision = 0, libraryRevision = 0, historyRevision = 0, editorRevision = 0;
const selectedDocs = new Set(), selectedEntries = new Set();
async function api(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { 'Content-Type': 'application/json', ...options.headers } });
  if (response.status === 204) return null;
  const body = await response.json();
  if (!response.ok) {
    const error = new Error(typeof body.detail === 'string' ? body.detail : body.detail?.message || body.detail?.answer || '请求数据无效，请检查输入。');
    error.record = body.detail?.research_id ? body.detail : null; throw error;
  }
  return body;
}
function node(tag, text, className) { const el = document.createElement(tag); if (text) el.textContent = text; if (className) el.className = className; return el; }
function showError(error, selector = '#error') { $(selector).textContent = error.message; $(selector).hidden = false; }
function confirmAction(message, action = '确认打开') {
  if (resolveConfirmation) return Promise.resolve(false);
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
function visibleDocuments() {
  const filter = $('#collection-filter').value;
  return filter === 'all' ? documents : documents.filter(doc => (doc.collection_id || 'unfiled') === filter);
}
function collectionOptions(select, value = '', includeAll = false) {
  const options = [];
  if (includeAll) { const option = node('option', '全部资料'); option.value = 'all'; options.push(option); }
  const unfiled = node('option', '未归档'); unfiled.value = includeAll ? 'unfiled' : ''; options.push(unfiled);
  for (const item of collections) { const option = node('option', `${item.name} (${item.document_count})`); option.value = item.id; options.push(option); }
  select.replaceChildren(...options); select.value = options.some(option => option.value === value) ? value : (includeAll ? 'all' : '');
}
function renderLibrary() {
  $('#documents').replaceChildren();
  const visible = visibleDocuments();
  for (const doc of visible) {
    const row = node('div', '', 'document-row'); row.append(checkbox(doc.id, doc.title, selectedDocs.has(doc.id), value => value ? selectedDocs.add(doc.id) : selectedDocs.delete(doc.id)));
    const edit = node('button', '编辑', 'edit-doc'); edit.setAttribute('aria-label', `编辑 ${doc.title}`); edit.onclick = () => openEditor(doc); row.append(edit); $('#documents').append(row);
  }
  if (!visible.length) $('#documents').append(node('p', '集合中还没有资料，可导入文件或点击 ＋ 添加。', 'muted compact'));
  $('#entries').replaceChildren();
  for (const entry of entries) $('#entries').append(checkbox(entry.id, entry.title, selectedEntries.has(entry.id), value => value ? selectedEntries.add(entry.id) : selectedEntries.delete(entry.id)));
  $('#document-count').textContent = `${documents.length} 份文档 · ${collections.length} 个集合`;
}
async function loadLibrary(initial = false) {
  const revision = ++libraryRevision;
  const data = await Promise.all([api('/api/documents'), api('/api/entries'), api('/api/collections')]);
  if (revision !== libraryRevision) return;
  [documents, entries, collections] = data;
  collectionOptions($('#collection-filter'), $('#collection-filter').value || 'all', true);
  if (initial) { documents.forEach(doc => selectedDocs.add(doc.id)); entries.forEach(entry => selectedEntries.add(entry.id)); }
  const existing = new Set(documents.map(doc => doc.id)); for (const id of selectedDocs) if (!existing.has(id)) selectedDocs.delete(id);
  renderLibrary();
}
function renderCitation(item, index) {
  const card = node('article', '', `citation${item.stale ? ' stale' : ''}`), head = node('header');
  head.append(node('span', `${index + 1}. ${item.title}`), node('small', `v${item.version}${item.stale ? ' · 已过期' : ''}`));
  card.append(head, node('blockquote', item.quote || item.body));
  if (item.stale) card.append(node('p', `${item.stale_reason}。这里保留的是研究当时的原文。`, 'stale-note'));
  if (item.citation_id) card.append(node('small', `来源 ${item.document_id} · ${item.citation_id} · SHA256 ${item.digest.slice(0, 12)}`, 'source-meta'));
  return card;
}
function renderResult(result, historical = false) {
  activeRecord = result;
  $('#result-title').textContent = historical ? '研究记录' : '检索结果';
  $('#answer').textContent = result.answer || result.message || '本次未生成回答。';
  $('#result-mode').textContent = result.status === 'failed' ? '检索失败 · 已保存动作摘要' : result.model_called ? `Agent · ${result.model_calls} 步` : '本地检索 · 未调用模型';
  $('#record-meta').textContent = `${result.query} · ${new Date(result.recorded_at).toLocaleString()} · ${result.scope_document_ids.length} 份资料 · ${result.retrieval?.path || result.retrieval?.requested || 'bm25'}${result.retrieval?.fallback_reason ? '（未配置向量，仅 BM25）' : ''}${result.parent_research_id ? ' · 已使用上一轮公开上下文' : ''}`;
  $('#freshness').hidden = !historical;
  $('#freshness').className = result.evidence_status === 'stale' ? 'freshness stale-note' : 'freshness';
  $('#freshness').textContent = result.evidence_status === 'stale' ? `${result.stale_citation_count} 条引用已过期。回答是历史快照，请重新检索后使用。` : result.evidence_status === 'current' ? '引用与当前文档一致；回答保留保存时的内容。' : '这份记录没有可核对的引用证据。';
  $('#citations').replaceChildren(...(result.citations || []).map(renderCitation)); $('#recommendations').replaceChildren();
  if (result.entries?.length && result.evidence_status !== 'stale') {
    $('#recommendations').append(node('p', '引用文档关联的工作入口：', 'muted compact'));
    for (const entry of result.entries) { const button = node('button', `${entry.title} ↗`, 'entry-button'); button.onclick = async () => { if (await confirmAction(`打开“${entry.title}”？\n${entry.description}`)) location.href = entry.path; }; $('#recommendations').append(button); }
  }
  $('#trace').replaceChildren(...(result.trace || []).map(item => node('li', item.detail + (Number.isFinite(item.model_ms) ? ` · 模型 ${item.model_ms} ms` : '') + (Number.isFinite(item.tool_ms) ? ` · 工具 ${item.tool_ms} ms` : '') + (item.retrieval ? ` · 路径 ${item.retrieval.path}` : ''))));
  $('#record-actions').hidden = !result.research_id;
  for (const format of ['md', 'json']) $(`#export-${format}`).href = `/api/research/${encodeURIComponent(result.research_id)}/export?format=${format}`;
  $('#result').hidden = false; $('#welcome').hidden = true; $('#workspace').hidden = true;
}
async function loadHistory() {
  const revision = ++historyRevision, records = await api('/api/research');
  if (revision !== historyRevision) return;
  $('#history-count').textContent = `${records.length} 条`;
  $('#history-list').replaceChildren(...records.map(record => {
    const button = node('button', '', 'history-item');
    button.append(node('strong', record.query), node('small', `${new Date(record.created_at).toLocaleString()} · ${record.mode === 'local' ? '本地' : 'Agent'} · ${{ matched: '有证据', not_found: '未命中', failed: '失败' }[record.status]}`));
    button.onclick = () => openRecord(record.id); return button;
  }));
  if (!records.length) $('#history-list').append(node('p', '完成一次检索后，记录会出现在这里。', 'muted compact'));
}
async function openRecord(id) {
  const revision = ++viewRevision; $('#error').hidden = true;
  try { const record = await api(`/api/research/${encodeURIComponent(id)}`); if (revision === viewRevision) renderResult(record, true); }
  catch (error) { if (revision === viewRevision) showError(error); }
}
async function search(event, originalScope = null) {
  event?.preventDefault();
  const requestRevision = ++searchRevision;
  const revision = ++viewRevision; activeRecord = null; $('#error').hidden = true; $('#result').hidden = true; $('#welcome').hidden = true; $('#workspace').hidden = true;
  $('#search-button').disabled = true; $('#search-button').textContent = '检索中…';
  const filter = $('#collection-filter').value;
  const payload = originalScope || { query: $('#query').value, mode: $('#mode').value, document_ids: [...selectedDocs], entry_ids: [...selectedEntries], collection_ids: filter === 'all' ? null : [filter], retrieval_method: $('#retrieval-method').value, parent_research_id: continuationParentId };
  try {
    const result = await api('/api/search', { method: 'POST', body: JSON.stringify(payload) });
    if (revision === viewRevision) renderResult(result);
  } catch (error) { if (revision === viewRevision) { showError(error); if (error.record) renderResult(error.record); } }
  finally {
    // Another history selection may own the visible result while this request finishes.
    if (requestRevision === searchRevision) { $('#search-button').disabled = false; $('#search-button').textContent = '检索知识 ↗'; }
    loadHistory().catch(error => { if (revision === viewRevision) showError(error); });
  }
}
async function refreshEvidence(expectedRevision) { if (activeRecord && expectedRevision === viewRevision) await openRecord(activeRecord.research_id); }
function openEditor(doc = null) {
  ++editorRevision; editing = doc; $('#dialog-title').textContent = doc ? '编辑文档' : '新增文档'; $('#document-title').value = doc?.title || ''; $('#document-body').value = doc?.body || '';
  const filter = $('#collection-filter').value;
  collectionOptions($('#document-collection'), doc ? doc.collection_id || '' : ['all', 'unfiled'].includes(filter) ? '' : filter);
  $('#delete-document').hidden = !doc; $('#document-error').hidden = true;
  $('#document-entry-options').replaceChildren(...entries.map(entry => checkbox(entry.id, entry.title, doc?.entry_ids.includes(entry.id) || false, () => {})));
  $('#document-dialog').showModal(); $('#document-title').focus();
}
$('#document-form').onsubmit = async (event) => {
  event.preventDefault(); const revision = editorRevision, current = editing, evidenceRevision = viewRevision;
  $('#save-document').disabled = true; $('#document-error').hidden = true;
  try {
    const value = { title: $('#document-title').value, body: $('#document-body').value, collection_id: $('#document-collection').value || null, entry_ids: [...document.querySelectorAll('#document-entry-options input:checked')].map(el => el.value), expected_version: current?.version || 0 };
    const saved = await api(current ? `/api/documents/${current.id}` : '/api/documents', { method: current ? 'PUT' : 'POST', body: JSON.stringify(value) });
    selectedDocs.add(saved.id); await loadLibrary(); if (revision === editorRevision) $('#document-dialog').close(); await refreshEvidence(evidenceRevision);
  } catch (error) { if (revision === editorRevision) showError(error, '#document-error'); }
  finally { $('#save-document').disabled = false; }
};
$('#delete-document').onclick = async () => {
  const current = editing, revision = editorRevision, evidenceRevision = viewRevision;
  if (!current || !await confirmAction(`删除“${current.title}”？研究记录会保留原文并标记过期。`, '确认删除')) return;
  try { await api(`/api/documents/${current.id}`, { method: 'DELETE' }); selectedDocs.delete(current.id); await loadLibrary(); if (revision === editorRevision) $('#document-dialog').close(); await refreshEvidence(evidenceRevision); }
  catch (error) { showError(error, '#document-error'); }
};
function chooseImport(files) {
  pendingFiles = [...files]; $('#import-error').hidden = true;
  $('#import-preview').replaceChildren(...pendingFiles.map(file => node('li', `${file.name} · ${(file.size / 1024).toFixed(1)} KiB`)));
  const filter = $('#collection-filter').value; collectionOptions($('#import-collection'), ['all', 'unfiled'].includes(filter) ? '' : filter);
  if (!$('#import-dialog').open) $('#import-dialog').showModal();
}
async function encodeFile(file) {
  const bytes = new Uint8Array(await file.arrayBuffer()); let binary = '';
  for (let index = 0; index < bytes.length; index += 8192) binary += String.fromCharCode(...bytes.subarray(index, index + 8192));
  return { filename: file.name, content_base64: btoa(binary) };
}
$('#import-form').onsubmit = async event => {
  event.preventDefault(); $('#import-error').hidden = true; $('#submit-import').disabled = true; $('#choose-files').disabled = true;
  try {
    const files = [...pendingFiles];
    if (!files.length || files.length > 20) throw new Error('请选择 1–20 个文件；整批未导入。');
    if (files.some(file => file.size > 256 * 1024) || files.reduce((sum, file) => sum + file.size, 0) > 2 * 1024 * 1024) throw new Error('单文件最多 256 KiB，整批最多 2 MiB；整批未导入。');
    const result = await api('/api/documents/import', { method: 'POST', body: JSON.stringify({ files: await Promise.all(files.map(encodeFile)), collection_id: $('#import-collection').value || null }) });
    result.imported.forEach(doc => selectedDocs.add(doc.id)); await loadLibrary(); $('#import-dialog').close(); $('#health').textContent = `已导入 ${result.count} 份资料`;
  } catch (error) { showError(error, '#import-error'); }
  finally { $('#submit-import').disabled = false; $('#choose-files').disabled = false; $('#file-input').value = ''; }
};
function renderCollections() {
  $('#collection-list').replaceChildren(...collections.map(collection => {
    const row = node('div', '', 'collection-row'), input = document.createElement('input'); input.value = collection.name; input.maxLength = 80; input.setAttribute('aria-label', `重命名 ${collection.name}`);
    const save = node('button', '保存', 'entry-button'), remove = node('button', '删除', 'danger');
    save.onclick = async () => { try { await api(`/api/collections/${collection.id}`, { method: 'PUT', body: JSON.stringify({ name: input.value }) }); await loadLibrary(); renderCollections(); } catch (error) { showError(error, '#collection-error'); } };
    remove.onclick = async () => { if (!await confirmAction(`删除集合“${collection.name}”？其中 ${collection.document_count} 份文档会保留并移到未归档。`, '删除集合')) return; try { await api(`/api/collections/${collection.id}`, { method: 'DELETE' }); await loadLibrary(); renderCollections(); } catch (error) { showError(error, '#collection-error'); } };
    row.append(input, node('span', `${collection.document_count} 份`, 'muted compact'), save, remove); return row;
  }));
}
$('#collection-form').onsubmit = async event => { event.preventDefault(); $('#collection-error').hidden = true; try { await api('/api/collections', { method: 'POST', body: JSON.stringify({ name: $('#collection-name').value }) }); $('#collection-name').value = ''; await loadLibrary(); renderCollections(); } catch (error) { showError(error, '#collection-error'); } };
$('#manage-collections').onclick = () => { $('#collection-error').hidden = true; renderCollections(); $('#collections-dialog').showModal(); };
$('#close-collections').onclick = () => $('#collections-dialog').close();
$('#collection-filter').onchange = () => { selectedDocs.clear(); visibleDocuments().forEach(doc => selectedDocs.add(doc.id)); renderLibrary(); };
$('#import-files').onclick = $('#choose-files').onclick = () => $('#file-input').click();
$('#file-input').onchange = event => { if (event.target.files.length) chooseImport(event.target.files); };
$('#close-import').onclick = () => { if (!$('#submit-import').disabled) $('#import-dialog').close(); };
$('#import-dialog').addEventListener('cancel', event => { if ($('#submit-import').disabled) event.preventDefault(); });
const dropZone = $('#drop-zone');
for (const type of ['dragenter', 'dragover']) dropZone.addEventListener(type, event => { event.preventDefault(); dropZone.classList.add('dragging'); });
for (const type of ['dragleave', 'drop']) dropZone.addEventListener(type, event => { event.preventDefault(); dropZone.classList.remove('dragging'); });
dropZone.addEventListener('drop', event => { if (event.dataTransfer.files.length) chooseImport(event.dataTransfer.files); });
$('#delete-record').onclick = async () => {
  const id = activeRecord?.research_id; if (!id || !await confirmAction('删除这份研究记录？原始文档会保留。', '删除记录')) return;
  try { await api(`/api/research/${id}`, { method: 'DELETE' }); if (activeRecord?.research_id === id) { ++viewRevision; activeRecord = null; $('#result').hidden = true; $('#welcome').hidden = false; } await loadHistory(); } catch (error) { showError(error); }
};
$('#rerun-record').onclick = () => {
  const record = activeRecord; if (!record) return;
  $('#query').value = record.query; $('#mode').value = record.mode;
  search(null, { query: record.query, mode: record.mode, document_ids: record.scope_document_ids.filter(id => documents.some(doc => doc.id === id)), entry_ids: record.scope_entry_ids, collection_ids: null, retrieval_method: record.retrieval?.requested || 'bm25' });
};
$('#continue-record').onclick = () => {
  if (!activeRecord) return;
  continuationParentId = activeRecord.research_id; $('#continuation-label').textContent = `继续：${activeRecord.query}。范围会与上一轮取交集，过期引用会被拒绝。`;
  $('#continuation-banner').hidden = false; $('#query').value = ''; $('#query').focus(); $('#search-form').scrollIntoView({ behavior: 'smooth', block: 'center' });
};
$('#end-continuation').onclick = () => { continuationParentId = null; $('#continuation-banner').hidden = true; };
$('#search-form').onsubmit = search; $('#add-doc').onclick = () => openEditor(); $('#close-dialog').onclick = () => $('#document-dialog').close();
$('#select-all').onclick = () => { visibleDocuments().forEach(doc => selectedDocs.add(doc.id)); renderLibrary(); }; $('#select-none').onclick = () => { selectedDocs.clear(); renderLibrary(); };
document.querySelectorAll('[data-query]').forEach(button => button.onclick = () => { $('#query').value = button.dataset.query; $('#search-form').requestSubmit(); });
async function initialize() {
  try {
    const health = await api('/api/health'); $('#health').textContent = '本地索引已就绪';
    if (!health.model_configured) $('#mode option[value=model]').textContent = 'Agent 检索 · 尚未配置服务';
    if (!health.embedding_configured) $('#retrieval-method option[value=hybrid]').textContent = '混合检索 · 未配置向量时仅 BM25';
    await Promise.all([loadLibrary(true), loadHistory()]);
    if (location.pathname.startsWith('/workspace/')) {
      const revision = viewRevision, id = location.pathname.split('/').pop(), data = await api(`/api/workspace/${encodeURIComponent(id)}`);
      if (revision !== viewRevision) return;
      $('#workspace-title').textContent = data.entry.title; $('#workspace-description').textContent = data.entry.description;
      $('#workspace-docs').replaceChildren(...data.documents.map(renderCitation)); $('#workspace').hidden = false; $('#welcome').hidden = true;
    }
  } catch (error) { $('#health').textContent = '连接失败'; showError(error); }
}
if (!globalThis.__NAVIGATOR_TEST__) initialize();
