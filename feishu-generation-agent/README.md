# 飞书生成任务 Agent

这是一个只绑定 `127.0.0.1` 的本地应用：它可扫描飞书多维表格中的任务，读取 `docx` 或 `wiki` 需求，下载并理解文档图片，用 LangGraph 拆解为图生图与图生视频任务，在网页中等待人工审批，然后调用 Chiyun 和火山方舟 Seedance，最后把产物作为附件写回原表格记录。保留的“新建飞书交付文档”模式仅用于兼容旧流程。

默认原则是“先看计划，再花钱”。未经网页批准，生成器不会提交任务；真实冒烟也有独立的双重门禁。

## 项目边界

- 当前只处理图生图和图生视频，不处理通用办公自动化。
- 当前入口是本地飞书链接和本地审批页，不依赖公网回调。
- 业务状态和 LangGraph checkpoint 都保存在本机 SQLite。
- 飞书读取、模型调用、生成和交付属于外部通信；开启 LangSmith 后，工作流输入输出还会发往 LangSmith。
- 结构保留了扩展边界，后续可增加飞书机器人入口或迁移到主机部署，而不改领域模型和供应商端口。

## 架构

```text
浏览器 127.0.0.1:8765
          │
       FastAPI
          │
   GraphRuntime ───── 业务 SQLite（run/event/operation/artifact）
          │
       LangGraph ───── Checkpoint SQLite
          │
   ┌──────┼───────────────┬──────────────┐
飞书读取  DeepSeek 规划   Claude 看图   人工审批页
                                      │
                         ┌────────────┴────────────┐
                       Chiyun 图生图          Seedance 图生视频
                         └────────────┬────────────┘
                                  飞书交付文档
```

LangGraph 节点依次为：

1. `ingest_source`：解析 docx/wiki 链接、读取文档块并下载图片。
2. `normalize_document`：形成稳定的文档、块和素材模型。
3. `analyze_images`：用 Claude Vision 只描述图片中可见内容。
4. `plan_requirements`：用 DeepSeek 生成结构化任务计划。
5. `audit_plan`：以独立审查提示检查遗漏、冲突和虚构内容。
6. `validate_plan`：执行本地确定性校验。
7. `human_approval`：通过 LangGraph `interrupt` 暂停，等待批准、退回或取消。
8. `revalidate_approval`：重新校验用户实际批准的任务。
9. `check_source_revision`：执行前确认飞书文档版本未变化；变化则重新规划。
10. `execute_selected_tasks`：按审批子集提交并轮询供应商任务。
11. `verify_and_download_artifacts`：校验数量、MIME、大小和 SHA-256。
12. `deliver_to_feishu`：创建交付文档、上传产物、写入任务结果并添加协作者。

## 安装

需要 Python 3.12 和 `uv`。在本目录执行：

```bash
uv sync --locked
cp .env.example .env
```

如果依赖下载较慢，可临时使用国内镜像：

```bash
UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple uv sync --locked
```

## 配置

`.env` 字段如下：

| 字段 | 含义 |
|---|---|
| `APP_HOST` | 固定为 `127.0.0.1`，防止意外暴露到局域网 |
| `APP_PORT` | 本地端口，默认 `8765` |
| `DATA_DIR` | 输入、供应商暂存和本地状态目录 |
| `OUTPUTS_DIR` | 生成产物目录 |
| `BUSINESS_DB_PATH` | 业务 SQLite 路径 |
| `CHECKPOINT_DB_PATH` | LangGraph checkpoint SQLite 路径 |
| `LARK_APP_ID` / `LARK_APP_SECRET` | 飞书自建应用凭证 |
| `LARK_BITABLE_URL` / `LARK_BITABLE_TABLE_ID` / `LARK_BITABLE_VIEW_ID` | 多维表格链接、数据表 ID 与视图 ID；本地 MVP 使用这三项扫描并回写 |
| `LARK_OUTPUT_OWNER_OPEN_ID` | 旧版交付文档协作者的 Open ID；多维表格模式不需要 |
| `LARK_OUTPUT_FOLDER_TOKEN` | 旧版新建交付文档所在的文件夹 token；多维表格模式不需要 |
| `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL` | 需求规划模型配置，默认模型名为 `deepseek-v4-pro` |
| `CLAUDE_API_KEY` / `CLAUDE_BASE_URL` / `CLAUDE_MODEL` | 图片理解模型配置 |
| `CHIYUN_API_KEY` / `CHIYUN_BASE_URL` / `CHIYUN_MODEL` | Chiyun 图生图配置 |
| `ARK_API_KEY` / `ARK_BASE_URL` / `SEEDANCE_MODEL` | 火山方舟 Seedance 配置 |
| `LANGSMITH_TRACING` / `LANGSMITH_API_KEY` / `LANGSMITH_PROJECT` | 可选追踪；默认关闭 |
| `LANGGRAPH_STRICT_MSGPACK` | 保持 `true`，禁止 checkpoint 回退到 pickle |
| `ALLOW_PAID_SMOKE` | 只有精确设置为 `YES` 才开放真实付费冒烟 |

