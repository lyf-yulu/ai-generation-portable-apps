"use strict";

const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const test = require("node:test");

const BitableState = require(
  "../../src/feishu_generation_agent/web/static/bitable-state.js"
);

test("production-only page has no legacy document form", () => {
  const html = readFileSync(
    join(__dirname, "../../src/feishu_generation_agent/web/static/index.html"),
    "utf8",
  );

  assert.equal(html.includes('id="run-form"'), false);
  assert.equal(html.includes('id="source-url"'), false);
  assert.equal(html.includes('id="scan-bitable-button"'), true);
  assert.equal(html.includes('id="animation-category-tab"'), true);
  assert.equal(html.includes('id="portrait-category-tab"'), true);
  assert.equal(html.includes(">刷新任务<"), true);
});

const tasks = [
  {
    record_id: "rec-1",
    display_text: "雨中纸船",
    source_url: "https://tenant.feishu.cn/docx/doc1",
    executor_open_ids: ["ou_alice"],
  },
];

test("scan start, success and failure preserve explicit UI phases", () => {
  let state = BitableState.createState();
  state = BitableState.scanStarted(state, "animation");
  assert.equal(state.categories.animation.scan.phase, "loading");
  assert.equal(state.categories.animation.scan.error, "");

  state = BitableState.scanSucceeded(state, "animation", tasks);
  assert.equal(state.categories.animation.scan.phase, "ready");
  assert.deepEqual(state.categories.animation.tasks, tasks);

  state = BitableState.scanFailed(state, "animation", "读取失败");
  assert.equal(state.categories.animation.scan.phase, "error");
  assert.equal(state.categories.animation.scan.error, "读取失败");
  assert.deepEqual(state.categories.animation.tasks, tasks);
});

test("claim success removes the task and conflict keeps it retryable", () => {
  let state = BitableState.scanSucceeded(BitableState.createState(), "animation", tasks);
  state = BitableState.claimStarted(state, "rec-1", "animation");
  assert.deepEqual(state.claim, {
    phase: "loading",
    recordId: "rec-1",
    runId: null,
    category: "animation",
    error: "",
  });

  const conflicted = BitableState.claimConflict(state, "已被领取");
  assert.equal(conflicted.claim.phase, "conflict");
  assert.equal(conflicted.claim.error, "已被领取");
  assert.equal(conflicted.categories.animation.tasks.length, 1);

  state = BitableState.claimSucceeded(state, "run-1");
  assert.equal(state.claim.phase, "ready");
  assert.equal(state.claim.runId, "run-1");
  assert.deepEqual(state.categories.animation.tasks, []);
});

test("retry delivery has loading, success and failure states", () => {
  let state = BitableState.createState();
  state = BitableState.retryStarted(state, "run-1");
  assert.deepEqual(state.deliveryRetry, {
    phase: "loading",
    runId: "run-1",
    error: "",
  });

  state = BitableState.retrySucceeded(state);
  assert.equal(state.deliveryRetry.phase, "ready");

  state = BitableState.retryStarted(state, "run-1");
  state = BitableState.retryFailed(state, "结果列冲突");
  assert.equal(state.deliveryRetry.phase, "error");
  assert.equal(state.deliveryRetry.error, "结果列冲突");
});

test("production task keeps delivery block state through a scan", () => {
  let state = BitableState.createState();
  state = BitableState.scanSucceeded(state, "animation", [{
    record_id: "rec-no-maker",
    display_text: "需求 A",
    progress: "制作中",
    maker_name: null,
    deliverable: false,
    delivery_block_reason: "缺少需求制作人",
  }]);

  assert.equal(state.categories.animation.tasks[0].progress, "制作中");
  assert.equal(state.categories.animation.tasks[0].deliverable, false);
  assert.equal(state.categories.animation.tasks[0].delivery_block_reason, "缺少需求制作人");
});

test("recent runs survive resetting the active task context", () => {
  let state = BitableState.createState();
  state = BitableState.claimStarted(state, "rec-1", "animation");
  state = BitableState.claimSucceeded(state, "run-active");
  state = BitableState.recentSucceeded(state, [
    { run_id: "run-old", status: "succeeded" },
  ]);
  state = BitableState.resetRunContext(state);

  assert.equal(state.claim.runId, null);
  assert.equal(state.claim.phase, "idle");
  assert.deepEqual(state.recentRuns, [{ run_id: "run-old", status: "succeeded" }]);
});

test("category tabs keep independent scan results", () => {
  let state = BitableState.createState();
  state = BitableState.scanStarted(state, "animation");
  state = BitableState.scanSucceeded(state, "animation", [
    { record_id: "rec-animation", task_type: "动画类" },
  ]);
  state = BitableState.selectCategory(state, "portrait");

  assert.equal(state.activeCategory, "portrait");
  assert.equal(BitableState.activeCategoryState(state).scan.phase, "idle");

  state = BitableState.scanStarted(state, "portrait");
  state = BitableState.scanSucceeded(state, "portrait", [
    { record_id: "rec-portrait", task_type: "真人类" },
  ]);
  state = BitableState.selectCategory(state, "animation");

  assert.deepEqual(
    BitableState.activeCategoryState(state).tasks.map((task) => task.record_id),
    ["rec-animation"],
  );
});

test("claim success removes a task only from its category", () => {
  let state = BitableState.createState();
  state = BitableState.scanSucceeded(
    state,
    "portrait",
    [{ record_id: "rec-portrait" }],
  );
  state = BitableState.claimStarted(state, "rec-portrait", "portrait");
  state = BitableState.claimSucceeded(state, "run-portrait");

  assert.deepEqual(state.categories.portrait.tasks, []);
  assert.deepEqual(state.categories.animation.tasks, []);
  assert.equal(state.claim.category, "portrait");
});

test("a portrait scan failure does not clear animation results", () => {
  let state = BitableState.createState();
  state = BitableState.scanSucceeded(
    state,
    "animation",
    [{ record_id: "rec-animation" }],
  );
  state = BitableState.scanFailed(state, "portrait", "真人视图读取失败");

  assert.deepEqual(
    state.categories.animation.tasks,
    [{ record_id: "rec-animation" }],
  );
  assert.equal(state.categories.animation.scan.phase, "ready");
  assert.equal(state.categories.portrait.scan.phase, "error");
  assert.equal(
    state.categories.portrait.scan.error,
    "真人视图读取失败",
  );
});

test("run stage exposes asset preparation, provider generation and delivery", () => {
  assert.equal(BitableState.runStage({
    status: "running",
    operations: [{ phase: "intent_created", provider_task_id: null }],
  }), "正在准备参考素材并提交");

  assert.equal(BitableState.runStage({
    status: "waiting_provider",
    operations: [{ phase: "submitted", provider_task_id: "task-1" }],
  }), "Seedance 正在生成");

  assert.equal(BitableState.runStage({
    status: "delivering",
    operations: [{ phase: "succeeded", provider_task_id: "task-1" }],
  }), "正在写入结果表");
});

test("run elapsed time keeps increasing until a terminal status", () => {
  const createdAt = "2026-07-23T10:00:00+00:00";
  const updatedAt = "2026-07-23T10:00:08+00:00";

  assert.equal(BitableState.runElapsedMs({
    status: "waiting_provider",
    created_at: createdAt,
    updated_at: updatedAt,
  }, Date.parse("2026-07-23T10:00:20+00:00")), 20_000);

  assert.equal(BitableState.runElapsedMs({
    status: "succeeded",
    created_at: createdAt,
    updated_at: updatedAt,
  }, Date.parse("2026-07-23T10:00:20+00:00")), 8_000);
});
