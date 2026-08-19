# feishu-output-sync

把服务机本地生成的图片/视频,增量搬运到**每人一个飞书多维表格**,让远程用户在飞书里直接看/下载自己的产出 —— 解决"我这能看到、用户那边找不到"。

## 它做什么 / 不做什么

- **只读**扫描各子应用的 `outputs/<用户名>/<日期>/` 目录,不删/不改/不移动任何源文件。
- 每个用户**自动创建**一个多维表格(名为「XX的AI产出」),内含 4 张附表:Seedance / Nano Banana / Dreamina / 人像生成。
- 图片和视频都作为**实体附件**上传(大视频走分片上传)。
- 增量:已搬过的文件记录在本地 SQLite,重跑/重启不重复上传。
- **完全独立**:不碰 portal、不碰 4 个子应用、不碰 Codex 的 `feishu-generation-agent/` 和 `.worktrees/feishu-bitable-bot/`。飞书走出站 HTTPS,不需要公网 IP / 内网穿透。

## 一次性:飞书后台配置

1. 打开 [飞书开放平台](https://open.feishu.cn/) → 创建**企业自建应用**。
2. 记下 **App ID** 和 **App Secret**。
3. **开通权限(scope)** 并发布应用版本:
   - `bitable:app`(查看、编辑、管理多维表格 —— 建 App/表/记录都靠它)
   - 云文档 / 云空间相关:能上传素材(medias)、能给用户加协作者(drive permissions)
4. 在飞书云空间建一个**文件夹**放这些表格,拿它的 `folder_token`(打开文件夹看 URL 里的 token)。确保应用对该文件夹有编辑权限。
5. 收集需要接收产出的用户的飞书 **open_id**(可在开放平台「通讯录」或管理后台查到),填进 `config.json` 的 `user_open_ids`,键是子应用登录用户名(见 `outputs/<用户名>/`)。

> ⚠️ **关键**:用应用身份(tenant_access_token)创建的表格,归属"应用"这个虚拟身份,**普通用户默认看不到**。程序会自动给 `user_open_ids` 里映射到的用户加编辑协作者权限。没映射 open_id 的用户,其产出会被**跳过并记日志**(不报错、不阻塞其他人)。

## 配置

```bash
cp config.example.json config.json
# 编辑 config.json:填 app_id / app_secret / folder_token / user_open_ids
```

`config.json` 和 `state/` 已被 `.gitignore` 忽略,不会进版本库。

## 运行

```bash
# 跑一轮后退出(联调、验证用)
python3 sync.py --once

# 循环(每 poll_interval_seconds 一轮)
python3 sync.py
```

首版建议先 `--once` 手动跑通,确认飞书里出现表格、附件正常、目标用户能打开;稳定后再挂**独立的** launchd plist 定时(不要挂进 `com.ai-portal`,保持隔离)。

## 上线顺序(建议)

1. `python3 -m pytest tests/ -q` —— 单元测试全绿(不碰真实飞书)。
2. 飞书后台配好应用 + 权限 + folder_token。
3. `config.json` 只填 **1 个测试用户**的 open_id。
4. `python3 sync.py --once` —— 检查:飞书出现「测试用户的AI产出」表格、4 张附表、该用户能打开、图/视频进了附件列。
5. 再跑一次 `--once` —— 确认无重复记录(SQLite 幂等生效)。
6. 补齐全部用户的 open_id,上定时。

## 结构

```
sync.py       入口:扫描 → 差异 → 上传 一轮
scanner.py    只读扫 outputs,解析 子应用/用户/日期/文件
registry.py   SQLite:已传指纹 + 用户→表格映射(幂等)
feishu.py     飞书客户端(token/建表/上传/授权/写记录),纯 stdlib urllib
config.json   本地配置(gitignored)
state/        本地 SQLite(gitignored)
tests/        单元测试(mock 飞书,不联网)
```

## 回退

纯新增独立目录。删掉整个 `feishu-output-sync/` 即完全回退,对现有系统零影响。飞书侧删掉自动建的表格即可。
