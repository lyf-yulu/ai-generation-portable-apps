(function (root, factory) {
  "use strict";

  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.ReferenceUploadState = api;
  }
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  function createState() {
    return { pendingByTaskId: {}, feedbackByTaskId: {} };
  }

  function withTask(state, taskId, { file, feedback }) {
    const pendingByTaskId = { ...(state.pendingByTaskId || {}) };
    const feedbackByTaskId = { ...(state.feedbackByTaskId || {}) };
    if (file === null) delete pendingByTaskId[taskId];
    else if (file !== undefined) pendingByTaskId[taskId] = file;
    if (feedback) feedbackByTaskId[taskId] = feedback;
    return { pendingByTaskId, feedbackByTaskId };
  }

  function createFeedback(phase, message) {
    return { phase, message };
  }

  function fileSelected(state, taskId, file) {
    return withTask(state, taskId, {
      file,
      feedback: createFeedback("selected", `已选择：${file.name}，点击“增添图片”上传`),
    });
  }

  function uploadStarted(state, taskId) {
    return withTask(state, taskId, {
      feedback: createFeedback("uploading", "正在上传图片…"),
    });
  }

  function uploadSucceeded(state, taskId) {
    return withTask(state, taskId, {
      file: null,
      feedback: createFeedback("success", "图片已加入参考列表"),
    });
  }

  function uploadFailed(state, taskId, message) {
    return withTask(state, taskId, {
      feedback: createFeedback("error", message || "图片添加失败，请重试"),
    });
  }

  function pendingFile(state, taskId) {
    return state.pendingByTaskId?.[taskId] || null;
  }

  function feedback(state, taskId) {
    return state.feedbackByTaskId?.[taskId] || null;
  }

  return {
    createState,
    feedback,
    fileSelected,
    pendingFile,
    uploadFailed,
    uploadStarted,
    uploadSucceeded,
  };
});
