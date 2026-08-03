"use strict";

const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const BitableState = require(
  "../../src/feishu_generation_agent/web/static/bitable-state.js"
);
const ReferenceUploadState = require(
  "../../src/feishu_generation_agent/web/static/reference-upload-state.js"
);
const ReferenceMutationState = require(
  "../../src/feishu_generation_agent/web/static/reference-mutation-state.js"
);
const ReviewState = require(
  "../../src/feishu_generation_agent/web/static/review-state.js"
);

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
    this.listeners = new Map();
    this.classList = { toggle() {} };
  }

  addEventListener(name, listener) {
    const listeners = this.listeners.get(name) || [];
    listeners.push(listener);
    this.listeners.set(name, listeners);
  }

  async dispatch(name) {
    await Promise.all(
      (this.listeners.get(name) || []).map((listener) =>
        listener({ target: this })
      ),
    );
  }

  append(...children) {
    this.children.push(...children);
  }

  replaceChildren(...children) {
    this.children = children;
  }

  querySelectorAll(selector) {
    const tags = new Set(
      selector.split(",").map((value) => value.trim().toUpperCase()),
    );
    const matches = [];
    const visit = (node) => {
      if (tags.has(node.tagName)) matches.push(node);
      node.children.forEach(visit);
    };
    this.children.forEach(visit);
    return matches;
  }

  setAttribute() {}
  scrollIntoView() {}
}

function jsonResponse(status, payload) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => "application/json" },
    async json() {
      return payload;
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
  const document = {
    createElement: (tagName) => new FakeNode(tagName),
    getElementById: getNode,
    querySelector: () => new FakeNode("main"),
  };
  const context = {
    BitableState,
    ReferenceMutationState,
    ReferenceUploadState,
    ReviewState,
    document,
    fetch,
    confirm: () => true,
    location: { reload() {} },
    setInterval: () => 1,
    clearInterval() {},
    console,
  };
  const source = readFileSync(
    join(__dirname, "../../src/feishu_generation_agent/web/static/app.js"),
    "utf8",
  );
  vm.runInNewContext(source, context);
  await settle();
  return { getNode };
}

function baseFetch(categoryResponse) {
  return async (url, options = {}) => {
    if (url === "/api/health") {
      return jsonResponse(200, { modes: { bitable: true } });
    }
    if (url === "/api/bitable/recent-runs") return jsonResponse(200, []);
    if (url === "/api/bitable/active-runs") return jsonResponse(200, []);
    if (url.startsWith("/api/bitable/tasks?")) {
      const category = new URL(url, "https://local.invalid").searchParams.get(
        "category",
      );
      return categoryResponse(category);
    }
    if (options.method === "POST" && url.includes("/claim")) {
      return jsonResponse(409, { detail: "该任务已被领取" });
    }
    throw new Error(`unexpected request: ${url}`);
  };
}

test("scan errors stay with their category instead of the global error box", async () => {
  const app = await loadApp(
    baseFetch((category) =>
      category === "portrait"
        ? jsonResponse(503, { detail: "真人视图读取失败" })
        : jsonResponse(200, []),
    ),
  );

  await app.getNode("portrait-category-tab").dispatch("click");
  await app.getNode("animation-category-tab").dispatch("click");

  assert.equal(app.getNode("error-message").hidden, true);
  assert.equal(
    app.getNode("error-message").textContent.includes("真人视图读取失败"),
    false,
  );

  await app.getNode("portrait-category-tab").dispatch("click");

  assert.equal(app.getNode("bitable-status").textContent, "真人视图读取失败");
  assert.equal(app.getNode("error-message").hidden, true);
});

test("claim errors do not remain in the global box after a cached category switch", async () => {
  const app = await loadApp(
    baseFetch((category) =>
      jsonResponse(
        200,
        category === "animation"
          ? [{ record_id: "rec-animation", display_text: "动画需求" }]
          : [],
      ),
    ),
  );
  await app.getNode("portrait-category-tab").dispatch("click");
  await app.getNode("animation-category-tab").dispatch("click");
  const claimButton = app
    .getNode("bitable-task-list")
    .querySelectorAll("button")[0];

  await claimButton.dispatch("click");
  await app.getNode("portrait-category-tab").dispatch("click");

  assert.equal(app.getNode("error-message").hidden, true);
  assert.equal(
    app.getNode("error-message").textContent.includes("该任务已被领取"),
    false,
  );
});

