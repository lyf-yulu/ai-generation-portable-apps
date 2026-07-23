# Seedance 多模态参考素材 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 支持多参考模式下的图片、视频与音频；获批的视频和音频使用临时匿名 HTTPS URL 提交官方 Seedance。

**Architecture:** 保留 `reference_images` 的序列化字段以兼容既有运行，但扩展引用角色和 MIME 校验。新增 `PublicMediaHost`，其 Uguu 实现只在生成提交前上传视频和音频；Seedance 保持图片 data URL 路径。前端复用轮询安全的上传状态模块，按媒体类型展示预览。

**Tech Stack:** Python 3.12、FastAPI、Pydantic、httpx、pytest、原生 ES module、node:test。

---

## 变更文件

- `src/feishu_generation_agent/domain/plan.py`：角色和模式规则。
- `src/feishu_generation_agent/storage/files.py`：媒体白名单、签名与大小验证。
- `src/feishu_generation_agent/integrations/public_media.py`：托管端口和 Uguu 适配器。
- `src/feishu_generation_agent/integrations/seedance.py`：多模态 payload。
- `src/feishu_generation_agent/{bootstrap.py,graph/runtime.py,web/{app.py,schemas.py}}`：注入、媒体 API 与审核校验。
- `src/feishu_generation_agent/web/static/{app.js,index.html,styles.css,review-state.js,reference-upload-state.js}`：交互与预览。
- `tests/unit/{test_domain.py,test_seedance.py,test_public_media.py}`、`tests/integration/test_api.py`、`tests/graph/test_execution_graph.py`、`tests/frontend/{review_state.test.cjs,reference_upload_state.test.cjs}`：行为覆盖。

### Task 1: 扩展领域引用和模式校验

**Files:** Modify `src/feishu_generation_agent/domain/plan.py`; Test `tests/unit/test_domain.py`.

- [ ] **Step 1: 写失败测试**

```python
def test_multi_reference_accepts_video_and_audio_roles() -> None:
    task = GenerationTask.model_validate(task_payload("video") | {
        "reference_images": [
            {"asset_id": "image-1", "role": "reference_image", "order": 1},
            {"asset_id": "video-1", "role": "reference_video", "order": 2},
            {"asset_id": "audio-1", "role": "reference_audio", "order": 3},
        ],
    })
    assert [ref.role for ref in task.reference_images] == ["reference_image", "reference_video", "reference_audio"]

def test_first_last_frame_rejects_media_reference_roles() -> None:
    with pytest.raises(ValidationError, match="首尾帧模式"):
        GenerationTask.model_validate(task_payload("video") | {
            "reference_mode": "first_last_frame",
            "reference_images": [
                {"asset_id": "first", "role": "first_frame", "order": 1},
                {"asset_id": "audio", "role": "reference_audio", "order": 2},
            ],
        })
```

- [ ] **Step 2: 运行并确认失败** — `pytest tests/unit/test_domain.py -k 'media_reference_roles or first_last_frame_rejects_media' -v`；预期因角色不支持而失败。

- [ ] **Step 3: 实现最小规则** — 在 `ImageReference` 角色字面量添加 `reference_video`、`reference_audio`。在 `GenerationTask` 中令首尾帧模式只接受排序后的 `first_frame`、`last_frame`；多参考模式拒绝这两个角色；图生图继续只接受 `reference_image`。

- [ ] **Step 4: 验证并提交** — `pytest tests/unit/test_domain.py -v` 预期 PASS；随后 `git add src/feishu_generation_agent/domain/plan.py tests/unit/test_domain.py && git commit -m "feat(agent): model multimodal references"`。

### Task 2: 验证和保存本地视频、音频

**Files:** Modify `src/feishu_generation_agent/storage/files.py`, `src/feishu_generation_agent/graph/runtime.py`, `src/feishu_generation_agent/web/app.py`; Test `tests/integration/test_api.py`.

- [ ] **Step 1: 写失败测试**

