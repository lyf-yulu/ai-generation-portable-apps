(function (root, factory) {
  "use strict";

  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.ReferenceMutationState = api;
  }
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  function createState() {
    return { rows: {}, tasks: {} };
  }

  function rowKey(taskId, assetId) {
    return `${taskId}::${assetId}`;
  }

  function start(state, taskId, assetId, action, filename = "") {
    const rows = { ...(state.rows || {}) };
    const tasks = { ...(state.tasks || {}) };
    delete tasks[taskId];
    rows[rowKey(taskId, assetId)] = {
      taskId,
      assetId,
      action,
      phase: action === "delete" ? "deleting" : "uploading",
      message: action === "delete"
        ? "正在删除并重新编号…"
        : `正在替换${filename ? ` ${filename}` : ""}…`,
    };
    return { rows, tasks };
  }

  function succeed(state, taskId, assetId, message) {
    const rows = { ...(state.rows || {}) };
    const tasks = { ...(state.tasks || {}) };
    delete rows[rowKey(taskId, assetId)];
    tasks[taskId] = { phase: "success", message };
    return { rows, tasks };
  }

  function fail(state, taskId, assetId, message) {
    const rows = { ...(state.rows || {}) };
    rows[rowKey(taskId, assetId)] = {
      ...(rows[rowKey(taskId, assetId)] || { taskId, assetId }),
      phase: "error",
      message: message || "参考素材操作失败，请重试",
    };
    return { rows, tasks: { ...(state.tasks || {}) } };
  }

  function rowFeedback(state, taskId, assetId) {
    const row = state.rows?.[rowKey(taskId, assetId)];
    return row ? { phase: row.phase, message: row.message } : null;
  }

  function taskFeedback(state, taskId) {
    return state.tasks?.[taskId] || null;
  }

  function isBusy(state, taskId, assetId) {
    const phase = state.rows?.[rowKey(taskId, assetId)]?.phase;
    return phase === "uploading" || phase === "deleting";
  }

  return {
    createState,
    fail,
    isBusy,
    rowFeedback,
    start,
    succeed,
    taskFeedback,
  };
});