test("approval view distinguishes blocking document and nonblocking asset issues", async () => {
  const runView = {
    run_id: "run-ingest-issues",
    thread_id: "thread-ingest-issues",
    source_url: "https://acme.feishu.cn/docx/issues",
    status: "waiting_approval",
    events: [],
    privacy: {},
    approval: {
      document_title: "素材读取测试",
      revision: 7,
      document_summary: "",
      tasks: [],
      media_assets: [],
      excluded_assets: [],
      selected_task_ids: [],
      coverage: {
        successful_total: 1,
        referenced_count: 1,
        excluded_count: 0,
        uncovered_count: 0,
        failed_count: 1,
      },
      validation_issues: [],
      ingest_issue_records: [
        {
          severity: "blocking",
          code: "sheet_export_timeout",
          display_message: "飞书电子表格导出超时，请稍后重试",
        },
        {
          severity: "asset",
          code: "media_download_failed",
          display_message: "文档图片下载失败，其他素材可继续处理",
        },
      ],
      blocking_ingest_issues: [],
      asset_ingest_issues: [],
      vision_issues: ["素材 asset-2 视觉分析失败：图片无法识别"],
    },
  };
  const app = await loadApp(async (url) => {
    if (url === "/api/health") {
      return jsonResponse(200, { modes: { bitable: true } });
    }
    if (url === "/api/bitable/recent-runs") return jsonResponse(200, []);
    if (url === "/api/bitable/active-runs") {
      return jsonResponse(200, [{
        run_id: runView.run_id,
        display_text: "素材读取测试",
      }]);
    }
    if (url === `/api/runs/${runView.run_id}`) {
      return jsonResponse(200, runView);
    }
    if (url.startsWith("/api/bitable/tasks?")) return jsonResponse(200, []);
    throw new Error(`unexpected request: ${url}`);
  });

  assert.equal(
    app.getNode("blocking-ingest-issues").textContent,
    "文档读取阻塞：飞书电子表格导出超时，请稍后重试",
  );
  assert.equal(app.getNode("blocking-ingest-issues").hidden, false);
  assert.equal(
    app.getNode("asset-ingest-issues").textContent,
    "素材读取失败（不影响其他素材）：文档图片下载失败，其他素材可继续处理",
  );
  assert.equal(app.getNode("asset-ingest-issues").hidden, false);
  assert.equal(
    app.getNode("vision-issues").textContent,
    "素材识别失败（不影响其他素材）：素材 asset-2 视觉分析失败：图片无法识别",
  );
  assert.equal(app.getNode("vision-issues").hidden, false);
});

test("failed runs show the safe provider error in the review panel", async () => {
  const runView = {
    run_id: "run-provider-error",
    thread_id: "thread-provider-error",
    source_url: "https://acme.feishu.cn/docx/provider-error",
    status: "failed",
    events: [],
    privacy: {},
    execution_records: [{
      task_id: "task-video",
      provider: "seedance",
      status: "submission_uncertain",
      error: {
        category: "provider_terminal_error",
        message: "生成服务拒绝了请求",
        retryable: false,
        code: "submit_http_400",
      },
    }],
    approval: {
      document_title: "供应商错误测试",
      revision: 1,
      document_summary: "",
      tasks: [],
      media_assets: [],
      excluded_assets: [],
      coverage: {},
      validation_issues: [],
      ingest_issue_records: [],
      vision_issues: [],
    },
  };
  const app = await loadApp(async (url) => {
    if (url === "/api/health") {
      return jsonResponse(200, { modes: { bitable: true } });
    }
    if (url === "/api/bitable/recent-runs") return jsonResponse(200, []);
    if (url === "/api/bitable/active-runs") {
      return jsonResponse(200, [{ run_id: runView.run_id }]);
    }
    if (url === `/api/runs/${runView.run_id}`) {
      return jsonResponse(200, runView);
    }
    if (url.startsWith("/api/bitable/tasks?")) return jsonResponse(200, []);
    throw new Error(`unexpected request: ${url}`);
  });

  assert.equal(
    app.getNode("execution-errors").textContent,
    "生成失败：Seedance：生成服务拒绝了请求（submit_http_400）",
  );
  assert.equal(app.getNode("execution-errors").hidden, false);
});
