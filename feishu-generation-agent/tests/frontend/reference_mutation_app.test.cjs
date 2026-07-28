"use strict";

const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const STATIC = join(__dirname, "../../src/feishu_generation_agent/web/static");
const BitableState = require(join(STATIC, "bitable-state.js"));
const ReferenceMutationState = require(join(STATIC, "reference-mutation-state.js"));
const ReferenceUploadState = require(join(STATIC, "reference-upload-state.js"));
const ReviewState = require(join(STATIC, "review-state.js"));

class FakeNode {
  constructor(tagName = "div", id = "") {
    this.tagName = tagName.toUpperCase();
    this.id = id;
    this.children = [];
    this.dataset = {};
    this.disabled = false;
    this.hidden = id === "error-message";
    this.textContent = "";
    this.value = "";
    this.files = [];
    this.className = "";
    this.listeners = new Map();
    this.classList = { toggle() {} };
  }

  addEventListener(name, listener) {
    const listeners = this.listeners.get(name) || [];
    listeners.push(listener);
    this.listeners.set(name, listeners);
  }

  dispatch(name) {
    return Promise.all(
      (this.listeners.get(name) || []).map((listener) => listener({ target: this })),
    );
  }

  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  querySelectorAll(selector) {
    const tags = new Set(
      selector.split(",").map((value) => value.trim().toUpperCase()),
    );
    return descendants(this).filter((node) => tags.has(node.tagName));
  }
  setAttribute(name, value) { this[name] = String(value); }
  scrollIntoView() {}
  focus() {}
}

function descendants(node) {
  const result = [];
  const visit = (child) => {
    if (!child || typeof child !== "object") return;
    result.push(child);
    (child.children || []).forEach(visit);
  };
  (node.children || []).forEach(visit);
  return result;
}

function byClass(node, className) {
  return descendants(node).filter((child) => (
    String(child.className).split(/\s+/).includes(className)
  ));
}

function jsonResponse(status, payload) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => "application/json" },
    async json() { return payload; },
  };
}

function runView() {
  return {
    run_id: "run-1",
    thread_id: "thread-1",
    source_url: "https://acme.feishu.cn/docx/reference-actions",
    status: "waiting_approval",
    events: [],
    privacy: {},
    approval: {
      document_title: "参考素材操作测试",
      revision: 7,
      document_summary: "",
      selected_task_ids: [],
      media_assets: [{
        asset_id: "asset-1",
        media_kind: "image",
        preview_url: "/api/assets/asset-1",
      }],
      excluded_assets: [],
      vision_descriptions: [],
      validation_issues: [],
      ingest_issue_records: [],
      vision_issues: [],
      coverage: {
        successful_total: 1,
        referenced_count: 1,
        excluded_count: 0,
        uncovered_count: 0,
        failed_count: 0,
      },
      tasks: [{
        task_id: "task-1",
        task_type: "image_to_video",
        title: "火锅",
        prompt: "参考图1中的火锅持续沸腾",
        negative_constraints: [],
        reference_mode: "multi_reference",
        reference_images: [{
          asset_id: "asset-1",
          role: "reference_image",
          order: 1,
        }],
        aspect_ratio: "16:9",
        duration: 10,
        resolution: "720p",
        generate_audio: false,
        output_count: 1,
        assumptions: [],
        warnings: [],
        blocking_issues: [],
      }],
    },
  };
}

async function settle() {
  for (let index = 0; index < 8; index += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
}

async function loadApp(fetch) {
  const nodes = new Map();
  const getNode = (id) => {
    if (!nodes.has(id)) nodes.set(id, new FakeNode("div", id));
    return nodes.get(id);
  };
  getNode("animation-category-tab").dataset.category = "animation";
  getNode("portrait-category-tab").dataset.category = "portrait";
  const context = {
    BitableState,
    File,
    FormData,
    ReferenceMutationState,
    ReferenceUploadState,
    ReviewState,
    document: {
      createElement: (tagName) => new FakeNode(tagName),
      getElementById: getNode,
      querySelector: () => new FakeNode("main"),
    },
    fetch,
    confirm: () => true,
    location: { pathname: "/", reload() {} },
    setInterval: () => 1,
    clearInterval() {},
    console,
  };
  for (const file of ["api-paths.js", "app.js"]) {
    vm.runInNewContext(readFileSync(join(STATIC, file), "utf8"), context);
  }
  await settle();
  return { getNode };
}

function fetchForRun(requests, { deleteGate = null } = {}) {
  const view = runView();
  return async (url, options = {}) => {
    requests.push({ url, options });
    if (url === "/api/health") {
      return jsonResponse(200, { modes: { bitable: true } });
    }
    if (url === "/api/bitable/recent-runs") return jsonResponse(200, []);
    if (url === "/api/bitable/active-runs") {
      return jsonResponse(200, [{ run_id: "run-1", display_text: "火锅" }]);
    }
    if (url.startsWith("/api/bitable/tasks?")) return jsonResponse(200, []);
    if (url === "/api/runs/run-1" && (!options.method || options.method === "GET")) {
      return jsonResponse(200, view);
    }
    if (url === "/api/runs/run-1/references" && options.method === "POST") {
      return jsonResponse(201, { asset_id: "asset-2" });
    }
    if (
      url === "/api/runs/run-1/tasks/task-1/references/asset-1"
      && options.method === "DELETE"
    ) {
      if (deleteGate) await deleteGate.promise;
      return jsonResponse(200, { ok: true });
    }
    throw new Error(`unexpected request: ${url}`);
  };
}

test("replacement uses a native label and reports success beside the references", async () => {
  const requests = [];
  const app = await loadApp(fetchForRun(requests));
  const taskList = app.getNode("task-list");
  const replaceInput = byClass(taskList, "reference-file-input")[0];
  const replaceLabel = byClass(taskList, "reference-replace-label")[0];

  assert.equal(replaceLabel.tagName, "LABEL");
  assert.equal(replaceLabel.htmlFor, replaceInput.id);
  assert.notEqual(replaceInput.hidden, true);

  replaceInput.files = [new File(["replacement"], "replacement.png", {
    type: "image/png",
  })];
  await replaceInput.dispatch("change");
  await settle();

  assert.equal(
    requests.some((request) => (
      request.options.method === "POST"
      && request.url === "/api/runs/run-1/references"
    )),
    true,
  );
  assert.match(
    byClass(taskList, "reference-section-feedback")[0].textContent,
    /参考素材已替换/,
  );
});

test("delete shows an immediate busy state and sends the request once", async () => {
  let releaseDelete;
  const deleteGate = {
    promise: new Promise((resolve) => { releaseDelete = resolve; }),
  };
  const requests = [];
  const app = await loadApp(fetchForRun(requests, { deleteGate }));
  const taskList = app.getNode("task-list");
  const deleteButton = byClass(taskList, "reference-delete-button")[0];

  const pending = deleteButton.dispatch("click");
  await settle();

  assert.equal(deleteButton.disabled, true);
  assert.equal(deleteButton.textContent, "删除中…");
  assert.equal(
    requests.filter((request) => request.options.method === "DELETE").length,
    1,
  );

  releaseDelete();
  await pending;
});
