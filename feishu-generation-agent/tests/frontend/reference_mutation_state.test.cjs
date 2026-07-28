"use strict";

const assert = require("node:assert/strict");
const { join } = require("node:path");
const test = require("node:test");

const ReferenceMutationState = require(join(
  __dirname,
  "../../src/feishu_generation_agent/web/static/reference-mutation-state.js",
));

test("replacement exposes uploading and success feedback", () => {
  let state = ReferenceMutationState.createState();
  state = ReferenceMutationState.start(
    state,
    "task-1",
    "image-2",
    "replace",
    "new.png",
  );

  assert.deepEqual(
    ReferenceMutationState.rowFeedback(state, "task-1", "image-2"),
    { phase: "uploading", message: "正在替换 new.png…" },
  );
  assert.equal(
    ReferenceMutationState.isBusy(state, "task-1", "image-2"),
    true,
  );

  state = ReferenceMutationState.succeed(
    state,
    "task-1",
    "image-2",
    "参考素材已替换",
  );

  assert.deepEqual(
    ReferenceMutationState.taskFeedback(state, "task-1"),
    { phase: "success", message: "参考素材已替换" },
  );
});

test("deletion failure restores controls and exposes the local reason", () => {
  let state = ReferenceMutationState.createState();
  state = ReferenceMutationState.start(
    state,
    "task-1",
    "image-2",
    "delete",
  );
  state = ReferenceMutationState.fail(
    state,
    "task-1",
    "image-2",
    "至少保留一张参考素材",
  );

  assert.equal(
    ReferenceMutationState.isBusy(state, "task-1", "image-2"),
    false,
  );
  assert.deepEqual(
    ReferenceMutationState.rowFeedback(state, "task-1", "image-2"),
    { phase: "error", message: "至少保留一张参考素材" },
  );
});
