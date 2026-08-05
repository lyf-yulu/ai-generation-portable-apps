import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';


function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}


async function exercise({ script, path, factoryName, invoke, prefix }) {
  const response = deferred();
  const localStorage = { getItem() { return null; }, setItem() {}, removeItem() {} };
  const document = {
    getElementById() { return null; }, querySelector() { return null; }, querySelectorAll() { return []; },
    addEventListener() {}, createElement() { return { style: {}, classList: { add() {} }, appendChild() {} }; },
    head: { appendChild() {} }, body: { appendChild() {} },
  };
  const window = { location: { pathname: path, search: '' }, _activeWorkspaceId: 'ws-a', _dlProgress: {} };
  const sandbox = {
    window, document, localStorage,
    PetiteVue: { createApp() { return { mount() {} }; } },
    URL, URLSearchParams, Blob,
    FormData: class FormData {}, DataTransfer: class DataTransfer {}, Event: class Event {},
    crypto: { randomUUID: () => 'legacy' },
    fetch: async () => response.promise,
    console, setTimeout, setInterval: () => 1, clearInterval() {}, alert() {}, confirm: () => true,
  };
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(script, 'utf8'), sandbox);
  const app = window[factoryName]();
  app.tabs = [{ id: 'ws-a' }, { id: 'ws-b' }];
  app.activeTabId = 'ws-a';
  app.loadWorkspaceDraft = () => false;
  const applied = [];
  app.applyPreset = (preset) => applied.push(preset);

  const pending = invoke(app);
  await Promise.resolve();
  app.activeTabId = 'ws-b';
  window._activeWorkspaceId = 'ws-b';
  response.resolve({ ok: true, status: 200, json: async () => ({ values: { prompt: 'A prompt' }, media: {} }) });
  await pending;

  assert.deepEqual(applied, [], prefix + ' late ws-a preset must not overwrite active ws-b form');
}


await exercise({
  script: 'seedance/static/app.js', path: '/seedance/index.html', factoryName: 'SeedanceApp', prefix: 'seedance',
  invoke: (app) => app._loadApiPreset('ws-a'),
});
await exercise({
  script: 'nano-banana/static/app.js', path: '/nano-banana/index.html', factoryName: 'NanoBananaApp', prefix: 'nano',
  invoke: (app) => app.loadInitialPreset('ws-a'),
});

console.log('topic preset isolation: ok');
