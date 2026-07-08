Seedance 多参考生成器（可迁移版）

适用环境：
- macOS
- 需要能运行 python3
- 不需要安装第三方 Python 包

启动：
1. 解压整个文件夹
2. 双击「双击启动 Seedance.command」
3. 首次运行会创建本地 .venv 环境
4. 浏览器会自动打开 http://127.0.0.1:端口

如果提示没有 python3：
- 启动器会尝试打开 macOS Command Line Tools 安装器
- 安装完成后重新双击启动器

使用：
- API Key 可在页面手动填写
- 上传首尾帧、参考图、参考视频、参考音频
- 设置 Seedance 参数、重复次数、并发数、输出目录
- 点击「开始生成」

保存：
- 输出视频默认保存在本文件夹 outputs/
- 存档文件保存在本文件夹 archives/
- 每个存档是一个 .seedance 文件，包含配置和素材

注意：
- 请不要用 file:// 打开 static/index.html
- 必须通过「双击启动 Seedance.command」打开本地 http 页面
- .seedance 存档可能包含 API Key 和素材，分享前请确认是否需要清除 key

迁移/分享：
- 直接发送整个文件夹，或发送压缩包「Seedance多参考生成器_可迁移版.zip」
- 对方解压后双击启动即可
