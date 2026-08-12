# 图片模式 pipeline（阶段 2）实施计划 — 骨架

> **状态：骨架。** 执行前需要先把每个 Task 的测试代码和实现代码补全（阶段 1 那份是完整范例）。
> **前置依赖：** 阶段 1（素材库）已完成，`CharacterAsset.storage_url` / `prompt_fragment` 可用。

**Goal:** 让飞书 agent 能把「CG 图需求文档」解析成图片计划并调用 seedream / banana / gpt-image2 出图，视频 pipeline 保持不变。

**Architecture:** 复用现有 11 个 graph 节点（编排层通用），只替换节点内部策略：planner 按 mode 装配不同 system prompt、prompt validator 按 mode 分流、provider 从单实例改成 registry。`TaskType.IMAGE_TO_IMAGE` 已存在，本阶段是把它「接通」而非新建。

**Tech Stack:** 同阶段 1。新增复用 `nano-banana/providers.json` 的三种 `api_style`（`openai_images` / `gemini_generate_content` / `ark_seedream`）。

---

## 关键现状（Explore 已确认，执行时复核行号）

| 位置 | 现状 | 本阶段要做的 |
|---|---|---|
| `domain/plan.py:7-12` | `TaskType.IMAGE_TO_IMAGE` 已存在 | 不动枚举；补图片专属字段校验 |
| `domain/plan.py:54-73` | `GenerationTask` 视频/图片字段挤在一个类，`validate_type_specific_fields` 手写分支 | 加图片字段（`image_provider`、`size_variants`），分支里补图片校验 |
| `integrations/planner.py:96-124` | `_SEEDANCE_PLANNING_CONTRACT` / `_PLAN_SYSTEM_PROMPT` 把 Seedance 视频契约写死 | 拆成 video / image 两套契约，`plan()` 按 mode 装配 |
| `domain/reference_contract.py:175-275` | `validate_seedance_prompt` 是纯视频契约 | 新增 `validate_image_prompt`，视频那个原样保留 |
| `graph/nodes.py:883-890` | `_generator_for_task` 硬编码 `IMAGE_TO_IMAGE → services.image_generator` | 改成按 `task.image_provider` 查 registry |
| `graph/nodes.py:75-76` | `GraphServices.image_generator` 单实例 | 改 `image_providers: Mapping[str, ImageGenerator]` |
| `graph/nodes.py:785-796` | `_execution_units` 只对 `IMAGE_TO_VIDEO` 拆多输出 | 放开 `IMAGE_TO_IMAGE` 多输出 |
| `integrations/production_bitable.py:39-45` | `deliverable` 只认「动画类」「真人类」 | 加「图片类」 |
| `bitable/production_service.py:57` | `enabled_task_types = frozenset({"动画类"})` | 加「图片类」 |
| `integrations/production_delivery.py:13-20` | 交付字段已模态无关（"结果" 走附件） | **不用改**，直接复用 |
| `builder.py:28-94` | 11 个节点全通用 | **不用改** |

---

## 已确认的产品决策（来自 2026-08-12 对话）

1. **Entry 粒度：一个概念 = 一个 entry。** 需求文档里「编号 1」「编号 2」各是一个 entry。同一编号的两个尺寸（1080×2080 / 1700×2500）**不拆 entry**，由后处理 resize 产出。
2. **Provider 全接：** seedream + banana + gpt-image2 三个都要能选。
3. **视频节点不复用给图片：** 指的是 provider / prompt 契约 / validator 这些策略层，**graph 编排节点仍共用**（硬拆并行 pipeline 会翻倍维护成本）。

---

## 真实需求文档结构（已抓取，作为测试 fixture 来源）

来源：`https://redcqchina.feishu.cn/wiki/BOlPwJ3I7iBpxLkHbKcc1fY6nmh`
标题：`【剧】女儿穿越救母_day6_CG图需求_202608.03`，revision=85，164 blocks，18 张图。

结构要点：
- 头部元信息：需求人、制作交付人、需求时间、交付时间
- **尺寸需求**：`尺寸：1700*2500`、`安全区：1080*2080`
- **核心要求**：`角色使用mm女主设定图`、`强调反转`
- **登场角色 + 风格参考**：image-1 ~ image-6，含 `注：下表为服装参考，仅能参考服装，不能参考画风。` 和 `注：Sarah的面部及身形形象参考，角色使用mm女主设定图。关键词：爽文爽剧`
- **具体插图（内嵌电子表格 block_type=30）**：列为 `编号 / 概念 / 对应场景 / 尺寸 / 类型 / 对应剧情贴图 / 内容描述 / 完成图 / 1080x2080 / 1700x2500`
  - 编号 1：`画面描述：Victor中景，脸部因为愤怒而变得扭曲` + `戏剧化顶光 + 侧逆光`，类型 CG
  - 编号 2：`画面描述：Sophia与Sarah握手言和，两位女性都散发着自信的光芒。` + `光线：明媚的光线`，类型 CG

