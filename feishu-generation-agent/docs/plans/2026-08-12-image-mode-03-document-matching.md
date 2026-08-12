# 文档 → 素材库自动匹配（阶段 3）实施计划 — 骨架

> **状态：骨架。** 执行前需要按阶段 1 完整版填充每步的测试代码和实现代码。
> **前置依赖：** 阶段 1（素材库）+ 阶段 2（图片 pipeline）已完成。

**Goal:** planner 解析图片需求文档时，把文档里出现的角色名/别名自动从素材库挂载到对应 entry 的 reference_images 上，未命中的角色首次遇到时自动入库并 tag。

**Architecture:** 在 `plan_requirements` 节点之前插入 `resolve_character_assets` 步骤：先跑精确名/别名匹配（`AssetLibraryStore.find_by_match_key`），未命中的候选交给 DeepSeek 做一次「文档人物 vs 素材库」语义匹配。匹配到的 asset 转成 `ImageReference` 注入 planner 输入；仍未命中的、但文档里有明确参考图的角色，由新增的 `auto_ingest_character` 步骤自动建 asset 并 tag。

**Tech Stack:** 同阶段 1/2，无新增。

---

## 关键决策（来自 2026-08-12 对话）

1. **首次遇到自动入库**：文档里有人物名 + 该人物在文档里有参考图（image-N），且素材库无匹配 → 自动建 asset。
2. **同人不同着装分开入库并标注**：文档里若明确提到「Sarah 战斗装」这类描述，作为 `variant` 存；LLM 语义匹配需能识别「场景中 Sarah 穿的是战斗装 → 应挂 Sarah/战斗装 variant」。
3. **库要能编辑**：阶段 1 REST 已提供。本阶段的自动入库产物要能被 UI 后期修正。

---

## Task 0（前置验证）：真实文档跑一遍不匹配的基线

**Files:** 无改动，只跑

- [ ] 用阶段 2 Task 0 存的 fixture（`cg_requirement_day6.json`）跑当前 planner，确认现在的行为：文档里出现「Sarah / Victor / Sophia」时 planner 是从文档内取参考图，而不是找素材库
- [ ] 记录 planner 输出的 baseline，作为 Task 1-4 的对比参照

---

## Task 1: `CharacterMatcher` 领域模块（精确匹配）

**Files:**
- Create: `src/feishu_generation_agent/domain/character_matcher.py`
- Test: `tests/unit/test_character_matcher.py`

`CharacterMatcher` 输入：`NormalizedDocument.text_view` + `AssetLibraryStore`。
输出：`list[MatchedCharacter]`，每项包含 `name / asset_id / matched_key / occurrences: list[block_id]`。

精确匹配规则：
- 用简单的中文分词（不依赖新库，按标点/空格切）+ 英文单词切
- 每个 token 走 `normalize_alias`，再查 `find_by_match_key`
- 命中 → 记录该 asset 出现在哪些 block
- 同名多 variant 全部返回，让 LLM 语义匹配层决定挂哪个

- [ ] Step 1-5：TDD 五步

---

## Task 2: LLM 语义匹配层（未命中的走 DeepSeek）

**Files:**
- Create: `src/feishu_generation_agent/integrations/character_semantic_matcher.py`
- Test: `tests/unit/test_character_semantic_matcher.py`

调 DeepSeek 一次，输入：
- 文档 `text_view`
- 已通过 Task 1 精确匹配到的角色（作为「已知锚点」，避免 LLM 重复推理）
- 素材库全量 asset 的 `name/variant/aliases/description/tags`（若素材库超 200 条则先按 tag 粗筛）

输出（JSON schema 强约束）：
```
{
  "matches": [
    {"asset_id": "a1", "context_block_ids": ["doxcn..."], "confidence": 0.9, "reason": "..."},
    ...
  ],
  "unresolved_candidates": [
    {"proposed_name": "Mike", "context_block_ids": ["..."], "reason": "..."}
  ]
}
```

`unresolved_candidates` 是给 Task 4 自动入库用的候选。

- [ ] Step 1-5：TDD 五步（用 fake DeepSeek client）

---

## Task 3: 组合匹配器 + graph 节点

**Files:**
- Modify: `src/feishu_generation_agent/graph/builder.py`（在 `plan_requirements` 前插节点）
- Modify: `src/feishu_generation_agent/graph/nodes.py`（新增 `resolve_character_assets_node`）
- Modify: `src/feishu_generation_agent/graph/state.py`（`AgentState` 加 `character_matches` 字段）
- Test: `tests/unit/test_resolve_character_assets_node.py`

节点做的事：
1. 只在 mode="image" 时激活；video mode 直接透传
2. 跑 Task 1 精确匹配
3. 剩余的走 Task 2 语义匹配
4. 把结果塞进 `AgentState.character_matches`
5. planner 节点从 state 读匹配结果，注入到 prompt（"下列角色应从素材库挂载：..."）

- [ ] Step 1-5：TDD 五步

---

## Task 4: 自动入库

**Files:**
- Create: `src/feishu_generation_agent/graph/nodes/auto_ingest.py`（或加到 `nodes.py`）
- Modify: `src/feishu_generation_agent/graph/builder.py`
- Test: `tests/unit/test_auto_ingest.py`

