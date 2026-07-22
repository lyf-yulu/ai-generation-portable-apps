(function (root, factory) {
  "use strict";

  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.BitableState = api;
  }
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  function createState() {
    return {
      tasks: [],
      scan: { phase: "idle", error: "" },
      claim: { phase: "idle", recordId: null, runId: null, error: "" },
      deliveryRetry: { phase: "idle", runId: null, error: "" },
      recentRuns: [],
    };
  }

  function scanStarted(state) {
    return { ...state, scan: { phase: "loading", error: "" } };
  }

  function scanSucceeded(state, tasks) {
    return {
      ...state,
      tasks: Array.isArray(tasks) ? JSON.parse(JSON.stringify(tasks)) : [],
      scan: { phase: "ready", error: "" },
    };
  }

  function scanFailed(state, error) {
    return {
      ...state,
      scan: { phase: "error", error: String(error || "扫描失败") },
    };
  }

  function claimStarted(state, recordId) {
    return {
      ...state,
      claim: { phase: "loading", recordId, runId: null, error: "" },
    };
  }

  function claimSucceeded(state, runId) {
    const recordId = state.claim.recordId;
    return {
      ...state,
      tasks: state.tasks.filter((task) => task.record_id !== recordId),
      claim: { phase: "ready", recordId, runId, error: "" },
    };
  }

  function claimConflict(state, error) {
    return {
      ...state,
      claim: {
        ...state.claim,
        phase: "conflict",
        error: String(error || "该任务已被领取"),
      },
    };
  }

  function retryStarted(state, runId) {
    return {
      ...state,
      deliveryRetry: { phase: "loading", runId, error: "" },
    };
  }

  function retrySucceeded(state) {
    return {
      ...state,
      deliveryRetry: { ...state.deliveryRetry, phase: "ready", error: "" },
    };
  }

  function retryFailed(state, error) {
    return {
      ...state,
      deliveryRetry: {
        ...state.deliveryRetry,
        phase: "error",
        error: String(error || "交付重试失败"),
      },
    };
  }

  function recentSucceeded(state, recentRuns) {
    return {
      ...state,
      recentRuns: Array.isArray(recentRuns) ? JSON.parse(JSON.stringify(recentRuns)) : [],
    };
  }

  function resetRunContext(state) {
    return {
      ...state,
      claim: { phase: "idle", recordId: null, runId: null, error: "" },
      deliveryRetry: { phase: "idle", runId: null, error: "" },
    };
  }

  return {
    createState,
    scanStarted,
    scanSucceeded,
    scanFailed,
    claimStarted,
    claimSucceeded,
    claimConflict,
    retryStarted,
    retrySucceeded,
    retryFailed,
    recentSucceeded,
    resetRunContext,
  };
});
