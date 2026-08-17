# 飞书 Super Agent — 架构设计（01）

**日期**：2026-08-13
**范围**：图片生成 + 视频生成，通过飞书机器人对话开放给用户
**承载应用**：复用现有飞书自建应用（feishu-output-sync / feishu-generation-agent 用的那个），加开机器人能力

---

## 1. 目标

用户在飞书里跟机器人对话完成一次生成：

```
用户说清要做什么 → 给提示词 → 上传参考图（可能多张）
   → Agent 分析需求、写出 plan → 发给用户检阅
   → 用户确认 → Agent 执行生成 → 产出回传
```

全程按 `open_id` 追踪同一用户的会话与产出隔离。

---

## 2. 现状盘点

### 2.1 已经有的（可直接复用）

| 能力 | 位置 | 复用方式 |
|---|---|---|
| LangGraph 12 节点流水线 | `graph/builder.py`、`graph/nodes.py` | 直接复用，入口换源 |
| **人工审批中断** | `nodes.py:873` `human_approval` → `interrupt()` | 直接复用，换渲染层 |
| 计划生成 + 自审 + 确定性校验 | `integrations/planner.py`（DeepSeek） | 零改动 |
| 图片理解 | `integrations/vision.py`（Claude Vision，sha256 缓存） | 零改动 |
| 图片/视频供应商 | Chiyun / Seedance(Ark) / Seedream / Banana | 零改动 |
| 幂等提交与崩溃恢复 | `operations` 表 + 租约 + `compare_and_set_operation` | 零改动 |
| 断电续跑 | SQLite checkpointer + `resume_pending_runs()` | 零改动 |
| 行级用户隔离 | `Repository.owner_scope()` / `owner_user_id` | **open_id 直接灌进去** |
| 每人一张多维表格交付 | `feishu-output-sync/`（独立 launchd 服务） | 产出兜底交付 |
| **飞书频道 SDK** | `lark-channel-sdk 1.2.0`，已在 `pyproject.toml` | **当前是死依赖，本次启用** |

### 2.2 缺失的（本次要建）

1. **飞书 IM 入口**：全仓库零 `im.message.receive_v1` / 事件订阅 / webhook。`FeishuClient` 只有 Bitable/Docx/Drive/Wiki 端点，**没有任何 `im/v1/*`**，连发消息都做不到。
2. **多轮会话收集层**：现有两个入口（多维表格扫描、本地贴链接）都是「一次性拿到完整需求」，没有「多轮澄清 + 攒参考图」的概念。
3. **对话态需求源**：现有 `ingest_source` 只会解析 docx/wiki 链接，聊天里没有文档可解析。
4. **卡片审批渲染**：审批 payload 现在只喂给本地网页（`web/static/app.js`），没有飞书卡片。

### 2.3 已埋好但没接的伏笔（重要）

代码里已经为机器人入口预留了位置，本次正好填上：

- `storage/bitable_tasks.py:39,48` — `bot_ingress` / `card_actions` 两张表 + `accept_ingress()` / `finish_ingress()` / `accept_action()` / `finish_action()` 幂等助手，**全仓库无调用方**。
- `graph/state.py:16,18` — `requester_open_id` / `reply_context` 已贯穿 `AgentState` → `RequirementRequest` → 持久化列，但所有构造点都传空 `{}`。
- `README.md:209` 明确写了「后续可增加飞书机器人入口，只换入口和回复通道，不改领域模型和供应商端口」。

**结论：这是补一层入口，不是重写系统。**

---

## 3. 架构

```
       飞书客户端（私聊 / 群 @机器人）
                 │  WebSocket 长连接（无需公网回调）
                 ▼
  ┌──────────────────────────────────────────┐
  │ A. 频道层  lark_channel.FeishuChannel    │
  │    on("message") / on(card action)       │
  │    媒体自动落盘（MediaCacheConfig）      │
  └──────────────┬───────────────────────────┘
                 ▼
  ┌──────────────────────────────────────────┐
  │ B. 会话编排层（新，不进 LangGraph）      │
  │    ChatSession 状态机 + SQLite           │
  │    Claude Opus 4.7 做需求澄清            │
  │    攒齐：意图 / 提示词 / 参考图[]        │
  └──────────────┬───────────────────────────┘
                 │ 齐了 → RequirementRequest
                 ▼
  ┌──────────────────────────────────────────┐
  │ C. 现有 LangGraph 流水线（零改动）       │
  │    ChatRequirementSource 实现            │
  │      DocumentSource 端口                 │
  │    → analyze_images → plan → audit       │
  │    → validate → human_approval(interrupt)│
  └──────────────┬───────────────────────────┘
                 ▼
  ┌──────────────────────────────────────────┐
  │ D. 审批渲染层（新）                      │
  │    plan → 飞书交互卡片 → 用户点按钮      │
  │    → resume_run(Command(resume=...))     │
  └──────────────┬───────────────────────────┘
                 ▼
       execute → verify → 产出回传聊天 + 多维表格
```

