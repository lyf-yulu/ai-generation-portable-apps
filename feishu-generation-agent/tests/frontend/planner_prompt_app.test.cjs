"use strict";

const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const STATIC = join(__dirname, "../../src/feishu_generation_agent/web/static");
const BitableState = require(join(STATIC, "bitable-state.js"));
const ReferenceUploadState = require(join(STATIC, "reference-upload-state.js"));
const ReviewState = require(join(STATIC, "review-state.js"));

class FakeNode {
  constructor(tagName = "div", id = "") {
    this.tagName = tagName.toUpperCase();
    this.id = id;
    this.children = [];
    this.dataset = {};
    this.disabled = false;
    this.hidden = false;
    this.textContent = "";
    this.value = "";
    this.className = "";
    this.listeners = new Map();
    this.classList = { toggle() {} };
  }

  addEventListener(name, listener) {
    const listeners = this.listeners.get(name) || [];
    listeners.push(listener);
    this.listeners.set(name, listeners);
  }

  async dispatch(name, target = this) {
    await Promise.all((this.listeners.get(name) || []).map((listener) => listener({ target })));
  }

  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  querySelectorAll() { return []; }
  setAttribute() {}
  scrollIntoView() {}
  focus() {}
}

function jsonResponse(status, payload) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => "application/json" },
    async json() { return payload; },
  };
}

function response(overrides = {}) {
  return {
    mode: "prime",
    editable: true,
    prompt_text: "Prime 提示词",
    version: 0,
    source: "prime",
    ...overrides,
  };
}

async function settle() {
  for (let index = 0; index < 8; index += 1) await new Promise((resolve) => setImmediate(resolve));
}

async function loadApp(fetch, { pathname = "/feishu-generation-agent/" } = {}) {
  const nodes = new Map();
  const getNode = (id) => {
    if (!nodes.has(id)) nodes.set(id, new FakeNode("div", id));
    return nodes.get(id);
  };
  getNode("animation-category-tab").dataset.category = "animation";
  getNode("portrait-category-tab").dataset.category = "portrait";
  const context = {
    BitableState,
    ReferenceUploadState,
    ReviewState,
    document: {
      createElement: (tagName) => new FakeNode(tagName),
      getElementById: getNode,
      querySelector: () => new FakeNode("main"),
    },
    fetch,
    confirm: () => true,
    location: { pathname, reload() {} },
    setInterval: () => 1,
    clearInterval() {},
    console,
  };
  for (const file of ["api-paths.js", "planner-prompt-state.js", "app.js"]) {
    vm.runInNewContext(readFileSync(join(STATIC, file), "utf8"), context);
  }
  await settle();
  return { getNode };
}

function baseFetch(promptHandler, requests) {
  return async (url, options = {}) => {
    requests.push({ url, options });
    if (url.endsWith("/api/planner-prompt")) return promptHandler(options);
    if (url.endsWith("/api/health")) return jsonResponse(200, { modes: { bitable: false } });
    throw new Error(`unexpected request: ${url}`);
  };
}

test("initial GET uses the mount prefix and editable controls the entry", async () => {
  const requests = [];
  const app = await loadApp(baseFetch(() => jsonResponse(200, response({ editable: false })), requests));

  assert.equal(requests[0].url, "/feishu-generation-agent/api/planner-prompt");
  assert.equal(app.getNode("planner-prompt-entry").hidden, true);
  assert.equal(app.getNode("planner-prompt-button").disabled, true);
});

test("save sends prefix-safe PUT and updates the rendered personal version", async () => {
  const requests = [];
  const app = await loadApp(baseFetch((options) => {
    if (options.method === "PUT") {
      return jsonResponse(200, response({
        mode: "personal", source: "personal", version: 3, prompt_text: "我的提示词",
      }));
    }
    return jsonResponse(200, response());
  }, requests));

  await app.getNode("planner-prompt-button").dispatch("click");
  const editor = app.getNode("planner-prompt-text");
  editor.value = "我的提示词";
  await editor.dispatch("input");
  await app.getNode("planner-prompt-save").dispatch("click");

  const put = requests.find((request) => request.options.method === "PUT");
  assert.equal(put.url, "/feishu-generation-agent/api/planner-prompt");
  assert.deepEqual(JSON.parse(put.options.body), { prompt_text: "我的提示词" });
  assert.equal(app.getNode("planner-prompt-mode").textContent, "使用个人版本 v3");
  assert.equal(app.getNode("planner-prompt-modal").hidden, false);
  assert.equal(app.getNode("planner-prompt-feedback").textContent, "个人版本已保存");
});

test("422 save keeps the modal and dirty text while showing the server error", async () => {
  const requests = [];
  const app = await loadApp(baseFetch((options) => options.method === "PUT"
    ? jsonResponse(422, { detail: "提示词无效" })
    : jsonResponse(200, response()), requests));

  await app.getNode("planner-prompt-button").dispatch("click");
  const editor = app.getNode("planner-prompt-text");
  editor.value = "无效内容";
  await editor.dispatch("input");
  await app.getNode("planner-prompt-save").dispatch("click");

  assert.equal(app.getNode("planner-prompt-modal").hidden, false);
  assert.equal(editor.value, "无效内容");
  assert.equal(app.getNode("planner-prompt-feedback").textContent, "提示词无效");
});

test("restore sends prefix-safe DELETE and renders returned Prime content", async () => {
  const requests = [];
  const app = await loadApp(baseFetch((options) => options.method === "DELETE"
    ? jsonResponse(200, response({ prompt_text: "返回的 Prime" }))
    : jsonResponse(200, response({
      mode: "personal", source: "personal", version: 3, prompt_text: "个人提示词",
    })), requests));

  await app.getNode("planner-prompt-button").dispatch("click");
  await app.getNode("planner-prompt-reset").dispatch("click");

  const deleted = requests.find((request) => request.options.method === "DELETE");
  assert.equal(deleted.url, "/feishu-generation-agent/api/planner-prompt");
  assert.equal(app.getNode("planner-prompt-mode").textContent, "使用 Prime");
  assert.equal(app.getNode("planner-prompt-text").value, "返回的 Prime");
});
