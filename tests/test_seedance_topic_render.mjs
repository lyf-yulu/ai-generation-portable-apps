import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const eventsEl = { textContent: '主题状态负责渲染此处' };
const resultsEl = { innerHTML: '' };
const document = {
  getElementById(id) {
    if (id === 'sd-events') return eventsEl;
    if (id === 'sd-results') return resultsEl;
    return null;
  },
  querySelector() { return null; },
  querySelectorAll() { return []; },
  addEventListener() {},
  createElement() { return { style: {}, classList: { add() {} }, appendChild() {} }; },
  head: { appendChild() {} },
  body: { appendChild() {} },
};
const localStorage = {
  getItem() { return null; },
  setItem() {},
  removeItem() {},
};
const window = {
  location: { pathname: '/seedance/index.html', search: '' },
  _dlProgress: {},
};
const sandbox = {
  window, document, localStorage,
  PetiteVue: { createApp() { return { mount() {} }; } },
  URL, URLSearchParams, Blob,
  FormData: class FormData {},
  DataTransfer: class DataTransfer {},
  Event: class Event {},
  crypto: { randomUUID: () => 'workspace-test' },
  fetch: async () => ({ ok: true, status: 200, json: async () => ({}) }),
  console, setTimeout, setInterval: () => 1, clearInterval() {},
  alert() {}, confirm: () => true,
};

vm.createContext(sandbox);
vm.runInContext(fs.readFileSync('seedance/static/app.js', 'utf8'), sandbox);

const app = window.SeedanceApp();
app._renderJobToDom({
  status: 'running',
  done: 1,
  total: 2,
  events: [{ time: '10:00:00', message: '任务一运行中' }],
  results: [],
  errors: [],
});

assert.match(resultsEl.innerHTML, /任务一运行中/, '结果面板仍应刷新任务进度');
assert.equal(
  eventsEl.textContent,
  '主题状态负责渲染此处',
  '结果重绘不能直接改写由主题响应式状态管理的日志框',
);

console.log('seedance topic render: ok');
