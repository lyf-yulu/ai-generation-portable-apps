// Task-type mode must be per-tab.
//
// Bug (reported 2026-08-10):
//   1) The hint text under the mode picker (v-if="taskMode === 'extend'") is
//      driven by a single shared field. If tab A picks 视频延长, tab B (still
//      untouched by the user) also renders the extend hint.
//   2) After tab A switches to extend, tab B, then back to A, tab A's mode
//      resets to reference — but ratio/duration keep the extend values, and
//      the reference-mode memory has been overwritten by whatever B did.
//
// Fix: saveCurrentTabState/loadTargetTabState now snapshot taskMode,
// _prevTaskMode, and _taskModeMemory alongside the other per-tab fields.

import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

function makeOption(value) { return { value, textContent: value }; }
function makeSelect(name, values) {
  const options = values.map(makeOption);
  const s = {
    name, tagName: 'SELECT', type: 'select-one', options,
    _value: values[0],
    get value() { return this._value; },
    set value(v) { this._value = v; },
    appendChild(o) { this.options.push(o); },
  };
  Object.defineProperty(s, 'innerHTML', {
    get() { return ''; }, set(v) { if (v === '') this.options = []; },
  });
  return s;
}

const modelSelect = makeSelect('model', ['doubao-seedance-2-5-260628']);
const durationInput = { name: 'duration', type: 'number', value: '12', min: '4', max: '30' };
const resolutionSelect = makeSelect('resolution', ['480p', '720p']);
const ratioSelect = makeSelect('ratio', ['16:9', '9:16', '1:1', '4:3', '3:4', '21:9', 'adaptive']);

const form = {
  elements: { model: modelSelect, duration: durationInput, resolution: resolutionSelect, ratio: ratioSelect },
  addEventListener() {}, reset() {},
};

const document = {
  getElementById(id) { return id === 'sd-form' ? form : null; },
  querySelector(sel) {
    const m = sel.match(/name="([^"]+)"/);
    return m ? form.elements[m[1]] || null : null;
  },
  querySelectorAll() { return []; },
  addEventListener() {},
  createElement() { return { value: '', textContent: '' }; },
  head: { appendChild() {} }, body: { appendChild() {} },
};

const storage = new Map();
const sandbox = {
  window: { location: { pathname: '/seedance/index.html' }, _dlProgress: {} },
  document,
  localStorage: {
    getItem(k) { return storage.has(k) ? storage.get(k) : null; },
    setItem(k, v) { storage.set(k, String(v)); },
    removeItem(k) { storage.delete(k); },
  },
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

const providers = JSON.parse(fs.readFileSync('seedance/providers.json', 'utf8'));
const app = sandbox.window.SeedanceApp();
app.providers = { volcengine: providers.providers.volcengine };
app.models = providers.providers.volcengine.models.map(m => ({
  id: m.id, label: m.label,
  duration_range: m.duration_range || null,
  resolutions: m.resolutions || null,
  ratios: m.ratios || null,
}));
app.applyModelLimits();

// Two tabs, both start on reference.
app.tabs = [{ id: 'A', name: 'A' }, { id: 'B', name: 'B' }];
app.activeTabId = 'A';
app.saveTabsToLocalStorage = () => {};   // stub the persistence hop
app.saveWorkspaceDraft = () => {};       // draft persistence not under test
app.loadPreset = () => {};               // preset load not under test
app._clearTopicResultDom = () => {};
app._renderJobToDom = () => {};
app._scrollActiveTabIntoView = () => {};

// On tab A the user picks 视频延长 and sets duration=7.
app.taskMode = 'extend';
app.changeTaskMode();
durationInput.value = '7';
// On tab A the user also refined the reference-mode memory before ever
// leaving reference. Simulate this by populating the pre-extend history:
// go back to reference, set 20@9:16, then back to extend, back to reference,
// and finally back to extend as the leaving mode.
app.taskMode = 'reference';
app.changeTaskMode();
ratioSelect.value = '9:16';
durationInput.value = '20';
app._taskModeMemory.reference = { ratio: '9:16', duration: 20 };
app.taskMode = 'extend';
app.changeTaskMode();

// User switches to tab B.
app.switchTab('B');

assert.equal(app.taskMode, 'reference',
  'tab B starts on the default mode — extend must not bleed across tabs');
assert.equal(app._taskModeMemory.reference.duration, 12,
  'tab B has its own memory: fresh defaults, not the 20 the user typed in A');

// On tab B the user tries 视频编辑 (duration=-1) briefly, then leaves it in extend.
app.taskMode = 'edit';
app.changeTaskMode();
assert.equal(durationInput.value, '-1');
app.taskMode = 'extend';
app.changeTaskMode();
durationInput.value = '9';                     // B's extend duration is different from A's
// Nudge the mode so mem picks up 9 (the real UI's <select @change> also fires
// on re-selection; we just skip the form-typing → mem sync step).
app.taskMode = 'extend';
app.changeTaskMode();

// User switches back to tab A.
app.switchTab('A');

assert.equal(app.taskMode, 'extend',
  'tab A must remember it was in extend mode');
assert.equal(app._taskModeMemory.reference.duration, 20,
  'tab A must remember its reference-mode memory — not the 12 B saw');
assert.equal(app._taskModeMemory.reference.ratio, '9:16',
  'tab A ratio memory must survive an excursion through tab B');

// And now switching A back to reference must land on the values A had.
app.taskMode = 'reference';
app.changeTaskMode();
assert.equal(durationInput.value, '20',
  'tab A → reference must restore duration 20, not B\'s or the default');
assert.equal(ratioSelect.value, '9:16',
  'tab A → reference must restore ratio 9:16, not B\'s or the default');

// And tab B round-trip: switch back and its extend duration is still 9.
app.switchTab('B');
assert.equal(app.taskMode, 'extend', 'tab B stays on extend across the round-trip');
assert.equal(app._taskModeMemory.extend.duration, 9,
  'tab B keeps its own 9, not A\'s 7');

// newTab must start from scratch, not clone the current tab's mode state.
app.taskMode = 'edit';
app.changeTaskMode();
app.newTab();
assert.equal(app.taskMode, 'reference',
  'a brand-new tab must open on the default mode regardless of the tab it forked from');
assert.equal(app._taskModeMemory.reference.duration, 12,
  'a brand-new tab must start with default memory, not inherit from the parent');

console.log('test_task_mode_tab_isolation.mjs: all assertions passed');
