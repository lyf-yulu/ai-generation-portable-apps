import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';


function makeElement(extra = {}) {
  return {
    innerHTML: '', textContent: '', value: '', style: {}, dataset: {}, elements: [],
    classList: { add() {}, remove() {}, toggle() {} },
    addEventListener() {}, appendChild() {}, removeChild() {}, reset() {},
    querySelector() { return null; }, querySelectorAll() { return []; }, closest() { return null; },
    ...extra,
  };
}


function loadApp({ script, path, factoryName, formId, resultsId, eventsId }) {
  const form = makeElement();
  const results = makeElement();
  const events = makeElement();
  const elements = { [formId]: form, [resultsId]: results, [eventsId]: events };
  const storage = new Map();
  const document = {
    visibilityState: 'visible',
    getElementById(id) { return elements[id] || null; },
    querySelector(selector) { return selector === '#' + formId ? form : null; },
    querySelectorAll() { return []; },
    addEventListener() {},
    createElement() { return makeElement(); },
    head: makeElement(), body: makeElement(),
  };
  const localStorage = {
    getItem(key) { return storage.has(key) ? storage.get(key) : null; },
    setItem(key, value) { storage.set(key, String(value)); },
    removeItem(key) { storage.delete(key); },
  };
  const window = {
    location: { pathname: path, search: '' },
    _activeWorkspaceId: 'ws-a',
    _dlProgress: {},
  };
  const sandbox = {
    window, document, localStorage,
    PetiteVue: { createApp() { return { mount() {} }; } },
    URL, URLSearchParams, Blob,
    FormData: class FormData { set() {} delete() {} },
    DataTransfer: class DataTransfer {}, Event: class Event {},
    crypto: { randomUUID: () => 'generated-workspace' },
    fetch: async () => ({ ok: true, status: 200, json: async () => ({}) }),
    console, setTimeout, setInterval: () => 1, clearInterval() {},
    alert() {}, confirm: () => true,
  };
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(script, 'utf8'), sandbox);
  return { app: window[factoryName](), window, results, events };
}


function job(workspaceId, message) {
  return {
    id: 'job-' + workspaceId,
    workspace_id: workspaceId,
    status: 'succeeded', done: 1, total: 1,
    events: [{ time: '10:00:00', message }],
    results: [], errors: [],
  };
}


function exercise(config) {
  const { app, window, results } = loadApp(config);
  app.tabs = [{ id: 'ws-a', name: 'A', running: false }];
  app.activeTabId = 'ws-a';
  app._tabStateCache = {};
  app.savedMedia = {};
  app.outputDir = 'A directory';
  app.dirHandle = { name: 'A handle' };
  app.autoDownload = true;
  results.innerHTML = 'OLD RESULT FROM A';

  app.newTab();
  assert.equal(results.innerHTML, '', config.prefix + ' new topic must start with an empty result panel');
  assert.equal(app.outputDir, '', config.prefix + ' new topic must not inherit the old output directory');
  assert.equal(app.dirHandle, null, config.prefix + ' new topic must not inherit the old directory handle');
  assert.equal(app.autoDownload, false, config.prefix + ' new topic must not inherit old auto-download state');

  app.tabs = [
    { id: 'ws-a', name: 'A', running: false },
    { id: 'ws-b', name: 'B', running: false },
  ];
  app.activeTabId = 'ws-b';
  window._activeWorkspaceId = 'ws-b';
  app._tabStateCache['ws-b'] = { _latestJob: job('ws-a', 'WRONG A RESULT') };
  if (config.factoryName === 'SeedanceApp') app.loadPreset = () => {};
  else app.loadInitialPreset = () => {};
  results.innerHTML = 'stale';

  app.loadTargetTabState();
  assert.equal(results.innerHTML, '', config.prefix + ' must reject a cached result owned by another topic');

  const bHandle = { name: 'B handle' };
  app._tabStateCache['ws-b'] = {
    _latestJob: job('ws-b', 'RIGHT B RESULT'),
    outputDir: 'B directory',
    dirHandle: bHandle,
    autoDownload: true,
  };
  app.loadTargetTabState();
  assert.match(results.innerHTML, /RIGHT B RESULT/, config.prefix + ' must restore a result owned by the active topic');
  assert.equal(app.outputDir, 'B directory', config.prefix + ' must restore the target topic output directory');
  assert.equal(app.dirHandle, bHandle, config.prefix + ' must restore the target topic directory handle');
  assert.equal(app.autoDownload, true, config.prefix + ' must restore the target topic auto-download state');

  if (config.factoryName === 'SeedanceApp') {
    app.jobs = [job('ws-a', 'history A'), job('ws-b', 'history B')];
    app.jobsLimit = 20;
    app.activeTabId = 'ws-a';
    assert.deepEqual(
      Array.from(app.visibleJobs(), (item) => item.workspace_id),
      ['ws-a', 'ws-b'],
      'seedance generation history must remain user-level across topics',
    );
  }
}


exercise({
  script: 'seedance/static/app.js', path: '/seedance/index.html', factoryName: 'SeedanceApp',
  prefix: 'seedance', formId: 'sd-form', resultsId: 'sd-results', eventsId: 'sd-events',
});
exercise({
  script: 'nano-banana/static/app.js', path: '/nano-banana/index.html', factoryName: 'NanoBananaApp',
  prefix: 'nano', formId: 'nb-form', resultsId: 'nb-results', eventsId: 'nb-events',
});

console.log('topic cache isolation: ok');
