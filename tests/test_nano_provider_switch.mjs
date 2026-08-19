import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

class FakeFormData {
  constructor(form) {
    this.values = new Map();
    if (form && form.elements) {
      for (const [name, el] of Object.entries(form.elements)) {
        if (el && typeof el.value !== 'undefined' && !el.disabled) this.values.set(name, el.value);
      }
    }
  }
  set(name, value) { this.values.set(name, value); }
  delete(name) { this.values.delete(name); }
  get(name) { return this.values.get(name); }
  has(name) { return this.values.has(name); }
}

const keyInput = {
  name: 'api_key', value: 'original-t8star-key', type: 'password',
  disabled: false, readOnly: false, placeholder: '留空使用本地配置',
};
const seedInput = { name: 'seed', value: '123', type: 'number', disabled: false };
const varySeed = { name: 'vary_seed', value: 'on', type: 'checkbox', disabled: false, checked: true };
const sizeSelect = { name: 'image_size', value: '2K', type: 'select-one', tagName: 'SELECT', options: [] };
const baseUrlInput = { name: 'base_url', value: 'https://ai.t8star.org', type: 'text', readOnly: false };
const form = { elements: { api_key: keyInput, base_url: baseUrlInput, seed: seedInput, vary_seed: varySeed, image_size: sizeSelect } };
const document = {
  getElementById(id) { return id === 'nb-form' ? form : null; },
  querySelector(selector) {
    const match = selector.match(/name="([^"]+)"/);
    return match ? form.elements[match[1]] || null : null;
  },
  querySelectorAll() { return []; },
  addEventListener() {},
  createElement() { return { style: {}, classList: { add() {}, toggle() {} }, appendChild() {} }; },
  head: { appendChild() {} }, body: { appendChild() {} },
};
const localStorage = { getItem() { return null; }, setItem() {}, removeItem() {} };
const window = { location: { pathname: '/nano-banana/index.html' }, _dlProgress: {} };
const sandbox = {
  window, document, localStorage, FormData: FakeFormData,
  PetiteVue: { createApp() { return { mount() {} }; } },
  URL, URLSearchParams, Blob, File: class File {}, fetch: async () => ({}),
  crypto: { randomUUID: () => 'uuid' }, console, setTimeout, setInterval: () => 1,
  clearInterval() {}, alert() {}, confirm: () => true, DataTransfer: class {},
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync('nano-banana/static/app.js', 'utf8'), sandbox);

const app = window.NanoBananaApp();
app.providers = {
  t8star: {
    label: 'T8Star', base_url: 'https://ai.t8star.org',
    image_size_options: ['1K', '2K', '4K'], supports_seed: true,
    models: [{ id: 'old-model' }], defaults: { model: 'old-model', image_size: '2K' },
  },
  volcengine: {
    label: '火山引擎官方', base_url: 'https://ark.cn-beijing.volces.com/api/v3',
    company_key: true, company_key_available: true,
    image_size_options: ['1K', '1.5K', '2K'], supports_seed: false,
    max_reference_images: 10,
    models: [{ id: 'doubao-seedream-5-0-pro-260628' }],
    defaults: { model: 'doubao-seedream-5-0-pro-260628', image_size: '2K' },
  },
};
app.provider = 't8star';
app._activeProvider = 't8star';
app._personalKeyHint = '已检测到原供应商 key';

// PetiteVue's v-model may update the reactive value before @change runs.
app.provider = 'volcengine';
app.applyProvider('volcengine');
assert.equal(keyInput.value, '');
assert.equal(keyInput.readOnly, true);
assert.equal(app.baseUrlReadonly, true);
assert.match(keyInput.placeholder, /服务器托管/);
assert.deepEqual(Array.from(app.imageSizeOptions), ['1K', '1.5K', '2K']);
assert.equal(app.supportsSeed, false);
assert.equal(seedInput.disabled, true);
assert.equal(varySeed.disabled, true);
assert.equal(app.maxReferenceImages, 10);

const managedSubmission = await app.formDataWithSavedMedia();
assert.equal(managedSubmission.has('api_key'), false);

app.provider = 't8star';
app.applyProvider('t8star');
assert.equal(keyInput.value, 'original-t8star-key');
assert.equal(keyInput.readOnly, false);
assert.equal(app.baseUrlReadonly, false);
assert.equal(keyInput.placeholder, '留空使用本地配置');
assert.deepEqual(Array.from(app.imageSizeOptions), ['1K', '2K', '4K']);
assert.equal(app.supportsSeed, true);
assert.equal(seedInput.disabled, false);
assert.equal(varySeed.disabled, false);
assert.equal(app.keyHint, '已检测到原供应商 key');

// A second workspace must not overwrite the first workspace's provider key.
app.activeTabId = 'workspace-2';
keyInput.value = 'second-workspace-key';
app._activeProvider = 't8star';
app.provider = 'volcengine';
app.applyProvider('volcengine');
app.activeTabId = 'default';
app._activeProvider = 'volcengine';
app.provider = 't8star';
app.applyProvider('t8star');
assert.equal(keyInput.value, 'original-t8star-key');

console.log('nano provider switch: ok');
