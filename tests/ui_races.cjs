const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

function harness() {
  class Element {
    constructor() { this.children = []; this.value = ''; this.hidden = false; this.dataset = {}; this.handlers = {}; this.classList = { add() {}, remove() {} }; }
    append(...items) { this.children.push(...items); }
    replaceChildren(...items) { this.children = items; }
    setAttribute(key, value) { this[key] = value; }
    addEventListener(type, handler) { this.handlers[type] = handler; }
    showModal() { this.open = true; }
    close() { this.open = false; }
    focus() {}
    scrollIntoView() {}
  }
  const elements = new Map(), pending = new Map();
  const element = selector => { if (!elements.has(selector)) elements.set(selector, new Element()); return elements.get(selector); };
  const context = vm.createContext({
    document: { querySelector: element, querySelectorAll: () => [], createElement: () => new Element() },
    location: { pathname: '/' }, __NAVIGATOR_TEST__: true, console,
    fetch: (url) => {
      if (url === '/api/research') return Promise.resolve({ ok: true, status: 200, json: async () => [] });
      return new Promise(resolve => pending.set(url, data => resolve({ ok: true, status: 200, json: async () => data })));
    },
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../navigator/static/app.js'), 'utf8'), context);
  for (const [selector, value] of [['#collection-filter', 'all'], ['#query', 'query'], ['#mode', 'local'], ['#retrieval-method', 'bm25']]) element(selector).value = value;
  return { context, element, pending };
}
const record = id => ({ research_id: id, recorded_at: '2026-11-06T00:00:00Z', query: id,
  answer: `Answer ${id}`, mode: 'local', status: 'matched', scope_document_ids: [], scope_entry_ids: [],
  citations: [], entries: [], trace: [], evidence_status: 'none' });

test('late history response cannot replace the selected record or its exports', async () => {
  const { context, element, pending } = harness();
  const first = vm.runInContext('openRecord("A")', context);
  const second = vm.runInContext('openRecord("B")', context);
  pending.get('/api/research/B')(record('B')); await second;
  pending.get('/api/research/A')(record('A')); await first;
  assert.equal(element('#answer').textContent, 'Answer B');
  assert.match(element('#export-md').href, /\/B\/export/);
  assert.equal(vm.runInContext('activeRecord.research_id', context), 'B');
});

test('late search does not overwrite history opened while the search was running', async () => {
  const { context, element, pending } = harness();
  const searching = vm.runInContext('search(null)', context);
  const opening = vm.runInContext('openRecord("B")', context);
  pending.get('/api/research/B')(record('B')); await opening;
  pending.get('/api/search')(record('A')); await searching;
  assert.equal(element('#answer').textContent, 'Answer B');
  assert.match(element('#export-json').href, /\/B\/export/);
  assert.equal(element('#search-button').disabled, false);
});

test('historical content is rendered as text and stale records cannot navigate from old entries', () => {
  const { context, element } = harness();
  context.result = { ...record('C'), answer: '<img src=x onerror=alert(1)>', evidence_status: 'stale', stale_citation_count: 1,
    entries: [{ id: 'journal', title: 'Journal', path: '/workspace/journal' }],
    citations: [{ title: '<script>bad()</script>', quote: '<b>literal</b>', version: 1, citation_id: 'a:v1:0', document_id: 'a', digest: '123456789012', stale: true, stale_reason: '文档已删除' }] };
  vm.runInContext('renderResult(result, true)', context);
  assert.equal(element('#answer').textContent, context.result.answer);
  assert.equal(element('#recommendations').children.length, 0);
  assert.match(element('#freshness').textContent, /过期/);
});

test('dropping files opens the import review with every filename', () => {
  const { context, element } = harness();
  const files = [{ name: 'alpha.md', size: 100 }, { name: 'beta.txt', size: 200 }];
  let prevented = false;
  element('#drop-zone').handlers.drop({ preventDefault() { prevented = true; }, dataTransfer: { files } });
  assert.equal(element('#import-dialog').open, true);
  assert.equal(element('#import-preview').children.length, 2);
  assert.match(element('#import-preview').children[0].textContent, /alpha.md/);
  assert.equal(vm.runInContext('pendingFiles.length', context), 2);
});
