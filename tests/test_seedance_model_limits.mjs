// Seedance per-model parameter limits.
//
// Ark validates duration / resolution / ratio only after the job is queued, so an
// out-of-range value costs the user a wait plus an async error instead of failing
// fast. providers.json carries each model's real limits (from the official
// capability matrix) and applyModelLimits() narrows the form to them.
//
// Official limits as of 2026-08-07:
//   2.5           4~30s, 480p/720p
//   2.0           4~15s, 480p/720p/1080p/4k
//   2.0-fast/mini 4~15s, 480p/720p

import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

function makeOption(value) {
  return { value, textContent: value };
}

function makeSelect(name, values) {
  const select = {
    name,
    tagName: 'SELECT',
    type: 'select-one',
    options: values.map(makeOption),
    innerHTML: '',
    _value: values[0],
    get value() { return this._value; },
    set value(v) { this._value = v; },
    appendChild(node) { this.options.push(node); },
  };
  // innerHTML = '' is how applyModelLimits clears the list before refilling.
  Object.defineProperty(select, 'innerHTML', {
    get() { return ''; },
    set(v) { if (v === '') this.options = []; },
  });
  return select;
}

const modelSelect = makeSelect('model', [
  'doubao-seedance-2-0-260128',
  'doubao-seedance-2-0-fast-260128',
  'doubao-seedance-2-0-mini-260615',
  'doubao-seedance-2-5-260628',
]);
const durationInput = { name: 'duration', type: 'number', value: '12', min: '4', max: '15' };
const resolutionSelect = makeSelect('resolution', ['720p', '480p', '1080p', '4k']);
const ratioSelect = makeSelect('ratio', ['16:9', '9:16', '1:1']);

const form = {
  elements: {
    model: modelSelect,
    duration: durationInput,
    resolution: resolutionSelect,
    ratio: ratioSelect,
  },
  addEventListener() {},
};

const listeners = {};
modelSelect.addEventListener = (event, fn) => { listeners[event] = fn; };

const document = {
  getElementById(id) { return id === 'sd-form' ? form : null; },
  querySelector(selector) {
    const match = selector.match(/name="([^"]+)"/);
    return match ? form.elements[match[1]] || null : null;
  },
  querySelectorAll() { return []; },
  addEventListener() {},
  createElement() { return { value: '', textContent: '' }; },
  head: { appendChild() {} },
  body: { appendChild() {} },
};

const sandbox = {
  window: { location: { pathname: '/seedance/index.html' }, _dlProgress: {} },
  document,
  localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
  FormData: class { constructor() {} set() {} get() {} },
  PetiteVue: { createApp() { return { mount() {} }; } },
  URL, URLSearchParams, Blob, File: class {},
  fetch: async () => ({}),
  crypto: { randomUUID: () => 'uuid' },
  console, setTimeout, setInterval: () => 1, clearInterval() {},
  alert() {}, confirm: () => true, DataTransfer: class {},
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync('seedance/static/app.js', 'utf8'), sandbox);

// The real capability data must come from providers.json, not from the test —
// otherwise this would pass even if the config were wrong.
const providers = JSON.parse(fs.readFileSync('seedance/providers.json', 'utf8'));
const volcengine = providers.providers.volcengine;

const app = sandbox.window.SeedanceApp();
app.providers = { volcengine };
app.models = volcengine.models.map(m => ({
  id: m.id,
  label: m.label,
  duration_range: m.duration_range || null,
  resolutions: m.resolutions || null,
  ratios: m.ratios || null,
  defaults: m.defaults || null,
}));

function limitsFor(id) {
  return volcengine.models.find(m => m.id === id);
}

// --- 2.5: 30s ceiling, no 1080p/4k ---------------------------------------
modelSelect.value = 'doubao-seedance-2-5-260628';
durationInput.value = '12';
app.applyModelLimits();

const spec25 = limitsFor('doubao-seedance-2-5-260628');
assert.equal(durationInput.max, String(spec25.duration_range[1]), '2.5 duration ceiling');
assert.equal(durationInput.max, '30', '2.5 must allow 30s');
assert.deepEqual(
  resolutionSelect.options.map(o => o.value),
  spec25.resolutions,
  '2.5 resolution list must match providers.json',
);
assert.ok(
  !resolutionSelect.options.some(o => o.value === '1080p' || o.value === '4k'),
  '2.5 does not support 1080p/4k — offering them would produce an async Ark error',
);

// --- 2.0: 15s ceiling, 1080p/4k available --------------------------------
modelSelect.value = 'doubao-seedance-2-0-260128';
app.applyModelLimits();

const spec20 = limitsFor('doubao-seedance-2-0-260128');
assert.equal(durationInput.max, '15', '2.0 caps at 15s');
assert.deepEqual(resolutionSelect.options.map(o => o.value), spec20.resolutions, '2.0 resolutions');
assert.ok(
  resolutionSelect.options.some(o => o.value === '4k'),
  '2.0 supports 4k and must keep offering it',
);

// --- switching 2.5 -> 2.0 must clamp an out-of-range duration ------------
modelSelect.value = 'doubao-seedance-2-5-260628';
app.applyModelLimits();
durationInput.value = '30';           // legal on 2.5
modelSelect.value = 'doubao-seedance-2-0-260128';
app.applyModelLimits();               // 30 is illegal on 2.0
assert.equal(durationInput.value, '15', 'duration must clamp to the new model ceiling, not stay at 30');

// --- an unsupported resolution must fall back, not persist ---------------
modelSelect.value = 'doubao-seedance-2-0-260128';
app.applyModelLimits();
resolutionSelect.value = '4k';        // legal on 2.0
modelSelect.value = 'doubao-seedance-2-5-260628';
app.applyModelLimits();              // 4k is illegal on 2.5
assert.ok(
  spec25.resolutions.includes(resolutionSelect.value),
  `resolution must fall back into 2.5's supported set, got ${resolutionSelect.value}`,
);

// --- a still-valid selection must survive the switch --------------------
modelSelect.value = 'doubao-seedance-2-0-260128';
app.applyModelLimits();
resolutionSelect.value = '720p';     // legal on both
modelSelect.value = 'doubao-seedance-2-5-260628';
app.applyModelLimits();
assert.equal(resolutionSelect.value, '720p', '720p is valid on both models and must be preserved');

// --- ratio: adaptive must be offered (required by edit/extend tasks) ----
modelSelect.value = 'doubao-seedance-2-5-260628';
app.applyModelLimits();
assert.ok(
  ratioSelect.options.some(o => o.value === 'adaptive'),
  'adaptive is mandatory for video edit/extend tasks and must be selectable',
);

// --- init() must wire the change listener that drives all of the above ---
// Without it the limits would only apply on provider load, so picking a
// different model in the dropdown would leave a stale ceiling behind.
await app.init();
assert.equal(typeof listeners.change, 'function', 'init() must add a change listener to the model select');

modelSelect.value = 'doubao-seedance-2-0-260128';
durationInput.value = '30';
listeners.change();
assert.equal(durationInput.value, '15', 'the change listener must clamp duration on its own');

console.log('test_seedance_model_limits.mjs: all assertions passed');
