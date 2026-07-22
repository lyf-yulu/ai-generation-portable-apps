"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const ReferenceUploadState = require(
  "../../src/feishu_generation_agent/web/static/reference-upload-state.js",
);

test("selected reference file survives status updates until upload succeeds", () => {
  const file = { name: "toast-reference.png" };
  let state = ReferenceUploadState.createState();
  state = ReferenceUploadState.fileSelected(state, "task-1", file);
  state = ReferenceUploadState.uploadStarted(state, "task-1");

  assert.equal(ReferenceUploadState.pendingFile(state, "task-1"), file);
  assert.deepEqual(ReferenceUploadState.feedback(state, "task-1"), {
    phase: "uploading",
    message: "正在上传图片…",
  });

  state = ReferenceUploadState.uploadSucceeded(state, "task-1");
  assert.equal(ReferenceUploadState.pendingFile(state, "task-1"), null);
  assert.deepEqual(ReferenceUploadState.feedback(state, "task-1"), {
    phase: "success",
    message: "图片已加入参考列表",
  });
});

test("failed reference upload keeps the selected file for retry", () => {
  const file = { name: "retry-reference.png" };
  let state = ReferenceUploadState.createState();
  state = ReferenceUploadState.fileSelected(state, "task-1", file);
  state = ReferenceUploadState.uploadFailed(state, "task-1", "图片格式不支持");

  assert.equal(ReferenceUploadState.pendingFile(state, "task-1"), file);
  assert.deepEqual(ReferenceUploadState.feedback(state, "task-1"), {
    phase: "error",
    message: "图片格式不支持",
  });
});