**注意：** 用 `FeishuDocumentSource` 抓这份文档时会报 `阻塞：内嵌电子表格读取失败（Block doxcnNRgqAW9OFWK8f65RbWONHf）：飞书电子表格读取服务未配置`。原因是 probe 时没注入 `sheet_exporter`；生产 pipeline 走 `bootstrap.open_application_services` 时有注入。**Task 1 必须先验证这一点**，否则整条链路拿不到每张插图的完整字段。

---

## Task 0（前置验证）：确认内嵌表格能读

**Files:** 无改动，只验证

- [ ] 确认 `bootstrap.py` 里 `FeishuSheetExporter`（或同名类）确实注入给了 `FeishuDocumentSource`
- [ ] 跑一次真实 ingest（写临时脚本，注入完整 services），确认 `具体插图` 表格的 10 列能进 `NormalizedDocument.text_view`
- [ ] 若读不到：先修 sheet_exporter 注入，这是阶段 2 的**硬前置**
- [ ] 把成功抓到的 `NormalizedDocument` 存成 `tests/fixtures/cg_requirement_day6.json` 供后续 Task 用
- [ ] 清理临时脚本，commit fixture

---

## Task 1: GenerationTask 加图片专属字段

**Files:**
- Modify: `src/feishu_generation_agent/domain/plan.py`
- Test: `tests/unit/test_domain.py`（追加）或新建 `tests/unit/test_plan_image_mode.py`

要加的字段（写测试时以此为准）：
- `image_provider: Literal["seedream", "banana", "gpt-image2"] | None` — 图片模式必填，视频模式禁止
- `size_variants: list[str]` — 例 `["1080x2080", "1700x2500"]`，图片模式至少 1 个
- `safe_area: str | None` — 例 `"1080x2080"`，可选

校验规则：
- `IMAGE_TO_IMAGE` 时 `image_provider` 必填、`size_variants` 非空、`duration`/`resolution`/`generate_audio` 必须为 None（现有逻辑已覆盖后者）
- `IMAGE_TO_VIDEO` 时 `image_provider`/`size_variants`/`safe_area` 必须为空
- `output_count` 在图片模式下允许 > 1

- [ ] Step 1-5：按阶段 1 的 TDD 五步走（写失败测试 → 确认失败 → 实现 → 确认通过 → commit）

---

## Task 2: 图片 prompt 契约 validator

**Files:**
- Modify: `src/feishu_generation_agent/domain/reference_contract.py`
- Test: `tests/unit/test_reference_contract.py`（追加）

新增 `validate_image_prompt(task, assets)`。契约要求（源自真实需求文档的导演须知）：
- prompt 必须引用参考图（`@图片N` token），至少 1 个
- prompt 必须包含画面描述（人物 + 动作/表情）
- prompt 必须包含光影描述（文档里每张图都有「戏剧化顶光 + 侧逆光」这类）
- prompt 禁止出现视频语汇（运镜、时长、镜头运动、声音）——出现即报错，这是「视频契约误用到图片」的护栏
- `negative_constraints` 建议含「禁止勾勒边缘线」「禁止拉伸图片」（文档明确要求），缺失时给 warning 不阻塞

`validate_seedance_prompt` **一行都不改**。

- [ ] Step 1-5：TDD 五步

---

## Task 3: Planner 契约按 mode 分叉

**Files:**
- Modify: `src/feishu_generation_agent/integrations/planner.py`
- Test: `tests/unit/test_planner_prompts.py`、`tests/unit/test_planner.py`（追加）

改动要点：
- 把 `_SEEDANCE_PLANNING_CONTRACT`（`planner.py:96-107`）重命名为 `_VIDEO_PLANNING_CONTRACT`，内容不变
- 新增 `_IMAGE_PLANNING_CONTRACT`，内容覆盖：一个概念一个 entry、`size_variants` 从「尺寸需求」段落提取、`image_provider` 选择依据（写实→gpt-image2、卡通/厚涂→banana、中式/国风→seedream）、角色参考图挂载规则、禁止视频语汇
- `DeepSeekPlanner.plan()`（`planner.py:823-870`）加 `mode: Literal["video", "image"] = "video"` 参数，按 mode 选契约
- `enforce_seedance_prompt_contract`（`planner.py:773-802`）保持只作用于 video；新增 `enforce_image_prompt_contract`
- `RequirementPlanner` protocol（`ports.py:31-49`）的 `plan` 签名同步加 `mode`

