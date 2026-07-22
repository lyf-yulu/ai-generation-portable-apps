"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const ReviewState = require("../../src/feishu_generation_agent/web/static/review-state.js");

function task(taskId, taskType = "image_to_video") {
  return {
    task_id: taskId,
    task_type: taskType,
    title: taskId,
    prompt: `server prompt ${taskId}`,
    negative_constraints: ["server negative"],
    reference_images: [{ asset_id: "asset-1", role: "reference_image", order: 1 }],
    aspect_ratio: "16:9",
    image_size: taskType === "image_to_image" ? "2K" : null,
    duration: taskType === "image_to_video" ? 10 : null,
    resolution: taskType === "image_to_video" ? "720p" : null,
    generate_audio: taskType === "image_to_video" ? false : null,
    output_count: 1,
  };
}

function view({
  revision = 7,
  status = "waiting_approval",
  selectedTaskIds = [],
  taskOnePrompt,
} = {}) {
  const tasks = [task("task-1"), task("task-2", "image_to_image")];
  if (taskOnePrompt) tasks[0].prompt = taskOnePrompt;
  return {
    run_id: "run-1",
    status,
    events: [],
    approval: {
      revision,
      document_title: "Test",
      document_summary: "server summary",
      tasks,
      media_assets: [{ asset_id: "asset-1", preview_url: "/asset-1" }],
      selected_task_ids: selectedTaskIds,
    },
  };
}

test("same-revision polls preserve local selection and every editable task field", () => {
  const original = view();
  let state = ReviewState.createReviewState();
  state = ReviewState.mergeServerView(state, original);

  assert.deepEqual(ReviewState.selectedTaskIds(state), ["task-1", "task-2"]);

  state = ReviewState.setTaskSelected(state, "task-2", false);
  state = ReviewState.patchTask(state, "task-1", {
    prompt: "last local prompt",
    negative_constraints: ["no blur", "no watermark"],
    aspect_ratio: "9:16",
    duration: 6,
    resolution: "1080p",
    generate_audio: true,
    output_count: 2,
    reference_images: [{ asset_id: "asset-1", role: "first_frame", order: 1 }],
  });
  state = ReviewState.patchTask(state, "task-2", {
    image_size: "4K",
    output_count: 3,
  });

  for (let pollCount = 0; pollCount < 4; pollCount += 1) {
    state = ReviewState.mergeServerView(state, view());
  }

  const draft = ReviewState.draftView(state);
  assert.equal(draft.approval.tasks[0].prompt, "last local prompt");
  assert.deepEqual(draft.approval.tasks[0].negative_constraints, ["no blur", "no watermark"]);
  assert.equal(draft.approval.tasks[0].aspect_ratio, "9:16");
  assert.equal(draft.approval.tasks[0].duration, 6);
  assert.equal(draft.approval.tasks[0].resolution, "1080p");
  assert.equal(draft.approval.tasks[0].generate_audio, true);
  assert.equal(draft.approval.tasks[0].output_count, 2);
  assert.deepEqual(draft.approval.tasks[0].reference_images, [
    { asset_id: "asset-1", role: "first_frame", order: 1 },
  ]);
  assert.equal(draft.approval.tasks[1].image_size, "4K");
  assert.equal(draft.approval.tasks[1].output_count, 3);

  const payload = ReviewState.buildApprovalPayload(state);
  assert.deepEqual(payload.selected_task_ids, ["task-1"]);
  assert.equal(payload.tasks[0].prompt, "last local prompt");
  assert.equal(payload.tasks[1].image_size, "4K");
  assert.equal(original.approval.tasks[0].prompt, "server prompt task-1");
});

