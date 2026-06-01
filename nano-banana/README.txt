Nano Banana 多图生成器（可迁移版）

启动：
1. 解压整个文件夹
2. 双击「Start Nano Banana.command」
3. 首次运行会创建本地 .venv
4. 浏览器会自动打开本地 http 页面

功能：
- 支持 text2img / img2img
- 支持最多 14 张参考图
- 支持提示词
- 支持模型、比例、尺寸、返回格式、seed、并发重复等参数
- 支持输出目录选择
- 支持多存档，每个存档是 archives/ 下的 .nanobanana 文件
- 支持图片预览和结果下载

注意：
- 不要用 file:// 打开 static/index.html
- 必须通过「Start Nano Banana.command」启动
- 存档文件可能包含 API Key 和素材，分享前请确认是否需要清除 key
- API 默认使用 https://ai.t8star.cn

接口：
- text2img: POST /v1/images/generations?async=true
- img2img: POST /v1/images/edits?async=true
- 查询任务: GET /v1/images/tasks/{task_id}
