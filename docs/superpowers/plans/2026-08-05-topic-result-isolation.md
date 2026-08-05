# Topic Result Isolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Seedance and Nano Banana topic tabs render only the progress, logs, and result belonging to the task submitted from that tab, including during rapid tab switches and concurrent tasks.

**Architecture:** Snapshot the owner workspace before every submit and pass it explicitly through request, polling, cache, and rendering. Persist that workspace on backend job records and reject mismatched frontend snapshots before they reach the DOM. Keep the Seedance user-level generation history global and unchanged.

**Tech Stack:** Browser JavaScript/PetiteVue, Python 3.12 FastAPI and stdlib compatibility handlers, Node.js behavioral tests, pytest.

## Global Constraints

- Seedance “生成历史” remains user-level global history and must not be filtered by active topic.
- No new browser build tool or runtime dependency.
- Existing jobs are in memory; production restart waits until all jobs are terminal.
- Seedance and Nano Banana must follow the same topic-isolation contract.
- Preserve unrelated tracked and untracked workspace changes.

---

### Task 1: Persist backend task ownership

**Files:**
- Modify: `seedance/app.py`
- Verify: `nano-banana/app.py`
- Create: `tests/test_job_workspace_ownership.py`

**Interfaces:**
- Consumes: `create_job(..., ws_id: str, username: str) -> str`
- Produces: every new `JOBS[job_id]` record and Seedance activity record contains the exact submitted `workspace_id`

- [ ] **Step 1: Write failing backend ownership tests**

Create isolated temporary state and call each app’s real `create_job()` with `ws-a`. Stub only the background thread start so no external generation runs. Assert the stored job has literal `workspace_id == "ws-a"`; for Seedance also assert the persisted activity record has the same value.

- [ ] **Step 2: Run the tests and verify the Seedance assertions fail**

Run: `python3 -m pytest tests/test_job_workspace_ownership.py -q`

Expected: Seedance job ownership is missing and its activity ownership is `localhost`; Nano Banana characterizes the already-working behavior.

- [ ] **Step 3: Store and propagate Seedance ownership**

Add `"workspace_id": ws_id` to the Seedance in-memory job dictionary and call:

```python
record_activity({...}, ws_id)
```

Do not change the user-level `/api/jobs` list filtering or ordering.

- [ ] **Step 4: Run the backend ownership tests**

Run: `python3 -m pytest tests/test_job_workspace_ownership.py -q`

Expected: all tests pass.

### Task 2: Lock asynchronous requests to the submitting topic

**Files:**
- Modify: `seedance/static/app.js`
- Modify: `nano-banana/static/app.js`
- Create: `tests/test_topic_async_isolation.mjs`

**Interfaces:**
- Produces: `api(url, method, body, workspaceOverride)` and `pollJobOnce(url, workspaceOverride)`
- Produces: `pollJob(jobId, ownerWorkspaceId, submissionToken)` whose writes are scoped to the owner

- [ ] **Step 1: Write a failing submit-switch test for both apps**

Use each real app factory in a VM DOM harness. Delay the POST response, submit from `ws-a`, switch to `ws-b`, then resolve the response. Assert the POST and all job-detail polls use `ws-a`, and no `ws-a` state appears in the active `ws-b` DOM or cache.

- [ ] **Step 2: Verify both tests fail for the ownership race**

Run: `node tests/test_topic_async_isolation.mjs`

Expected: the first poll is associated with `ws-b` or reads the current global workspace.

- [ ] **Step 3: Pass the owner explicitly through submit and polling**

At the beginning of `submit()`, capture:

```javascript
const ownerWorkspaceId = this.activeTabId;
```

Route submission status through the owner cache when the user switches during the POST. Call `api(..., ownerWorkspaceId)` and `pollJob(jobId, ownerWorkspaceId, submissionToken)`. Every poll calls `pollJobOnce(..., ownerWorkspaceId)`.

- [ ] **Step 4: Ignore stale same-topic pollers**

Maintain a monotonically increasing per-topic submission token/current job marker. Only the newest task for a topic may update that topic’s live status, latest-result snapshot, DOM, or auto-download action. Older tasks may finish in the backend and remain in global history without overwriting the newer result.

- [ ] **Step 5: Run the asynchronous isolation tests**

Run: `node tests/test_topic_async_isolation.mjs`

Expected: both app suites pass.

### Task 3: Guard cache restoration and clear new topics

**Files:**
- Modify: `seedance/static/app.js`
- Modify: `nano-banana/static/app.js`
- Create: `tests/test_topic_cache_isolation.mjs`

**Interfaces:**
- Consumes: backend job snapshots with `workspace_id`
- Produces: cache snapshots are rendered only when `job.workspace_id === activeTabId`

- [ ] **Step 1: Write failing cache and new-topic tests**

For both apps, seed the result DOM with an old task, call `newTab()`, and assert the result DOM is empty. Seed a target tab cache with a job owned by another workspace, call `loadTargetTabState()`, and assert it is not rendered. Verify a matching snapshot is restored normally.

- [ ] **Step 2: Verify the tests fail on stale DOM and mismatched cache**

Run: `node tests/test_topic_cache_isolation.mjs`

Expected: stale output remains after `newTab()` and a mismatched `_latestJob` is rendered.

- [ ] **Step 3: Clear and validate topic results**

Add a small per-app result reset helper used by `newTab()` and empty-cache restoration. Before caching or rendering a polled job, require its `workspace_id` to match `ownerWorkspaceId`; on mismatch set an isolation error only in the owner topic and stop that poll without touching the active topic.

When a running topic is forcibly closed, polling may finish for backend bookkeeping but must not recreate the deleted topic cache or render into another topic.

- [ ] **Step 4: Run cache-isolation and existing render tests**

Run:

```bash
node tests/test_topic_cache_isolation.mjs
node tests/test_seedance_topic_render.mjs
node tests/test_nano_provider_switch.mjs
```

Expected: all tests pass.

### Task 4: Verify global history and full regression surface

**Files:**
- Modify if necessary: `tests/test_jobs_list_workspace_id.py`
- Verify: `seedance/static/index.html`
- Verify: `seedance/static/app.js`

**Interfaces:**
- Preserves: `visibleJobs()` returns the user-level global jobs list limited by `jobsLimit`

- [ ] **Step 1: Add or retain an explicit global-history assertion**

The test fixture contains jobs from `ws-a` and `ws-b`, sets active topic to `ws-a`, and asserts Seedance generation history still exposes both jobs. This prevents a future isolation fix from accidentally changing user-level history.

- [ ] **Step 2: Run focused regression tests**

Run all new Node and pytest tests plus existing topic/provider/job-list tests.

- [ ] **Step 3: Run the broader relevant suite**

Run the repository’s selected parallel-result, workspace, FastAPI parity, Seedance, and Nano Banana tests. Record unrelated pre-existing failures separately rather than changing unrelated code.

- [ ] **Step 4: Inspect the final diff and production job state**

Confirm the diff contains no history filtering and no unrelated files. Query current sub-app job lists before any restart; do not restart while a job is non-terminal.

- [ ] **Step 5: Commit implementation**

Stage only the topic-isolation source and test files, then create a new commit without amending existing history.