test("submission snapshot is immutable across polls and failure keeps the draft", () => {
  let state = ReviewState.mergeServerView(ReviewState.createReviewState(), view());
  state = ReviewState.setTaskSelected(state, "task-2", false);
  state = ReviewState.patchTask(state, "task-1", { prompt: "draft before submit" });

  const submission = ReviewState.beginApprovalSubmit(state);
  state = submission.state;
  const snapshot = submission.payload;
  assert.equal(Object.isFrozen(snapshot), true);
  assert.equal(Object.isFrozen(snapshot.tasks), true);
  assert.equal(ReviewState.isSubmitting(state), true);
  assert.throws(
    () => ReviewState.patchTask(state, "task-1", { prompt: "late edit" }),
    /提交中/,
  );

  state = ReviewState.mergeServerView(state, view());
  assert.equal(snapshot.tasks[0].prompt, "draft before submit");
  assert.deepEqual(snapshot.selected_task_ids, ["task-1"]);

  state = ReviewState.failApprovalSubmit(state);
  assert.equal(ReviewState.isSubmitting(state), false);
  assert.equal(ReviewState.draftView(state).approval.tasks[0].prompt, "draft before submit");
  assert.deepEqual(ReviewState.selectedTaskIds(state), ["task-1"]);
});

test("successful submission adopts a server update observed during the request", () => {
  let state = ReviewState.mergeServerView(ReviewState.createReviewState(), view());
  state = ReviewState.patchTask(state, "task-1", { prompt: "submitted local prompt" });
  state = ReviewState.beginApprovalSubmit(state).state;
  state = ReviewState.mergeServerView(
    state,
    view({ revision: 8, status: "approved", selectedTaskIds: ["task-1"], taskOnePrompt: "submitted local prompt" }),
  );

  state = ReviewState.completeApprovalSubmit(state);
  assert.equal(ReviewState.isSubmitting(state), false);
  assert.equal(ReviewState.conflictMessage(state), "");
  assert.equal(ReviewState.hasDirty(state), false);
  assert.equal(ReviewState.draftView(state).approval.revision, 8);
  assert.equal(ReviewState.draftView(state).status, "approved");
});

test("dirty draft conflicts with a new server revision until explicitly discarded", () => {
  let state = ReviewState.mergeServerView(ReviewState.createReviewState(), view());
  state = ReviewState.patchTask(state, "task-1", { prompt: "local dirty prompt" });
  state = ReviewState.mergeServerView(
    state,
    view({ revision: 8, taskOnePrompt: "new server prompt" }),
  );

  assert.equal(ReviewState.conflictMessage(state), "服务端计划已更新，请确认/刷新");
  assert.equal(ReviewState.canApprove(state), false);
  assert.throws(() => ReviewState.buildApprovalPayload(state), /服务端计划已更新/);
  assert.equal(ReviewState.draftView(state).approval.tasks[0].prompt, "local dirty prompt");

  state = ReviewState.discardLocalChanges(state);
  assert.equal(ReviewState.conflictMessage(state), "");
  assert.equal(ReviewState.draftView(state).approval.revision, 8);
  assert.equal(ReviewState.draftView(state).approval.tasks[0].prompt, "new server prompt");
  assert.deepEqual(ReviewState.selectedTaskIds(state), ["task-1", "task-2"]);
});

test("same-revision plan changes conflict when dirty and refresh immediately when clean", () => {
  const changedPlan = view({ taskOnePrompt: "same revision server rewrite" });
  let dirty = ReviewState.mergeServerView(ReviewState.createReviewState(), view());
  dirty = ReviewState.patchTask(dirty, "task-1", { prompt: "local draft" });
  dirty = ReviewState.mergeServerView(dirty, changedPlan);
  assert.equal(ReviewState.conflictMessage(dirty), "服务端计划已更新，请确认/刷新");
  assert.equal(ReviewState.draftView(dirty).approval.tasks[0].prompt, "local draft");

  let clean = ReviewState.mergeServerView(ReviewState.createReviewState(), view());
  clean = ReviewState.mergeServerView(clean, changedPlan);
  assert.equal(ReviewState.conflictMessage(clean), "");
  assert.equal(ReviewState.draftView(clean).approval.tasks[0].prompt, "same revision server rewrite");

  const summaryChanged = view();
  summaryChanged.approval.document_summary = "new plan summary";
  dirty = ReviewState.discardLocalChanges(dirty);
  dirty = ReviewState.patchTask(dirty, "task-1", { prompt: "another local draft" });
  dirty = ReviewState.mergeServerView(dirty, summaryChanged);
  assert.equal(ReviewState.conflictMessage(dirty), "服务端计划已更新，请确认/刷新");
});

