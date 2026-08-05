# BytePlus Seedance 连通性测试脚本设计

## 目标

提供一个独立、无第三方依赖的命令行脚本，用于从 Portal 服务机验证 BytePlus Seedance 区域入口是否可以直连。脚本完成任务提交、状态轮询和结果下载，并允许调用者直接更换提示词、模型 endpoint、区域入口及可选参考图。

本脚本只用于人工连通性与生成测试，不接入 Portal 页面，不修改现有 Seedance 生产逻辑，也不负责管理 API Key。

## 文件与运行环境

- 脚本位置：`tools/test_byteplus_seedance.py`
- 运行时：Python 3.9 及以上
- 依赖：仅 Python 标准库
- 密钥来源：环境变量 `ARK_API_KEY`
- 默认 API 根地址：`https://ark.ap-southeast.bytepluses.com/api/v3`
- 默认模型 endpoint：`ep-20260805102121-kqzt7`

脚本不得把 API Key 写入文件、请求摘要、错误输出或下载文件名。

## 命令行接口

脚本接受以下参数：

- `--prompt TEXT`：直接提供提示词。
- `--prompt-file PATH`：从 UTF-8 文本文件读取提示词。
- `--image SOURCE`：可选参考图；支持本地文件、`http://`/`https://` URL 和 `asset://` 素材地址。不提供时执行文生视频。
- `--image-role ROLE`：参考图角色，可选 `reference_image`、`first_frame`、`last_frame`，默认 `reference_image`；仅在提供 `--image` 时有效。
- `--base-url URL`：覆盖默认区域 API 根地址，便于切换 EU 入口。
- `--model ENDPOINT`：覆盖默认模型 endpoint。
- `--ratio RATIO`：画面比例，默认 `16:9`。
- `--duration SECONDS`：视频时长，默认 `5` 秒。
- `--output PATH`：下载目标，默认在当前目录生成带时间戳的 MP4 文件名。
- `--poll-interval SECONDS`：轮询间隔，默认 `5` 秒。
- `--timeout SECONDS`：总等待上限，默认 `900` 秒。
- `--dry-run`：显示不含密钥的请求地址与 JSON 请求体，不提交任务。

`--prompt` 与 `--prompt-file` 互斥，并且必须提供其中一个。空提示词、非正数时长、非正数轮询间隔和非正数超时时间在发起网络请求前拒绝。提供本地参考图时，脚本还会在发起请求前校验文件存在、可读取且扩展名属于常见图片格式。

## 数据流

1. 解析并校验命令行参数。
2. 从环境变量读取 `ARK_API_KEY`；`--dry-run` 不要求密钥。
3. 解析可选参考图：
   - 本地图片根据扩展名确定 MIME 类型，读取后编码为 `data:<mime>;base64,...`。
   - `http://`、`https://` 和 `asset://` 地址保持原样。
4. 构造任务请求：
   - `POST {base_url}/contents/generations/tasks`
   - `Authorization: Bearer <ARK_API_KEY>`
   - `Content-Type: application/json`
   - 请求体首先包含文本内容；如果提供参考图，则追加一个 `image_url` 内容项，其 URL 为解析后的图片来源、角色为 `--image-role`。
   - 请求体还包含模型 endpoint、音频开关、比例、时长和水印设置。
5. 从响应中提取任务 ID，并立即显示任务 ID。
6. 通过 `GET {base_url}/contents/generations/tasks/{task_id}` 定时查询任务。
7. 任务成功后从响应中提取视频 URL，以流式方式下载到临时文件，再原子替换为 `--output` 目标文件。
8. 打印最终文件绝对路径与文件大小。

## 默认请求行为

- `generate_audio`：`true`
- `watermark`：`false`
- `ratio`：`16:9`
- `duration`：`5`

脚本负责文生视频和单张参考图的图生视频。创建、上传和管理 Asset 素材不在本次范围内；调用者可以把已有的 `asset://` 地址作为 `--image` 传入。参考视频、参考音频及多张参考图也不在本次范围内。

## 错误处理

- HTTP 非成功响应：输出状态码以及 BytePlus 返回的错误码、消息和请求 ID（如存在）。
- 响应不是合法 JSON或缺少任务 ID：输出不含鉴权头的响应摘要并退出。
- 任务失败或过期：输出任务状态及服务端错误信息并返回非零退出码。
- 超时：保留任务 ID，提示调用者可继续在控制台查询，不下载文件。
- 下载失败：删除未完成的临时文件，不覆盖已有的完整输出。
- 用户中断：停止轮询并返回退出码 130。

所有日志都不得输出完整请求头或 API Key。

## 验证

实现阶段采用以下不产生生成费用的验证：

- 参数解析和请求体构造单元测试。
- 本地图片 data URL 编码、远程 URL 透传和三种图片角色的单元测试。
- 使用本地伪 HTTP 服务验证提交、轮询、成功下载、失败响应和超时行为。
- `--dry-run` 验证提示词、模型与区域入口覆盖结果。
- Python 3.9 兼容性语法检查。

真实生成由用户在确认服务机未开启 VPN/系统代理后手动运行，以此判断目标区域入口的实际直连能力。
