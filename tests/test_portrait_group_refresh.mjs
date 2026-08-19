import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const mounted = {};
const genericElement = {
  style: {},
  addEventListener() {},
  appendChild() {},
  removeChild() {},
  classList: { add() {}, remove() {}, toggle() {} },
};
const document = {
  querySelectorAll() { return []; },
  querySelector() { return null; },
  getElementById() { return genericElement; },
  createElement() { return { ...genericElement, style: {}, classList: genericElement.classList }; },
  head: genericElement,
  body: genericElement,
};

const requests = [];
let createResponse = { ok: true, group_id: 'group-new' };
async function fetch(url, options = {}) {
  requests.push({ url, method: options.method || 'GET' });
  let data;
  if (url === '/api/apps') {
    data = { ok: true, apps: [] };
  } else if (url === '/api/auth/me') {
    data = { ok: true, username: 'tester', role: 'user' };
  } else if (url === '/volcengine-portrait/api/virtual/groups' && options.method === 'POST') {
    data = createResponse;
  } else if (url === '/volcengine-portrait/api/virtual/groups') {
    data = { ok: true, groups: [{ group_id: 'group-new', name: '新建组' }] };
  } else {
    data = { ok: false, error: `unexpected request: ${url}` };
  }
  return { ok: true, status: 200, json: async () => data };
}

const localStorage = {
  values: new Map(),
  getItem(key) { return this.values.get(key) || null; },
  setItem(key, value) { this.values.set(key, value); },
};
const location = {
  protocol: 'https:', pathname: '/', search: '',
  replace() {},
};
const window = { location, _dlProgress: {}, isSecureContext: true };
const sandbox = {
  window, document, localStorage, location, fetch,
  PetiteVue: {
    createApp(components) {
      Object.assign(mounted, components);
      return { mount() {} };
    },
  },
  crypto: { randomUUID: () => 'workspace-test' },
  URL, URLSearchParams, Blob,
  FormData: class FormData {},
  DataTransfer: class DataTransfer {},
  Event: class Event {},
  navigator: {}, console,
  setTimeout: () => 1,
  setInterval: () => 1,
  clearInterval() {},
  alert() {},
  confirm: () => true,
};

vm.createContext(sandbox);
vm.runInContext(fs.readFileSync('portal/static/app.js', 'utf8'), sandbox);

const app = mounted.VolcenginePortraitApp();
app.groupName = '新建组';
await app.createGroup();

assert.equal(app.groupId, 'group-new');
assert.equal(app.assetGroupId, 'group-new');
assert.deepEqual(
  app.groups.map(group => ({ ...group })),
  [{ group_id: 'group-new', name: '新建组' }],
  '创建成功后应自动重新读取组列表',
);
assert.equal(
  requests.filter(request => request.url === '/volcengine-portrait/api/virtual/groups' && request.method === 'GET').length,
  1,
  '创建成功后应发起一次组列表刷新请求',
);

requests.length = 0;
createResponse = { ok: false, error: '创建失败' };
const failedApp = mounted.VolcenginePortraitApp();
await failedApp.createGroup();
assert.equal(failedApp.uploadError, true);
assert.equal(
  requests.some(request => request.url === '/volcengine-portrait/api/virtual/groups' && request.method === 'GET'),
  false,
  '创建失败时不应刷新组列表',
);

console.log('portrait group refresh: ok');
