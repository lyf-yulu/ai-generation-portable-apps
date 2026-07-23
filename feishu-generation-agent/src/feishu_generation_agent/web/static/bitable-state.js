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

  const CATEGORY_NAMES = new Set(["animation", "portrait"]);

  function createCategoryState() {
    return {
      tasks: [],
      scan: { phase: "idle", error: "" },
    };
  }

  function categoryState(state, category) {
    if (!CATEGORY_NAMES.has(category)) throw new Error("未知任务类别");
    return state.categories[category];
  }

  function withCategory(state, category, nextCategoryState) {
    categoryState(state, category);
    return {
      ...state,
      categories: {
        ...state.categories,
        [category]: nextCategoryState,
      },
    };
  }

  function createState() {
    return {
      activeCategory: "animation",
      categories: {
        animation: createCategoryState(),
        portrait: createCategoryState(),
      },
      claim: {
        phase: "idle",
        recordId: null,
        runId: null,
        category: null,
        error: "",
      },
      deliveryRetry: { phase: "idle", runId: null, error: "" },
      recentRuns: [],
    };
  }

  function selectCategory(state, category) {
    categoryState(state, category);
    return { ...state, activeCategory: category };
  }

  function activeCategoryState(state) {
    return categoryState(state, state.activeCategory);
  }

  function scanStarted(state, category) {
    const current = categoryState(state, category);
    return withCategory(state, category, {
      ...current,
      scan: { phase: "loading", error: "" },
    });
  }

  function scanSucceeded(state, category, tasks) {
    const current = categoryState(state, category);
    return withCategory(state, category, {
      ...current,
      tasks: Array.isArray(tasks) ? JSON.parse(JSON.stringify(tasks)) : [],
      scan: { phase: "ready", error: "" },
    });
  }

  function scanFailed(state, category, error) {
    const current = categoryState(state, category);
    return withCategory(state, category, {
      ...current,
      scan: { phase: "error", error: String(error || "扫描失败") },
    });
  }

  function claimStarted(state, recordId, category) {
    categoryState(state, category);
    return {
      ...state,
      claim: { phase: "loading", recordId, runId: null, category, error: "" },
    };
  }

  function claimSucceeded(state, runId) {
    const recordId = state.claim.recordId;
    const category = state.claim.category;
    const nextState = withCategory(state, category, {
      ...categoryState(state, category),
      tasks: categoryState(state, category).tasks.filter(
        (task) => task.record_id !== recordId,
      ),
    });
    return {
      ...nextState,
      claim: { ...state.claim, phase: "ready", recordId, runId, error: "" },
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
      claim: {
        phase: "idle",
        recordId: null,
        runId: null,
        category: null,
        error: "",
      },
      deliveryRetry: { phase: "idle", runId: null, error: "" },
    };
  }

  return {
    createState,
    selectCategory,
    activeCategoryState,
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