test("server-side approved partial selection is rendered instead of selecting every task", () => {
  const state = ReviewState.mergeServerView(
    ReviewState.createReviewState(),
    view({ status: "approved", selectedTaskIds: ["task-2"] }),
  );

  assert.deepEqual(ReviewState.selectedTaskIds(state), ["task-2"]);
  assert.equal(ReviewState.draftView(state).approval.selected_task_ids[0], "task-2");
  assert.equal(ReviewState.canApprove(state), false);
});

test("reference revision changes conflict with dirty fields and discard removes ghost references", () => {
  let state = ReviewState.mergeServerView(ReviewState.createReviewState(), view());
  state = ReviewState.patchTask(state, "task-1", { prompt: "keep until confirmed" });
  const changed = view({ revision: 8 });
  changed.approval.tasks[0].reference_images = [
    { asset_id: "asset-2", role: "first_frame", order: 1 },
  ];
  changed.approval.media_assets = [{ asset_id: "asset-2", preview_url: "/asset-2" }];

  state = ReviewState.mergeServerView(state, changed);
  assert.equal(ReviewState.conflictMessage(state), "服务端计划已更新，请确认/刷新");
  assert.equal(ReviewState.draftView(state).approval.tasks[0].reference_images[0].asset_id, "asset-1");

  state = ReviewState.discardLocalChanges(state);
  const refreshed = ReviewState.draftView(state);
  assert.deepEqual(refreshed.approval.tasks[0].reference_images, [
    { asset_id: "asset-2", role: "first_frame", order: 1 },
  ]);
  assert.deepEqual(refreshed.approval.media_assets, [
    { asset_id: "asset-2", preview_url: "/asset-2" },
  ]);
});

test("switching to multi-reference converts every image role", () => {
  let state = ReviewState.mergeServerView(ReviewState.createReviewState(), view());
  state = ReviewState.patchTask(state, "task-1", {
    reference_images: [
      { asset_id: "asset-1", role: "first_frame", order: 1 },
      { asset_id: "asset-2", role: "last_frame", order: 2 },
    ],
  });

  state = ReviewState.setReferenceMode(state, "task-1", "multi_reference");

  const taskOne = ReviewState.draftView(state).approval.tasks[0];
  assert.equal(taskOne.reference_mode, "multi_reference");
  assert.deepEqual(
    taskOne.reference_images.map((reference) => reference.role),
    ["reference_image", "reference_image"],
  );
});

test("switching to frame mode requires exactly two images and assigns endpoints", () => {
  let state = ReviewState.mergeServerView(ReviewState.createReviewState(), view());
  assert.throws(
    () => ReviewState.setReferenceMode(state, "task-1", "first_last_frame"),
    /恰好两张/,
  );

  state = ReviewState.patchTask(state, "task-1", {
    reference_images: [
      { asset_id: "asset-1", role: "reference_image", order: 1 },
      { asset_id: "asset-2", role: "reference_image", order: 2 },
    ],
  });
  state = ReviewState.setReferenceMode(state, "task-1", "first_last_frame");

  const taskOne = ReviewState.draftView(state).approval.tasks[0];
  assert.equal(taskOne.reference_mode, "first_last_frame");
  assert.deepEqual(
    taskOne.reference_images.map((reference) => reference.role),
    ["first_frame", "last_frame"],
  );
});