**mode 从哪来：** 生产表「需求类型」字段值为「图片类」→ mode="image"。legacy run 默认 video。

- [ ] Step 1-5：TDD 五步

---

## Task 4: Provider registry

**Files:**
- Modify: `src/feishu_generation_agent/graph/nodes.py`
- Modify: `src/feishu_generation_agent/bootstrap.py`
- Modify: `src/feishu_generation_agent/config.py`（新增 seedream / gpt-image2 的 key/base_url/model 配置）
- Test: `tests/unit/test_provider_registry.py`（新建）

改动要点：
- `GraphServices.image_generator: ImageGenerator` → `image_providers: Mapping[str, ImageGenerator]`
- `_generator_for_task`（`nodes.py:883`）图片分支改成：`return task.image_provider, services.image_providers[task.image_provider]`
- provider 缺配置时给出明确中文错误，不要 KeyError
- `ChiyunImageGenerator` 已覆盖 `gpt-image*` / `dall-e*`（OpenAI 风格）和 Gemini 风格（`chiyun.py:43-47`）。banana2 走 Gemini 风格、gpt-image2 走 OpenAI 风格，**两者都能用现有 ChiyunImageGenerator**，只需不同 model 参数各实例化一次
- seedream 走火山 `ark_seedream` 风格（`nano-banana/providers.json` 的 volcengine 段），**需要新建** `integrations/ark_seedream.py`，参考 `nano-banana/app.py` 的火山图片请求实现

配置字段建议：
```
seedream_model: str = "doubao-seedream-5-0-pro-260628"   # 复用已有 ark_api_key / ark_base_url
banana_model: str = "banana2-ssvip"                       # 复用 chiyun_api_key / chiyun_base_url
gpt_image_model: str = "gpt-image-2"                      # 复用 chiyun_api_key / chiyun_base_url
```

- [ ] Step 1-5：TDD 五步（每个 provider 一个 Task 更好，registry 一个 Task）

---

## Task 5: 放开图片多输出

**Files:**
- Modify: `src/feishu_generation_agent/graph/nodes.py:785-796`
- Test: `tests/unit/test_production_tasks.py` 或新建

把 `if task.task_type is not TaskType.IMAGE_TO_VIDEO or task.output_count == 1: return [task]`
改成按 `output_count` 判断，不再看 task_type。

- [ ] Step 1-5：TDD 五步

---

## Task 6: 尺寸变体后处理

**Files:**
- Create: `src/feishu_generation_agent/integrations/image_resize.py`
- Modify: `src/feishu_generation_agent/graph/nodes.py`（verify_and_download_artifacts 之后）
- Test: `tests/unit/test_image_resize.py`（新建）

依据决策「一个概念 = 一个 entry，尺寸由后处理生成」：出图后按 `task.size_variants` 逐个 resize，每个变体作为独立 Artifact 交付。

- Pillow 已在依赖里（`pyproject.toml` 有 `pillow>=11,<13`）
- resize 策略参考 `nano-banana/providers.json` 的 `resize_method` / `resize_interpolation` 默认值
- 原图保留，变体额外产出（交付时一并上传）

- [ ] Step 1-5：TDD 五步

---

## Task 7: Bitable「图片类」白名单

**Files:**
- Modify: `src/feishu_generation_agent/integrations/production_bitable.py:39-45`
- Modify: `src/feishu_generation_agent/bitable/production_service.py:57`
- Test: `tests/unit/test_production_bitable.py`、`tests/unit/test_bitable_mvp_service.py`（追加）

`deliverable` computed field 和 `enabled_task_types` 都加「图片类」。交付字段（`production_delivery.py:13-20`）**不用改**。

- [ ] Step 1-5：TDD 五步

---

## Task 8: 端到端验证

- [ ] 用 Task 0 存的 fixture 跑一次完整 graph（fake providers），确认能产出图片 plan
- [ ] 真实跑一次（真出图，会产生费用），确认三个 provider 至少各成功一次
- [ ] 确认视频 pipeline 回归无损：`uv run pytest -q` 全绿 + 跑一次真实视频 run

---

## 阶段 2 完成标准

- [ ] 「图片类」需求文档能产出图片 plan，每个概念一个 entry
- [ ] seedream / banana / gpt-image2 三个 provider 都能出图
- [ ] 每个 entry 按 `size_variants` 产出多尺寸变体
- [ ] 图片 prompt 走图片契约校验，视频 prompt 仍走 Seedance 契约
- [ ] 视频 pipeline 行为零变化
- [ ] 交付走现有「结果」附件字段，不新建表