```python
async def test_reference_upload_accepts_mp4_and_rejects_unknown_media(client, run_id):
    accepted = await client.post(
        f"/api/runs/{run_id}/references",
        data={"task_id": "task-1", "role": "reference_video", "order": "2"},
        files={"file": ("clip.mp4", b"\x00\x00\x00\x18ftypisom", "video/mp4")},
    )
    assert accepted.status_code == 200
    rejected = await client.post(
        f"/api/runs/{run_id}/references",
        data={"task_id": "task-1", "role": "reference_audio", "order": "3"},
        files={"file": ("bad.bin", b"not-audio", "audio/mpeg")},
    )
    assert rejected.status_code == 422
```

- [ ] **Step 2: 运行并确认失败** — `pytest tests/integration/test_api.py -k reference_upload_accepts_mp4 -v`；预期运行时只接受图片而失败。

- [ ] **Step 3: 实现最小校验** — 在 `FileStore` 增加可信 MIME 白名单：MP4/MOV/WebM、MP3/WAV/M4A/AAC/OGG；检查 `ftyp`、EBML、OggS、RIFF/WAVE、ID3/MP3 帧或 ADTS 签名。拒绝 MIME 与签名不匹配的内容。`AgentRuntime.add_reference()` 依可信 MIME 限制角色：图片→普通/首尾帧，视频→`reference_video`，音频→`reference_audio`；`get_reference_file()` 返回任意受信媒体。

- [ ] **Step 4: 验证并提交** — `pytest tests/integration/test_api.py -k 'reference_upload or reference_content' -v` 预期 PASS；随后 `git add src/feishu_generation_agent/storage/files.py src/feishu_generation_agent/graph/runtime.py src/feishu_generation_agent/web/app.py tests/integration/test_api.py && git commit -m "feat(agent): accept validated reference media"`。

### Task 3: 实现独立的临时托管适配器

**Files:** Create `src/feishu_generation_agent/integrations/public_media.py`; Test `tests/unit/test_public_media.py`.

- [ ] **Step 1: 写失败测试**

```python
@pytest.mark.anyio
async def test_uguu_host_returns_https_url(httpx_mock) -> None:
    httpx_mock.add_response(url="https://uguu.se/upload.php", json={"files": [{"url": "https://a.uguu.se/token.mp4"}]})
    host = UguuPublicMediaHost(httpx.AsyncClient())
    assert await host.upload(b"video", "clip.mp4", "video/mp4") == "https://a.uguu.se/token.mp4"

@pytest.mark.anyio
async def test_uguu_host_rejects_non_https_url(httpx_mock) -> None:
    httpx_mock.add_response(url="https://uguu.se/upload.php", json={"files": [{"url": "http://bad"}]})
    with pytest.raises(PublicMediaUploadError, match="HTTPS"):
        await UguuPublicMediaHost(httpx.AsyncClient()).upload(b"audio", "a.mp3", "audio/mpeg")
```

- [ ] **Step 2: 运行并确认失败** — `pytest tests/unit/test_public_media.py -v`；预期模块不存在。

- [ ] **Step 3: 实现最小适配器** — 定义 `PublicMediaHost.upload(content, filename, mime_type) -> str` 和 `PublicMediaUploadError`。`UguuPublicMediaHost` 用 `httpx.AsyncClient.post("https://uguu.se/upload.php", files={"files[]": (...)}, timeout=60)` 上传，将网络、HTTP、JSON、缺失 URL、非 HTTPS 和带用户信息 URL 归一化为不含素材内容的异常。

- [ ] **Step 4: 验证并提交** — `pytest tests/unit/test_public_media.py -v` 预期 PASS；随后 `git add src/feishu_generation_agent/integrations/public_media.py tests/unit/test_public_media.py && git commit -m "feat(agent): add temporary public media host"`。

### Task 4: 组装官方 Seedance 多模态请求

