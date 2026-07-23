# Portrait Small Image Normalization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make undersized portrait reference images acceptable to Volcengine Asset without changing source files, and expose deterministic Volcengine 4xx failures accurately.

**Architecture:** Keep the change inside the portrait Asset integration boundary. A focused helper produces upload bytes from a `MediaAsset`; compliant images pass through unchanged, while undersized images are decoded and resized in memory before public hosting. The Asset HTTP client separately maps network/5xx failures to retryable errors and 4xx provider validation failures to terminal errors.

**Tech Stack:** Python 3.12, Pillow 11–12, httpx, pytest

## Global Constraints

- Only the `volcengine_portrait` path may normalize source images.
- Do not modify Feishu documents or files under `data/runs/.../inputs`.
- Resize only when width or height is below 300px.
- Preserve aspect ratio and use Lanczos resampling.
- Do not change animation, Seedance direct, Chiyun, or Nano Banana behavior.
- No new dependency is required; Pillow is already declared.

---

### Task 1: Normalize undersized portrait upload bytes

**Files:**
- Modify: `feishu-generation-agent/src/feishu_generation_agent/integrations/volcengine_portrait.py`
- Test: `feishu-generation-agent/tests/unit/test_volcengine_portrait.py`

**Interfaces:**
- Consumes: `MediaAsset.local_path`, `MediaAsset.mime_type`, `MediaAsset.width`, and `MediaAsset.height`.
- Produces: `_portrait_upload_content(asset: MediaAsset) -> bytes`.

- [ ] **Step 1: Write failing tests**

Add tests that create a real `216×384` PNG and assert the bytes received by the public host decode to `300×534`. Assert the source file hash is unchanged. Add a second test with a compliant image and assert the upload bytes exactly equal the original bytes.

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
uv run pytest tests/unit/test_volcengine_portrait.py -q
```

Expected: the undersized-image test fails because the public host still receives `216×384`.

- [ ] **Step 3: Implement minimal normalization**

Implement `_portrait_upload_content` with Pillow:

```python
def _portrait_upload_content(asset: MediaAsset) -> bytes:
    content = asset.local_path.read_bytes()
    with Image.open(BytesIO(content)) as image:
        width, height = image.size
        if width >= 300 and height >= 300:
            return content
        scale = max(300 / width, 300 / height)
        target = (ceil(width * scale), ceil(height * scale))
        resized = image.resize(target, Image.Resampling.LANCZOS)
        output = BytesIO()
        resized.save(output, format=image.format)
        return output.getvalue()
```

Call this helper only from `VolcengineAssetClient.ensure_image_asset` before `public_media_host.upload`.

- [ ] **Step 4: Run tests and verify GREEN**

Run:

```bash
uv run pytest tests/unit/test_volcengine_portrait.py -q
```

Expected: all portrait tests pass.

- [ ] **Step 5: Commit**

```bash
git add feishu-generation-agent/src/feishu_generation_agent/integrations/volcengine_portrait.py feishu-generation-agent/tests/unit/test_volcengine_portrait.py
git commit -m "fix(agent): normalize small portrait references"
```

### Task 2: Preserve Volcengine Asset 4xx diagnostics

**Files:**
- Modify: `feishu-generation-agent/src/feishu_generation_agent/integrations/volcengine_portrait.py`
- Test: `feishu-generation-agent/tests/unit/test_volcengine_portrait.py`

**Interfaces:**
- Consumes: Volcengine `ResponseMetadata.Error.Code` and `Message`.
- Produces: terminal `AgentError` for HTTP 4xx; retryable `AgentError` for network failures and HTTP 5xx.

- [ ] **Step 1: Write failing tests**

Add a mock response with HTTP 400 and:

```json
{
  "ResponseMetadata": {
    "Error": {
      "Code": "InvalidParameter.WidthTooSmall",
      "Message": "Width must be between 300px and 6000px."
    }
  }
}
```

Assert `ErrorCategory.PROVIDER_TERMINAL`, `retryable is False`, and that the technical detail contains both the error code and message. Retain a separate HTTP 503 test asserting `ErrorCategory.TRANSIENT`.

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
uv run pytest tests/unit/test_volcengine_portrait.py -q
```

Expected: the HTTP 400 test reports a transient error.

- [ ] **Step 3: Implement response classification**

Handle transport errors separately. For received responses, parse the JSON body before status classification. Map 400–499 to `_terminal_error` with sanitized error code/message; map 500–599 and invalid JSON responses to `_transient_error`.

- [ ] **Step 4: Run targeted and full verification**

Run:

```bash
uv run pytest tests/unit/test_volcengine_portrait.py tests/unit/test_graph_nodes.py -q
uv run pytest -q
node --test tests/frontend/*.test.cjs
```

Expected: targeted tests pass, all backend tests pass, and all 23 frontend tests pass.

- [ ] **Step 5: Commit**

```bash
git add feishu-generation-agent/src/feishu_generation_agent/integrations/volcengine_portrait.py feishu-generation-agent/tests/unit/test_volcengine_portrait.py
git commit -m "fix(agent): classify portrait asset API errors"
```

### Task 3: Integrate and verify the real task path

**Files:**
- No source changes expected.

**Interfaces:**
- Consumes: merged portrait normalization and error mapping.
- Produces: a restarted local service ready for the user to rerun “茶壶青蛙”.

- [ ] **Step 1: Merge the feature branch into `main`**

Use a non-fast-forward merge and preserve unrelated untracked files.

- [ ] **Step 2: Verify no task is actively generating**

Query `/api/bitable/active-runs`; a waiting-approval task is safe, but do not restart while a provider task is generating.

- [ ] **Step 3: Restart and verify**

Run:

```bash
launchctl kickstart -k gui/$(id -u)/com.feishu-generation-agent
curl -fsS http://127.0.0.1:8765/api/health
```

Expected: a new process listens on port 8765 and health returns `"ready": true`.

- [ ] **Step 4: Validate preprocessing against the real first reference**

Run the normalization helper against the cached first reference and assert:

- source SHA-256 remains `1f9ebb4832c60ebe56eaa264ecf21206e3862d93316c9dd2b5fab3e7144cf1a5`;
- upload copy is `300×534`;
- the task remains available as rerunnable.

