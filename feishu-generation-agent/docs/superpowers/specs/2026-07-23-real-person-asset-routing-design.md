# 真人类官方资产路由设计

## 目标

让生产多维表格中 `需求类型=真人类` 的任务可领取、审批、重跑和交付。真人类图生视频使用火山官方 Asset API 上传需求图片并以 `asset://` 引用调用 Ark Seedance；真人类图生图继续使用 Chiyun。动画类现有图生图与 Seedance 流程不变。

## 路由

生产表服务允许 `动画类` 和 `真人类` 任务领取、批准与重跑；其他类型保持“暂未启用”。运行绑定中的 `snapshot.task_type` 是后续生成路由的唯一来源。

图生图任务始终提交给 Chiyun。图生视频任务在动画类时提交给现有 `SeedanceVideoGenerator`，在真人类时提交给新增的 `VolcenginePortraitVideoGenerator`。

## 真人类图生视频

批准后，适配器为每个运行创建一个独立的 AIGC Asset 分组，名称含运行 ID。它只上传该视频任务所引用的图片：将本地图片发布为短期公开 URL，以 AK/SK 签名调用 `CreateAsset`，轮询 `GetAsset` 到 `Active`，然后以 `asset://<id>` 作为 `reference_image` 调用 Ark `/contents/generations/tasks`。

适配器沿用现有 Seedance 的时长、比例、分辨率和最长 15 分钟轮询语义。创建的分组与 Asset ID 存入提交记录，默认保留，供失败排查与重跑追溯。音频、视频外部参考仍沿用当前公网 URL 透传逻辑；Asset API 只处理图片。

## 凭据与安全

新增仅后端读取的 `VOLCENGINE_ACCESS_KEY`、`VOLCENGINE_SECRET_KEY`、`VOLCENGINE_PROJECT_NAME` 配置。API Key 继续使用已有 `ARK_API_KEY`，Ark 地址继续使用 `ARK_BASE_URL`。实现参考本机 `volcengine-portrait` 的 SigV4 与资产调用，但不导入其应用模块，不调用其 HTTP 服务，也不记录任何凭据或签名材料。

实施时将本机已有 `volcengine-portrait/config.json` 的 AK/SK 安全写入 Agent 的本地 `.env`；该文件保持不纳入版本控制。

## 错误与交付

资产组创建、临时公开地址、资产激活和生成轮询任一步失败，均作为当前任务可读错误进入既有审批/运行轨迹；不会写入结果表。成功产物照现有生产交付器写入统一结果表，复制来源字段并附结果。

## 验收与测试

- 真人类任务在扫描页显示可处理，其他未知类型仍禁用。
- 图生图真人任务继续使用 Chiyun。
- 图生视频真人任务按“建组、创建并激活资产、提交 Ark、轮询”顺序执行，且请求中的图片为 `asset://`。
- 动画类视频仍使用原 `SeedanceVideoGenerator`。
- 单元测试使用模拟 HTTP，不发送付费生成请求、不读取或输出真实密钥。