**Files:** Modify `src/feishu_generation_agent/integrations/seedance.py`, `src/feishu_generation_agent/bootstrap.py`; Test `tests/unit/test_seedance.py`, `tests/graph/test_execution_graph.py`.

- [ ] **Step 1: 写失败测试**

```python
@pytest.mark.anyio
async def test_submit_uses_data_url_for_image_and_public_urls_for_video_audio(fake_client):
    generator = SeedanceVideoGenerator(client=fake_client, public_media_host=FakePublicMediaHost({
        "video-1": "https://public.example/clip.mp4", "audio-1": "https://public.example/music.mp3",
    }))
    await generator.submit(multimodal_task(), multimodal_assets())
    content = fake_client.request.json["content"]
    assert content[1]["image_url"]["url"].startswith("data:image/")
    assert content[2]["video_url"]["url"] == "https://public.example/clip.mp4"
    assert content[3]["audio_url"]["url"] == "https://public.example/music.mp3"
```

- [ ] **Step 2: 运行并确认失败** — `pytest tests/unit/test_seedance.py -k public_urls_for_video_audio -v`；预期生成器拒绝非图片资产。

- [ ] **Step 3: 实现最小 payload 分支** — 将 `PublicMediaHost` 注入 `SeedanceVideoGenerator`。图片生成 `image_url` data URL；`reference_video` 生成 `video_url`，`reference_audio` 生成 `audio_url`。按 `asset_id` 缓存本次 `submit()` 获取到的 URL。托管异常必须在首次上游提交前抛出；Graph 测试确认没有 provider job 被创建且任务沿既有失败路径释放锁。

- [ ] **Step 4: 验证并提交** — `pytest tests/unit/test_seedance.py tests/graph/test_execution_graph.py -v` 预期 PASS；随后 `git add src/feishu_generation_agent/integrations/seedance.py src/feishu_generation_agent/bootstrap.py tests/unit/test_seedance.py tests/graph/test_execution_graph.py && git commit -m "feat(agent): submit multimodal Seedance references"`。

### Task 5: 完成审批页交互与回归验证

**Files:** Modify `src/feishu_generation_agent/web/static/{app.js,index.html,styles.css,review-state.js,reference-upload-state.js}`; Test `tests/frontend/{review_state.test.cjs,reference_upload_state.test.cjs}`.

- [ ] **Step 1: 写失败测试**

```javascript
test("keeps selected audio and labels its category", () => {
  const next = UploadState.select(UploadState.initial(), "task-1", { name: "music.mp3", type: "audio/mpeg" });
  assert.equal(next.byTask["task-1"].label, "已选择音频：music.mp3");
});

test("normalizes video or audio references into multi reference mode", () => {
  const task = ReviewState.normalizeReferenceMode({
    reference_mode: "first_last_frame",
    reference_images: [{ asset_id: "video", role: "reference_video", order: 1 }],
  });
  assert.equal(task.reference_mode, "multi_reference");
});
```

- [ ] **Step 2: 运行并确认失败** — `node --test tests/frontend/reference_upload_state.test.cjs tests/frontend/review_state.test.cjs`；预期状态模块只识别图片而失败。

- [ ] **Step 3: 实现最小 UI** — 文件选择器接受白名单图片/视频/音频，状态文字显示素材类型。多参考模式渲染通用添加器、图片缩略图、`<video preload="metadata" muted controls playsinline>` 和 `<audio controls preload="metadata">`；首尾帧仅展示图片槽位。存在视频或音频时显示第三方临时托管不可撤回提示。保持现有删除、排序、轮询和防丢选择文件逻辑。

- [ ] **Step 4: 完整验证与提交** — `pytest -q && node --test tests/frontend/*.test.cjs` 预期 PASS；在 `http://127.0.0.1:8765/` 以非生产执行方式确认选择反馈、预览、模式限制和删除状态，不创建生产任务、不写生产表；随后 `git add src/feishu_generation_agent/web/static tests/frontend && git commit -m "feat(agent): edit multimodal references in review"`。