应用未配齐时仍可打开首页和 `/api/health`，但创建 run 会返回 503。先运行配置探针可以看到缺少哪一类能力。

## 飞书网页配置

在飞书开放平台创建企业自建应用，启用机器人不是本地 MVP 的必要条件。应用至少需要这些能力：

- 获取 tenant access token。
- 读取新版文档元数据和文档块。
- 读取 wiki 节点并解析到 docx。
- 下载文档中的图片素材。
- 在指定文件夹创建新版文档。
- 上传文件；大文件交付会使用分片上传。
- 写入文档块。
- 为交付文档添加用户协作者。

多维表格 MVP 还需要应用拥有目标空间、数据表和视图的读取权限，以及“结果”附件字段的写入权限。把表格链接填入 `LARK_BITABLE_URL`，并填入 URL 中对应的 table/view ID；不要把带 token 的完整调试响应、事件载荷或附件 Base64 写入 `.env`、日志或 SQLite。

旧版交付文档模式才需要把测试需求文档和输出文件夹授权给应用。`LARK_OUTPUT_FOLDER_TOKEN` 是文件夹 URL 中的 token。`LARK_OUTPUT_OWNER_OPEN_ID` 可从飞书开放平台 API 调试台的用户查询结果、机器人事件测试载荷中的 `open_id`，或管理员提供的用户 Open ID 获取；它不是手机号、union_id 或 user_id。

权限变更后需要在飞书管理后台重新发布应用版本，并确认组织管理员已批准。

## 启动与使用

先做不产生生成费用的检查：

```bash
uv run agent-config-probe --no-network
uv run agent-config-probe
```

启动：

```bash
uv run feishu-generation-agent
```

打开 `http://127.0.0.1:8765`，粘贴飞书文档链接。页面会显示节点轨迹和任务卡片。可以修改提示词、负面约束、比例、尺寸、视频时长、分辨率、声音、生成数量和参考图顺序，也可以增添、替换或解除本地图片引用。

三个审批动作含义：

- “退回重新规划”把意见交给规划模型，再次生成计划，不调用图像或视频生成。
- “全部取消”将 run 置为 `cancelled`，不调用生成。
- “批准所选任务”只执行勾选且通过本地校验的任务。

交付失败时点击“仅重试交付”。该操作复用本地产物、飞书 document ID、已上传文件 token 和已完成块批次，不重新调用 Chiyun 或 Seedance。

“删除本地运行”只允许等待审批或已结束的 run；会删除业务记录、checkpoint、输入和产物目录，但不会删除已经创建的飞书交付文档。

## 多维表格本地 MVP

多维表格模式只使用既有的四列：`文本`、`需求来源`、`执行人`、`结果`。不会自动创建、修改或填充任何字段。

1. 在 `.env` 配置 `LARK_BITABLE_URL`、`LARK_BITABLE_TABLE_ID` 和 `LARK_BITABLE_VIEW_ID`，并确保应用能读取目标视图、上传附件及更新“结果”列。
2. 启动应用后，在首页点击“扫描多维表格任务”。列表只显示“需求来源”是有效飞书文档链接且“结果”为空的记录；“执行人”只展示，不作为筛选条件。
3. 手动点击一条记录的“开始分析”。本机 SQLite 会先原子领取记录；重复领取会提示冲突，不会启动第二个运行。
4. 等页面到达“等待审批”。检查计划、任务参数和参考图；在这里退回或取消不会调用 Chiyun 或 Seedance。批准所选任务才会触发生成。
5. 成功后系统在写回前重新读取该记录。若“结果”仍为空，则上传全部产物并一次性写入附件列；若外部已填入结果，系统显示冲突且绝不覆盖。
6. 若生成失败或取消，结果列保持为空并释放本地领取，可重新扫描。若只有写回失败，使用“仅重试交付”；它只复用已保存的产物和上传 token，不重新生成。