---

## 4. 关键决策与取舍

### D1 — 在现有 `feishu-generation-agent` 内加模块，不新建服务

**取舍**：新建独立服务能解耦，但要复制供应商集成、幂等提交、崩溃恢复三套硬骨头，且两份代码会漂移。现有系统的图、checkpoint、审批中断、交付全部与入口无关，加一层入口的成本远低于复制。

### D2 — 会话收集层**不**进 LangGraph

**理由**：LangGraph 适合「节点确定、边确定」的流程。多轮澄清轮次不定、可能随时补图、可能中途改主意，塞进图里会让 checkpoint 状态爆炸。
**做法**：独立 `SessionOrchestrator` + 自己的 SQLite 表。**信息攒齐了才 `start_run()` 进图**，图仍然只跑「一次性拿到完整需求」的确定性流程 —— 与现有两个入口的契约完全一致。

### D3 — 用 `ChatRequirementSource` 实现现有 `DocumentSource` 端口 ★

这是整个方案最省事的接法。`ports.py` 里 `DocumentSource` 已经是抽象端口，只要新实现一个：

- `ingest()` → 把会话产物伪装成 `NormalizedDocument`：意图+提示词拼成 `text_view`，上传的参考图变成 `media_assets`（沿用 `[image:image-N]` 引用约定，planner 的 `@图片N` 契约不用动）
- `get_revision()` → 返回会话版本号（收集轮次哈希）。会话在提交后**冻结**，所以执行前的 `check_source_revision` 依然有意义：用户在审批期间又发了新图 → revision 变 → 自动回到重新规划。这个语义比文档源更贴切。

**下游 `analyze_images` / `plan_requirements` / `audit_plan` / `validate_plan` / `human_approval` / `execute` / `verify` 全部零改动。**

### D4 — 审批走飞书交互卡片，复用 `human_approval` 中断

现有 `interrupt(_approval_payload(state))` 已经把 `draft_plan` / `audit_report` / `validation_issues` 都吐出来了，只是现在只喂给本地网页。
新增：卡片渲染器 + 卡片回调 → 幂等（用已有 `card_actions` 表）→ `runtime.resume_run(Command(resume=decision))`。

三个动作直接映射现有 `_parse_approval` 的严格契约：

| 卡片按钮 | decision | 图的行为 |
|---|---|---|
| ✅ 确认生成 | `approve` + `selected_task_ids` | → 执行（已有路径） |
| ✏️ 要改 | `reject` + `feedback` | → 回到 `plan_requirements` 重规划（**已有循环**） |
| ✖️ 取消 | `cancel` | → END |

「要改」这条不用新写逻辑 —— 图里本来就有重规划回边。

### D5 — WebSocket 长连接接事件，不开公网回调

**契合当前部署**：服务机是本机 Mac、局域网 HTTPS、无公网 IP，`README` 也写了「不依赖公网回调」。`FeishuChannel.connect()` 是出站连接，与 `feishu-output-sync` 的出站 HTTPS 同一思路，不需要内网穿透、不需要改防火墙。

**代价**：进程必须常驻。挂 launchd，且**独立于 `com.ai-portal`**（照抄 `com.feishu-output-sync` 的隔离做法），避免 Portal 重启把会话打断。

### D6 — 身份：`open_id` → `owner_user_id`

现有 `Repository.owner_scope()` + `_owned_where()` 的行级隔离直接可用，`ensure_owned_run()` 已经在每个路由上把关。飞书 `open_id` 灌进去即可，**不需要新建权限体系**。

### D7 — 模型分工（含取舍说明）

| 用途 | 模型 | 说明 |
|---|---|---|
| 会话澄清 / 需求理解 | **`claude-opus-4-7`** | 你指定的 4.7，走现有 `ai.t8star.org` 中转，已实测可用 |
| 图片理解（vision） | `claude-opus-4-7` | 建议一并升级；缓存 key 含 model，旧缓存自动失效 |
| 结构化规划（planner） | **保持 DeepSeek** | 见下 |

**为什么 planner 不换**：现有 `DeepSeekPlanner` 不是简单的一次调用 —— 它有强制 JSON 模式、内联 JSON Schema、约 400 行确定性业务校验（`validate_plan`）、3 次「解析→校验→修复」重试循环，以及 thinking 开/关两套 bind（规划高推理、审计关推理）。这套东西是围着 DeepSeek 调出来的，换模型要重跑全部 plan 相关测试。
**取舍**：你说的「model 搭载 claude4.7」我理解为 super agent 的对话/理解层。planner 换不换是独立决策，建议**先不换**，跑通后单独 A/B。

