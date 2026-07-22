"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const BitableState = require(
  "../../src/feishu_generation_agent/web/static/bitable-state.js"
);

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
  state = BitableState.scanStarted(state);
  assert.equal(state.scan.phase, "loading");
  assert.equal(state.scan.error, "");

  state = BitableState.scanSucceeded(state, tasks);
  assert.equal(state.scan.phase, "ready");
  assert.deepEqual(state.tasks, tasks);

  state = BitableState.scanFailed(state, "读取失败");
  assert.equal(state.scan.phase, "error");
  assert.equal(state.scan.error, "读取失败");
  assert.deepEqual(state.tasks, tasks);
});

test("claim success removes the task and conflict keeps it retryable", () => {
  let state = BitableState.scanSucceeded(BitableState.createState(), tasks);
  state = BitableState.claimStarted(state, "rec-1");
  assert.deepEqual(state.claim, {
    phase: "loading",
    recordId: "rec-1",
    runId: null,
    error: "",
  });

  const conflicted = BitableState.claimConflict(state, "已被领取");
  assert.equal(conflicted.claim.phase, "conflict");
  assert.equal(conflicted.claim.error, "已被领取");
  assert.equal(conflicted.tasks.length, 1);

  state = BitableState.claimSucceeded(state, "run-1");
  assert.equal(state.claim.phase, "ready");
  assert.equal(state.claim.runId, "run-1");
  assert.deepEqual(state.tasks, []);
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
  state = BitableState.scanSucceeded(state, [{
    record_id: "rec-no-maker",
    display_text: "需求 A",
    progress: "制作中",
    maker_name: null,
    deliverable: false,
    delivery_block_reason: "缺少需求制作人",
  }]);

  assert.equal(state.tasks[0].progress, "制作中");
  assert.equal(state.tasks[0].deliverable, false);
  assert.equal(state.tasks[0].delivery_block_reason, "缺少需求制作人");
});

test("recent runs survive resetting the active task context", () => {
  let state = BitableState.createState();
  state = BitableState.claimStarted(state, "rec-1");
  state = BitableState.claimSucceeded(state, "run-active");
  state = BitableState.recentSucceeded(state, [
    { run_id: "run-old", status: "succeeded" },
  ]);
  state = BitableState.resetRunContext(state);

  assert.equal(state.claim.runId, null);
  assert.equal(state.claim.phase, "idle");
  assert.deepEqual(state.recentRuns, [{ run_id: "run-old", status: "succeeded" }]);
});
