import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';


function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}


function makeElement(extra = {}) {
  return {
    innerHTML: '',
    textContent: '',
    style: {},
    dataset: {},
    classList: { add() {}, remove() {}, toggle() {} },
    addEventListener() {},
    appendChild() {},
    removeChild() {},
    querySelector() { return null; },
    querySelectorAll() { return []; },
    closest() { return null; },
    reset() {},
    ...extra,
  };
}


async function exerciseSubmitSwitch({ script, path, factoryName, prefix, formId, resultsId, eventsId }) {
  const form = makeElement({ elements: [] });
  const results = makeElement();
  const events = makeElement();
  const elements = { [formId]: form, [resultsId]: results, [eventsId]: events };
  const requests = [];
  const post = deferred();
  const stalePoll = deferred();
  const closedPoll = deferred();

  const document = {
    visibilityState: 'visible',
    getElementById(id) { return elements[id] || null; },
    querySelector(selector) {
      if (selector === '#' + formId) return form;
      return null;
    },
    querySelectorAll() { return []; },
    addEventListener() {},
    createElement() { return makeElement(); },
    head: makeElement(),
    body: makeElement(),
  };
  const storage = new Map();
  const localStorage = {
    getItem(key) { return storage.has(key) ? storage.get(key) : null; },
    setItem(key, value) { storage.set(key, String(value)); },
    removeItem(key) { storage.delete(key); },
  };
  const window = {
    location: { pathname: path, search: '' },
    _activeWorkspaceId: 'ws-a',
    _dlProgress: {},
    isSecureContext: true,
  };
  const terminalResult = factoryName === 'NanoBananaApp'
    ? [{ index: 1, images: [{ download_url: '/api/download/a', filename: 'a.png' }] }]
    : [{ index: 1, download_url: '/api/download/a', filename: 'a.mp4' }];
  const terminalJob = {
    id: 'job-a',
    status: 'succeeded',
    workspace_id: 'ws-a',
    done: 1,
    total: 1,
    events: [{ time: '10:00:00', message: 'A complete' }],
    results: terminalResult,
    errors: [],
  };

  async function fetch(url, options = {}) {
    requests.push({ url: String(url), method: options.method || 'GET', headers: options.headers || {} });
    if ((options.method || 'GET') === 'POST' && String(url).includes('/api/jobs?')) {
      return post.promise;
    }
    if (String(url).includes('/api/jobs/job-a')) {
      return { ok: true, status: 200, json: async () => terminalJob };
    }
    if (String(url).includes('/api/jobs/job-old')) return stalePoll.promise;
    if (String(url).includes('/api/jobs/job-closed')) return closedPoll.promise;
    if (String(url).includes('/api/jobs/job-mismatch')) {
      return { ok: true, status: 200, json: async () => ({ ...terminalJob, id: 'job-mismatch', workspace_id: 'ws-a' }) };
    }
    if (String(url).includes('/api/jobs?')) {
      return { ok: true, status: 200, json: async () => ({ ok: true, jobs: [] }) };
    }
    return { ok: true, status: 200, json: async () => ({}) };
  }

  class FakeFormData {
    constructor() { this.values = new Map(); }
    set(key, value) { this.values.set(key, value); }
    delete(key) { this.values.delete(key); }
  }

  const sandbox = {
    window, document, localStorage,
    PetiteVue: { createApp() { return { mount() {} }; } },
    URL, URLSearchParams, Blob,
    FormData: FakeFormData,
    DataTransfer: class DataTransfer {},
    Event: class Event {},
    crypto: { randomUUID: () => 'legacy-workspace' },
    fetch,
    console,
    setTimeout,
    setInterval: () => 1,
    clearInterval() {},
    alert() {},
    confirm: () => true,
  };

  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(script, 'utf8'), sandbox);
  const app = window[factoryName]();
  app.tabs = [
    { id: 'ws-a', name: 'A', running: false },
    { id: 'ws-b', name: 'B', running: false },
  ];
  app.activeTabId = 'ws-a';
  app._tabStateCache = {};
  app.savedMedia = {};
  app.autoDownload = false;
  app.dirHandle = null;
  app._blobDownload = () => {};
  if (factoryName === 'NanoBananaApp') {
    app.formDataWithSavedMedia = async () => new FakeFormData();
    app.loadActivity = async () => {};
  }

  const submitPromise = app.submit();
  for (let i = 0; i < 8 && requests.length === 0; i++) {
    await new Promise((done) => setTimeout(done, 0));
  }
  assert.ok(requests.length > 0, prefix + ' must send the submit request');
  assert.equal(requests[0].headers['X-Workspace-Id'], 'ws-a', prefix + ' POST must start in ws-a');

  app.activeTabId = 'ws-b';
  window._activeWorkspaceId = 'ws-b';
  app.statusText = 'B idle';
  app.autoDownload = true;
  results.innerHTML = 'B untouched';

  post.resolve({ ok: true, status: 200, json: async () => ({ ok: true, job_id: 'job-a' }) });
  await submitPromise;
  for (let i = 0; i < 8; i++) await new Promise((done) => setTimeout(done, 0));

  const detail = requests.find((request) => request.url.includes('/api/jobs/job-a'));
  assert.ok(detail, prefix + ' must poll the submitted job');
  assert.equal(detail.headers['X-Workspace-Id'], 'ws-a', prefix + ' poll must stay bound to submitting topic');
  assert.equal(app.statusText, 'B idle', prefix + ' background task must not alter active ws-b status');
  assert.equal(results.innerHTML, 'B untouched', prefix + ' background task must not render into ws-b');
  assert.equal(app._tabStateCache['ws-a']?._latestJob?.id, 'job-a', prefix + ' result must be cached under ws-a');
  assert.equal(app._tabStateCache['ws-b']?._latestJob, undefined, prefix + ' ws-b cache must remain isolated');

  // A slower, older task from the same topic must not overwrite the newer task.
  app.activeTabId = 'ws-a';
  window._activeWorkspaceId = 'ws-a';
  results.innerHTML = 'NEWEST RESULT';
  app._tabStateCache['ws-a'] = { _submissionToken: 1, _activeJobId: 'job-old' };
  const oldPending = app.pollJob('job-old', 'ws-a', 1, { autoDownload: false, dirHandle: null, outputDir: '' });
  for (let i = 0; i < 8 && !requests.some((request) => request.url.includes('/job-old')); i++) {
    await new Promise((done) => setTimeout(done, 0));
  }
  const newestJob = { ...terminalJob, id: 'job-new', workspace_id: 'ws-a' };
  app._tabStateCache['ws-a'] = {
    _submissionToken: 2,
    _activeJobId: 'job-new',
    _latestJob: newestJob,
  };
  stalePoll.resolve({ ok: true, status: 200, json: async () => ({ ...terminalJob, id: 'job-old' }) });
  await oldPending;
  assert.equal(results.innerHTML, 'NEWEST RESULT', prefix + ' stale same-topic poll must not redraw the result panel');
  assert.equal(app._tabStateCache['ws-a']._latestJob.id, 'job-new', prefix + ' stale poll must not replace newest cache');

  // Closing a running topic must not allow its late response to recreate cache.
  app._tabStateCache['ws-a'] = { _submissionToken: 3, _activeJobId: 'job-closed' };
  const closedPending = app.pollJob('job-closed', 'ws-a', 3, { autoDownload: false, dirHandle: null, outputDir: '' });
  for (let i = 0; i < 8 && !requests.some((request) => request.url.includes('/job-closed')); i++) {
    await new Promise((done) => setTimeout(done, 0));
  }
  app.tabs = [{ id: 'ws-b', name: 'B', running: false }];
  delete app._tabStateCache['ws-a'];
  app.activeTabId = 'ws-b';
  window._activeWorkspaceId = 'ws-b';
  results.innerHTML = 'B AFTER CLOSE';
  closedPoll.resolve({ ok: true, status: 200, json: async () => ({ ...terminalJob, id: 'job-closed' }) });
  await closedPending;
  assert.equal(app._tabStateCache['ws-a'], undefined, prefix + ' closed topic cache must stay deleted');
  assert.equal(results.innerHTML, 'B AFTER CLOSE', prefix + ' closed topic response must not render into ws-b');

  // Even with a valid job ID, a workspace mismatch is never rendered.
  app._tabStateCache['ws-b'] = { _submissionToken: 4, _activeJobId: 'job-mismatch' };
  results.innerHTML = 'B SAFE';
  await app.pollJob('job-mismatch', 'ws-b', 4, { autoDownload: false, dirHandle: null, outputDir: '' });
  assert.equal(results.innerHTML, 'B SAFE', prefix + ' mismatched job ownership must not reach the DOM');
  assert.equal(app._tabStateCache['ws-b']._latestJob, undefined, prefix + ' mismatched job must not enter cache');
  assert.match(app._tabStateCache['ws-b'].statusText, /主题隔离校验失败/, prefix + ' mismatch must be reported in owner topic');
}


await exerciseSubmitSwitch({
  script: 'seedance/static/app.js',
  path: '/seedance/index.html',
  factoryName: 'SeedanceApp',
  prefix: 'seedance',
  formId: 'sd-form',
  resultsId: 'sd-results',
  eventsId: 'sd-events',
});

await exerciseSubmitSwitch({
  script: 'nano-banana/static/app.js',
  path: '/nano-banana/index.html',
  factoryName: 'NanoBananaApp',
  prefix: 'nano',
  formId: 'nb-form',
  resultsId: 'nb-results',
  eventsId: 'nb-events',
});

console.log('topic async isolation: ok');