**实测记录**：`claude-opus-4-7` 官方 API 拒绝 `temperature`/`top_p`/`top_k`（400），但现有 `bootstrap.py:350-360` 构造 `ChatAnthropic` 时传了 `temperature=0`。已对中转实测：带 `temperature` 和不带都返回 200，**中转容忍**。
→ 现在不会炸，但这是中转的宽松行为。日后若迁回官方 API 或换供应商会立刻 400。**新写的会话层不要传采样参数。**

### D8 — 产出交付双通道

1. **回聊天**：生成完直接把图/视频发回对话（用户要的即时反馈）
2. **落多维表格**：复用 `feishu-output-sync` 的「每人一张表」做归档兜底（聊天记录会被刷走，表格是长期资产）

---

## 5. 会话状态机

```
IDLE ──收到消息──▶ COLLECTING ──信息齐备──▶ PLANNING ──plan 出来──▶ AWAITING_APPROVAL
                      ▲                                                    │
                      │                                    ┌───────────────┼───────────────┐
                      │                                 approve          reject         cancel
                      │                                    │               │               │
                      └────────────────────────────────────┘               │               ▼
                                  （重新收集/重规划）                       │            CLOSED
                                                                            ▼
                                                                        EXECUTING ──▶ DELIVERED
```

**COLLECTING 的判定**：由 Opus 4.7 结构化输出 `{ready: bool, missing: [...], intent, prompt, notes}` 决定。不齐就追问缺的那一项，齐了才进图。

**边界情况**：
- 审批期间用户又发图 → session revision 变 → `check_source_revision` 自动作废审批、回到重规划（已有机制）
- 同一用户并发多会话 → 以 `(open_id, chat_id)` 为会话键；同一会话同时只允许一个 active run
- 群聊 → 只响应 @机器人；私聊 → 全部响应

---

## 6. 需要你在飞书开放平台做的事（checklist）

复用现有自建应用，增量开通：

- [ ] 「应用能力」→ 添加**机器人**
- [ ] 「事件订阅」→ 订阅方式选**长连接**（不要填回调 URL）
- [ ] 订阅事件：`im.message.receive_v1`（接收消息）
- [ ] 订阅事件：卡片回传交互（`card.action.trigger`）
- [ ] 权限：`im:message`、`im:message:send_as_bot`、`im:resource`（下载用户发的图片/文件）
- [ ] 权限：`im:chat:readonly`（群信息，仅群聊需要）
- [ ] **重新发布应用版本**，并让管理员审批
- [ ] 把机器人拉进测试群 / 直接私聊测试

> 现有的 `bitable:app`、云文档、drive 权限保持不变，产出搬运不受影响。

---

## 7. 风险

| 风险 | 缓解 |
|---|---|
| 长连接断线 / launchd 存活 | `KeepaliveConfig` + 独立 plist（不挂 `com.ai-portal`）+ 重连日志 |
| 飞书图片 `image_key` 有时效 | 收到消息**立刻**下载落盘（`MediaCacheConfig` 已实现），入 `FileStore` 按 sha256 寻址 |
| 用户在等待期间反复发消息 | 已有 `bot_ingress` 去重表；会话层做消息队列，不并发触发 run |
| 视频生成耗时长（分钟级） | 卡片状态原地更新（`update_card`），不刷屏；超时兜底提示 |
| 生成费用失控 | 沿用「未批准不提交」铁律；`max_output_count` 上限保持生效 |
| 中转容忍采样参数掩盖了兼容问题 | 新代码不传采样参数；在配置探针里加一条 opus-4-7 断言 |

---

## 8. 分期

| 期 | 内容 | 可验证结果 |
|---|---|---|
| **P1** | 频道层打通：长连接收消息、下载图片、回消息 | 私聊发「你好」+ 一张图，机器人回显收到 N 张图 |
| **P2** | 会话收集层 + Opus 4.7 澄清 | 多轮对话后输出结构化需求，标记 ready |
| **P3** | `ChatRequirementSource` 接图 + 卡片审批 | 收到 plan 卡片，点「确认」进入执行 |
| **P4** | 产出回传 + 多维表格归档 | 图/视频发回聊天 |
| **P5** | 群聊策略、并发、超时、错误兜底 | 压力/异常场景过 |

每期一份独立 plan 文档，不合并。

---

## 9. 待确认

1. planner 是否也要换 Claude 4.7（当前建议：不换，先跑通）
2. 群聊是否开放（当前假设：先只开私聊，P5 再加群）
3. 视频生成的等待体验（卡片轮询更新 vs 完成后单独推送）
