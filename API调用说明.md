# 本地 API 调用说明

两个程序启动后都会在本机开放 HTTP API，OpenClaw 或其它本地脚本可以直接调用，不需要操作网页。

- Seedance 默认地址：`http://127.0.0.1:8787`
- Nano Banana 默认地址：`http://127.0.0.1:8797`

## 通用接口

### 查看当前配置

```bash
curl http://127.0.0.1:8787/api/config
curl http://127.0.0.1:8797/api/config
```

返回内容包含：

- `providers`：当前 `providers.json` 中可用供应商、模型、默认参数
- `default_provider`：默认供应商
- `config_error`：配置文件损坏时的错误详情
- `has_key` / `masked_key`：本地是否能读取到默认 key

### 查看请求模板

Agent 建议先读这个接口学习当前版本的标准调用格式。

```bash
curl http://127.0.0.1:8787/api/request-template
curl http://127.0.0.1:8797/api/request-template
```

返回内容包含：

- `templates.minimal`：最小可用 JSON 请求
- `templates.full`：完整字段 JSON 请求
- `field_notes`：关键字段说明

### 查看接口结构

```bash
curl http://127.0.0.1:8787/api/schema
curl http://127.0.0.1:8797/api/schema
```

返回内容包含：

- `providers`：可用供应商和默认 Base URL
- `value_fields`：可提交的参数字段
- `file_fields`：可提交的素材字段
- `media_item`：素材上传格式

### 提交 JSON 任务

```text
POST /api/jobs/json
Content-Type: application/json
```

成功后返回：

```json
{
  "ok": true,
  "job_id": "xxxx",
  "status_url": "/api/jobs/xxxx"
}
```

失败时会保留旧版 `error` 字符串，同时增加结构化错误字段：

```json
{
  "ok": false,
  "error": "API key is required",
  "error_code": "invalid_request",
  "error_info": {
    "code": "invalid_request",
    "message": "API key is required",
    "detail": "",
    "retryable": false
  }
}
```

### 查询任务

```bash
curl http://127.0.0.1:8787/api/jobs/你的job_id
curl http://127.0.0.1:8797/api/jobs/你的job_id
```

任务结果里的 `download_url` 可直接下载生成文件。

### 查看后台记录

```bash
curl http://127.0.0.1:8787/api/activity
curl http://127.0.0.1:8797/api/activity
```

返回内容包含总调用次数、页面运行次数、API 调用次数、成功、失败、运行中数量和最近记录列表。

查看单条详情：

```bash
curl http://127.0.0.1:8787/api/activity/记录ID
curl http://127.0.0.1:8797/api/activity/记录ID
```

详情里包含请求摘要、返回摘要、任务结果、错误信息。API Key 会脱敏，`data_url` 不保存完整 base64，只保存是否存在和字符长度。

从 v0.2.5 开始，后台记录详情会额外返回 `restore` 字段。网页里的“后台记录”详情页可以点击“恢复到当前页”，把当次提示词、参数和素材恢复到当前生成页继续使用。旧记录如果当时没有保存素材副本，会尽量恢复参数；如果旧记录里有 `saved_media` 引用，也会恢复这些素材。

### 本地维护接口

打开当前输出目录：

```bash
curl -X POST http://127.0.0.1:8787/api/open-output-dir \
  -F 'output_dir=/Users/你的用户名/Desktop/seedance_outputs'

curl -X POST http://127.0.0.1:8797/api/open-output-dir \
  -F 'output_dir=/Users/你的用户名/Desktop/nano_outputs'
```

`output_dir` 可以省略；省略时会打开程序默认输出目录。这个接口会在本机调用系统文件管理器：macOS 使用 `open`，Windows 使用 `os.startfile`，Linux 使用 `xdg-open`。

手动清理缓存：

```bash
curl -X POST http://127.0.0.1:8787/api/cleanup-cache
curl -X POST http://127.0.0.1:8797/api/cleanup-cache
```

清理策略：

- 删除 `state/media` 中超过 30 天、且没有被当前存档或后台记录引用的孤立素材。
- 删除 `logs` 中超过 14 天的日志文件。
- 不删除 `outputs` 里的生成结果。
- 不会自动定时运行，只在网页点击“清理缓存”或调用接口时执行。

## 供应商增量更新

两个 app 根目录都有 `providers.json`：

- `seedance/providers.json`
- `nano-banana/providers.json`

这个文件用于开发端维护供应商、模型、默认 Base URL 和默认参数。客户正常不需要修改；需要增量更新模型时，可以替换同名 `providers.json` 后重启 app。

支持通过 JSON 增量调整：

- 供应商显示名
- 默认 `base_url`
- 模型列表
- 默认模型和默认参数

不支持只靠 JSON 新增后端未实现过的全新 API 协议。`api_style` 必须是程序已经支持的接口类型。

## 素材上传格式

支持两种方式。

### Base64 data URL

```json
{
  "data_url": "data:image/png;base64,...",
  "filename": "ref.png"
}
```

### 远程 URL

```json
{
  "url": "https://example.com/ref.png",
  "filename": "ref.png"
}
```

目前不开放任意本地文件路径读取，避免本地 API 被误用读取电脑上的其它文件。

## Dry Run 测试

加上 `"dry_run": true` 后，只测试参数解析和素材上传解析，不会调用外部模型，也不会产生费用。