节点做的事（只在 mode="image" 时激活）：
1. 遍历 Task 2 的 `unresolved_candidates`
2. 从 `NormalizedDocument.media_assets` 找到该候选人物名邻近的 image asset（策略：候选 `context_block_ids` 前后 N 个 block 里最近的 image block）
3. 用 `AssetLibraryStore.create` 建 asset：`name=proposed_name`、`variant="默认"`、`kind=CHARACTER`、`tags=["auto-ingested", "需求文档:{document_id}"]`、`description=context 摘要`
4. `content` 从 `MediaAsset.local_path` 读；`mime_type` 从 asset 拿
5. 建库成功后，加入 `character_matches` 让 planner 挂上
6. 建库失败（DuplicateAssetError 等）→ log warning 但不阻塞 planner（把这条候选降级为 "从文档现取参考图"）

- [ ] Step 1-5：TDD 五步

---

## Task 5: Planner prompt 注入

**Files:**
- Modify: `src/feishu_generation_agent/integrations/planner.py`（图片契约里加「已知角色」区段）
- Test: `tests/unit/test_planner_prompts.py`（追加）

planner 收到 `character_matches` 时，在 image prompt 契约里追加：

```
【素材库已匹配角色】
- Sarah / 晚宴礼服（asset_id=a1）：金色长发，蓝色眼睛
- Victor / 默认（asset_id=b3）：中年男性，络腮胡
生成 entry 时若画面出现上述角色，必须把对应 asset 挂到 reference_images 里，role="reference_image"，禁止用文档里的普通图片替代。
```

`ImageReference.asset_id` 走素材库 UUID 而非文档 asset-N；对应 provider 消费时用 `CharacterAsset.storage_url`。

- [ ] Step 1-5：TDD 五步

---

## Task 6: reference 消费链路对接素材库 URL

**Files:**
- Modify: `src/feishu_generation_agent/integrations/chiyun.py`（图片 provider 上传前 resolve 素材）
- Modify: `src/feishu_generation_agent/integrations/ark_seedream.py`（阶段 2 新建，同上）
- Test: 相关 provider 测试追加

改动：provider `submit()` 拿到 `task.reference_images` 时，先按 `asset_id` 判断是「文档 asset-N」还是「素材库 UUID」。素材库 UUID 从 `AssetLibraryStore.get()` 取 `storage_url` 消费；文档 asset 走原有逻辑（本地路径 → base64 或上传）。

- [ ] Step 1-5：TDD 五步

---

## Task 7: 火山镜像（可选，本阶段可延后）

**Files:**
- Create: `src/feishu_generation_agent/integrations/volcengine_assets.py`
- Test: `tests/unit/test_volcengine_assets.py`

复用 `volcengine-portrait/app.py:711-800` 的 `openapi_call` / `_upload_to_public_host` / `poll_asset_status`（搬进 agent，不 import 子应用）。

触发时机：seedance video / portrait video 消费素材时，若 `CharacterAsset.volcengine_asset_id` 为空 → 调 CreateAsset 上传，回填 asset_id。命名 `app_asset_0/1/2...`，单组满了才新建下一个。

**本阶段不做也行**：阶段 3 只解决「图片模式自动匹配」，视频模式不改。若后续视频模式也要用素材库，那时再补这个 Task。

- [ ] Step 1-5：TDD 五步

---

## Task 8: 端到端验证

- [ ] 手动录入 Sarah（晚宴礼服 + 战斗装）、Victor、Sophia 到素材库
- [ ] 跑真实需求文档：确认精确匹配 3 个人都命中
- [ ] 删掉 Sophia，再跑：确认 LLM 语义匹配层能识别「握手言和的两位女性」里的 Sophia，触发自动入库
- [ ] 检查自动入库产物：`tags` 含 `auto-ingested`，UI 上可编辑
- [ ] 跑一遍 `uv run pytest -q` 全绿
- [ ] 视频模式回归：跑一次视频文档，确认 `character_matches` 为空、planner 行为无变化

---

## 阶段 3 完成标准

- [ ] 图片文档里出现的已录入角色能自动挂载素材库参考图
- [ ] 未录入的角色首次遇到自动入库，`tags` 含 `auto-ingested`
- [ ] 同人不同 variant 时语义匹配能选对（Sarah/晚宴礼服 vs Sarah/战斗装）
- [ ] 素材库对 planner 是「已挂载」而非「候选」——planner 不会二次选择
- [ ] 视频 pipeline 行为零变化
- [ ] 自动入库产物可通过 REST PATCH 修正

---

## 未来路标（阶段 3 完成后可评估）

写进 memory 的三阶段演进：

- **阶段 A（当前）** 本机主存，LAN URL
- **阶段 B（上公网服务器时）** URL 换公网域名，`storage_url` 快照批量重写；火山 CreateAsset 直接吃自家公网 URL 扔掉 uguu.se
- **阶段 C（团队扩大 / 多机部署）** 存储切对象存储（阿里 OSS / R2），SQLite 可评估升级 Postgres；`storage_path` 语义从「本地路径」变「对象 key」，`storage_url` 走 CDN

三阶段的**共同不变式**：`AssetLibraryStore.create/get/update/delete/find_by_match_key` 五个 API 签名保持稳定；火山镜像层只是 provider adapter，存储层可换。