本地状态只保存任务绑定、审批指纹、运行状态、产物和幂等信息；不保存 API Key、飞书原始事件载荷、附件 Base64 或带凭证的链接。应用重启后，`waiting_approval` 仍停在审批门禁，已提交的供应商任务只轮询恢复，不会二次提交。

## 恢复语义

- `waiting_approval` 在重启后只恢复展示，不会自动批准。
- `created`、`running`、`resuming`、`waiting_provider` 会用原 thread ID 继续。
- 供应商提交前先保存不可变 submission intent；已保存官方任务 ID 时只轮询，不重复提交。
- `delivering` 重启后只继续交付重试，不回到生成阶段。
- `delivery_failed` 保留产物，等待显式重试。
- Chiyun/Seedance 的提交、产物修复、分片上传和文档块写入都有持久化幂等记录。

## 测试

```bash
uv run pytest -q
uv run python -m compileall -q src tests
```

先用真实表格做完全只读的检查；它只读取 wiki/表格定位、四个字段和目标视图，既不领取任务，也不调用模型或生成器：

```bash
uv run agent-config-probe --network
uv run agent-smoke --bitable-read-only
```

需要验证从表格记录到审批页的链路时，可指定一条专用测试记录：

```bash
uv run agent-smoke --bitable-record-id recxxxxxxxx
```

该命令会读取需求并运行已有分析/规划流程，但在 `waiting_approval` 停止，绝不提交 Chiyun 或 Seedance。请只对已获准的测试内容使用它。不要在终端、截图或工单中粘贴凭证、原始事件载荷或媒体 Base64。

真实付费冒烟会产生模型和生成费用，并创建飞书文档。必须同时满足两个门禁：

```bash
ALLOW_PAID_SMOKE=YES uv run agent-smoke \
  --confirm-paid-smoke https://tenant.feishu.cn/docx/专用测试文档token
```

脚本会先打印预计付费步骤，并在每个付费调用前再次要求在终端输入精确的 `YES`。它只生成一张图和一个 4 秒、480p、无声音视频；完成后重开本地服务依赖并验证 operation 数量未增加。普通测试和配置探针不会提交生成任务。

## LangSmith 与隐私

`LANGSMITH_TRACING=false` 时应用会显式关闭 LangChain/LangGraph 追踪。开启后必须配置 Key，审批页会显示外发警告。文档正文、视觉描述、提示词、运行结果和错误上下文可能进入 LangSmith 项目；包含敏感业务信息时应保持关闭。

## 常见问题

- 飞书 401：检查 App ID/Secret、应用版本是否发布，以及凭证是否属于当前租户。
- 飞书 403：多维表格模式检查空间/数据表/视图读取权限和“结果”附件列写入权限；旧版模式还需检查文档、wiki、素材下载、文件夹、创建文档、上传和协作者权限。修改权限后重新发布应用。
- 429：供应商或飞书限流。保留 run，等待后重启或按页面允许的动作重试。
- 文档图片失败：确认图片块对应用可见，素材下载权限已批准，文件没有被删除；审批页也可替换为本地图片。
- 模型 JSON 无效：规划器会进行有限次数的结构修复；持续失败时先用探针确认模型名，再查看已脱敏的节点事件。
- Ark 长轮询：不要重复批准。重启后系统会依据官方任务 ID 继续轮询。
- 飞书分片失败：点击“仅重试交付”；已完成的 part 不会重复上传。
- 多维表格字段检查失败：确认字段名称精确为“文本、需求来源、执行人、结果”，且“结果”为附件字段；MVP 不会自动修表。
- 扫描为空：确认“需求来源”是一个可读取的飞书 docx/wiki 链接、“结果”列为空，且记录没有被另一条本地运行领取。
- `/api/health` 显示未就绪：先执行 `agent-config-probe --no-network`，按 capability 补齐配置，再执行 `agent-config-probe --network`。

## 未来飞书机器人入口

后续可以实现 `FeishuBotSource`，把机器人消息或交互卡片转换成与本地链接入口相同的 `RequirementRequest`。卡片按钮携带 run ID，服务端仍通过原 thread ID 恢复 LangGraph interrupt。这样只替换入口和回复通道，不改变规划、审批、生成、幂等恢复和交付逻辑。