```json
{
  "dry_run": true,
  "prompt": "测试提示词"
}
```

## Seedance 示例

### 支持字段

常用参数：

- `api_key`
- `provider`：`t8star` 或 `volcengine`
- `base_url`
- `model`
- `custom_model`：高级字段，非空时覆盖 `model`
- `prompt`
- `duration`
- `resolution`
- `ratio`
- `seed`
- `generate_audio`
- `watermark`
- `return_last_frame`
- `web_search`
- `repeat_count`
- `concurrency`
- `poll_interval`
- `timeout`
- `vary_seed`
- `output_dir`

素材字段：

- `first_frame`
- `last_frame`
- `ref_image_1` 到 `ref_image_9`
- `ref_video_1` 到 `ref_video_3`
- `ref_audio_1` 到 `ref_audio_3`

### Seedance dry_run

```bash
curl -X POST http://127.0.0.1:8787/api/jobs/json \
  -H 'Content-Type: application/json' \
  --data '{
    "dry_run": true,
    "provider": "volcengine",
    "model": "doubao-seedance-2-0-260128",
    "prompt": "api seedance test",
    "duration": 8,
    "ratio": "16:9",
    "resolution": "720p",
    "media": {
      "ref_image_1": {
        "data_url": "data:image/png;base64,YWJj",
        "filename": "ref.png"
      }
    }
  }'
```

### Seedance 正式提交

```bash
curl -X POST http://127.0.0.1:8787/api/jobs/json \
  -H 'Content-Type: application/json' \
  --data '{
    "api_key": "你的API_KEY",
    "provider": "volcengine",
    "base_url": "https://ark.cn-beijing.volces.com/api/v3",
    "model": "doubao-seedance-2-0-260128",
    "prompt": "视频提示词",
    "duration": 8,
    "ratio": "16:9",
    "resolution": "720p",
    "generate_audio": true,
    "watermark": false,
    "repeat_count": 1,
    "concurrency": 1,
    "media": {
      "ref_image_1": {
        "url": "https://example.com/ref1.png",
        "filename": "ref1.png"
      },
      "ref_video_1": {
        "url": "https://example.com/ref.mp4",
        "filename": "ref.mp4"
      },
      "ref_audio_1": {
        "url": "https://example.com/ref.mp3",
        "filename": "ref.mp3"
      }
    }
  }'
```

豆包官方火山方舟模式注意：

- `provider` 使用 `volcengine`
- 默认 `base_url` 是 `https://ark.cn-beijing.volces.com/api/v3`
- 首尾帧模式不能和参考图、参考视频、参考音频混用
- 参考音频不能单独使用，至少要同时有参考图或参考视频

## Nano Banana 示例

### 支持字段

常用参数：

- `api_key`
- `provider`：`t8star` 或 `gemini`
- `base_url`
- `mode`：`img2img` 或 `text2img`
- `model`
- `custom_model`：高级字段，非空时覆盖 `model`
- `prompt`
- `aspect_ratio`
- `image_size`
- `response_format`
- `seed`
- `control_after_generate`
- `skip_error`
- `repeat_count`
- `concurrency`
- `poll_interval`
- `timeout`
- `vary_seed`
- `output_dir`
- `resize_enabled`
- `resize_width`
- `resize_height`
- `resize_interpolation`
- `resize_method`
- `resize_condition`
- `resize_multiple_of`

素材字段：

- `image_1` 到 `image_14`

### Nano Banana dry_run

```bash
curl -X POST http://127.0.0.1:8797/api/jobs/json \
  -H 'Content-Type: application/json' \
  --data '{
    "dry_run": true,
    "provider": "t8star",
    "model": "nano-banana-2",
    "mode": "img2img",
    "prompt": "api nano test",
    "media": {
      "image_1": {
        "data_url": "data:image/png;base64,YWJj",
        "filename": "image.png"
      }
    }
  }'
```

### Nano Banana 正式提交

```bash
curl -X POST http://127.0.0.1:8797/api/jobs/json \
  -H 'Content-Type: application/json' \
  --data '{
    "api_key": "你的API_KEY",
    "provider": "t8star",
    "base_url": "https://ai.t8star.cn",
    "model": "nano-banana-2",
    "mode": "img2img",
    "prompt": "图片提示词",
    "aspect_ratio": "auto",
    "image_size": "2K",
    "response_format": "url",
    "repeat_count": 1,
    "concurrency": 1,
    "media": {
      "image_1": {
        "url": "https://example.com/image1.png",
        "filename": "image1.png"
      }
    }
  }'
```

Chiyun 模式：

```json
{
  "provider": "gemini",
  "base_url": "https://chiyun.work",
  "model": "banana2-ssvip"
}
```

可用模型以 `/api/config` 或 `/api/schema` 返回为准。

## OpenClaw 接入建议

1. 先调用 `/api/schema` 获取字段列表。
2. 调用 `/api/request-template` 获取 `minimal/full` 请求模板。
3. 把工作流里的参数映射到 `value_fields`。
4. 把图片、视频、音频转成 `media` 对象。
5. 先用 `dry_run: true` 测试解析。
6. 正式提交后轮询 `status_url`。
7. 从任务结果里的 `download_url` 下载文件。

最小流程：

```text
GET  /api/schema
GET  /api/config
GET  /api/request-template
POST /api/jobs/json
GET  /api/jobs/{job_id}
GET  /api/download/{token}
GET  /api/activity
```
