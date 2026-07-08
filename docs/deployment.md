# Portal 部署文档

> 适用：macOS 上 launchd 守护的生产部署
> 更新：2026-06-25

---

## 1. 当前部署拓扑

```
┌─────────────────────────────┐
│  launchd (用户域 GUI/501)   │
│  ↓ KeepAlive=true            │
│  com.ai-portal (Portal 9090) │
│      ↓ subprocess.Popen      │
│  ├── seedance (8787)         │
│  ├── nano-banana (8797)      │
│  ├── dreamina (8888)         │
│  └── volcengine-portrait (8891)│
└─────────────────────────────┘
                ↑ HTTPS 自签证书
       LAN: https://192.168.30.5:9090
```

- Portal 由 `~/Library/LaunchAgents/com.ai-portal.plist` 守护启动，**不是** `启动器.command`
- `启动器.command` 是 GUI 工具：自己启动 Portal 进程（不走 launchd）+ 显示菜单。和 launchd 守护**互不感知**——双开会冲突端口
- cloudflared tunnel 已 unload，需要外网时再 `launchctl bootstrap`
- 自签证书 SAN 含 `127.0.0.1` 和当前 LAN IP，IP 漂移时 Portal 启动期会自动重签

---

## 2. 关键文件位置

| 文件 | 路径 |
|---|---|
| plist | `~/Library/LaunchAgents/com.ai-portal.plist` |
| Portal stdout 日志 | `~/Library/Logs/ai-portal.log` |
| Portal stderr 日志 | `~/Library/Logs/ai-portal.err` |
| 子应用日志 | `portal/state/logs/<app>.log` |
| 自签证书 | `portal/certs/cert.pem`, `key.pem`, `lan_ip.txt` |
| 用户/会话/密钥 | `portal/state/users.json`, `sessions.json`, `user_keys.json` |
| 用量统计 | `portal/state/usage.json`（+ `.bak`） |
| 安全重启脚本 | `portal/state/scripts/safe_restart.sh` |

---

## 3. plist 关键字段

```xml
<key>Label</key>            <string>com.ai-portal</string>
<key>WorkingDirectory</key> <string>/Users/260413a/ai-generation-portable-apps/portal</string>
<key>ProgramArguments</key>
<array>
    <string>/usr/bin/python3</string>  <!-- 系统 3.9，不要 homebrew 3.12 -->
    <string>app.py</string>
</array>
<key>EnvironmentVariables</key>
<dict>
    <key>PORTAL_PORT</key> <string>9090</string>
    <key>PATH</key>        <string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/Users/260413a/.local/bin:/opt/homebrew/bin</string>
    <key>HOME</key>        <string>/Users/260413a</string>
</dict>
<key>RunAtLoad</key>      <true/>   <!-- 开机自启 -->
<key>KeepAlive</key>      <true/>   <!-- 进程崩溃自动拉起 -->
<key>ThrottleInterval</key> <integer>10</integer>  <!-- 重启间隔 ≥ 10s -->
```

**注意**：
- `ProgramArguments` 必须是 `/usr/bin/python3`（macOS 自带 3.9）。换成 homebrew 的 `python3.12` 后子进程 `subprocess.Popen([sys.executable, "app.py"])` 会用 3.12，但子应用代码假设 stdlib 行为；如果 brew 3.12 升级或 PATH 变化导致 sys.executable 不一致，子进程会走错版本
- 不要再加 `PORTAL_HTTP_ONLY=1`（旧版 cloudflared 模式残留）

---

## 4. 启停命令

| 操作 | 命令 |
|---|---|
| 加载（首次部署） | `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ai-portal.plist` |
| 卸载 | `launchctl bootout gui/$(id -u)/com.ai-portal` |
| 启动 | `launchctl kickstart gui/$(id -u)/com.ai-portal` |
| **重启**（推荐） | `bash portal/state/scripts/safe_restart.sh` |
| 强制重启 | `launchctl kickstart -k gui/$(id -u)/com.ai-portal` |
| 查询状态 | `launchctl print gui/$(id -u)/com.ai-portal \| head -30` |
| 看实时日志 | `tail -f ~/Library/Logs/ai-portal.log` |

---

## 5. 重启前必检（safe_restart.sh 已固化）

1. **关 VPN**（GoGoJump / Tailscale）：避免 `lan_ip.txt` 写入 `240.0.0.1` / `100.64.x.x` 污染证书 SAN
2. **无 in-flight 任务**：`curl -sk https://127.0.0.1:9090/api/platform/activity` 看最近 5 分钟没人活动
3. **无 running jobs**：检查 4 个子应用 `/api/jobs` 列表无 status=running
4. **同事下班时间**：重启会杀掉所有 in-memory job

`safe_restart.sh` 自动跑这 3+1 项检查；任意失败拒绝重启，加 `--force` 跳过。

---

## 6. cloudflared 隧道（外网访问）

仅当需要外网访问时：
```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ai-portal-tunnel.plist
```
当前已 unload，状态：
```bash
launchctl print gui/$(id -u)/com.ai-portal-tunnel
# Bad request. Could not find service in domain
```

cloudflared 跑起来时 Portal 仍走 9090，但建议 plist 里加 `PORTAL_HTTP_ONLY=1` 让 cloudflared 终结 TLS。**当前 com.ai-portal.plist 没有这个变量**，HTTPS 自签直连局域网。

---

## 7. LAN IP 漂移与证书重签

- `portal/app.py:get_lan_ip()` 优先从 ifconfig 私有段（192.168/10/172.16-31）拿 IP
- VPN 抢路由时跳过 240/100.64-127/169.254/198.18-19 黑名单
- 启动时若 `lan_ip.txt` 与当前 IP 不一致，自动 unlink cert/key 并重签
- 重签需要 `openssl` 在 PATH 中（`/usr/bin/openssl` 系统自带）

**lan_ip 漂移时表现**：
- Portal 启动日志：`LAN IP changed, regenerating cert...`
- 浏览器需重新接受证书警告

---

## 8. 端口表

| 应用 | 生产 | 测试（Start Test.command） |
|---|---|---|
| Portal HTTPS | 9090 | 9190 |
| Portal HTTP→HTTPS 重定向 | 9089 | 9189 |
| seedance | 8787 | 8788 |
| nano-banana | 8797 | 8798 |
| dreamina | 8888 | 8890 |
| volcengine-portrait | 8891 | 8892 |

**新增子应用务必：**
1. 在新窗口写代码，避免和 release zip 端口冲突
2. 在 `portal/app.py:APPS` 注册（含独立环境变量端口名）
3. 在 `Start Test.command` 加测试端口（生产 + 1）
4. 在 `启动器.command:PORTS` 数组加端口
5. 重新打 release zip

---

## 9. 孤儿端口排查 SOP

**症状**：launchctl kickstart 后 Portal 启动卡在 `[Errno 48] Address already in use`

**排查**：
```bash
# 1. 看哪个端口被占
for p in 8787 8797 8888 8891 9090; do
  echo "=== port $p ==="
  lsof -nP -iTCP:$p -sTCP:LISTEN 2>/dev/null
done

# 2. 找 PPID=1 的孤儿（被 launchd 收养）
ps -axo pid,ppid,command | awk '$2==1 && /app\.py/ {print}'

# 3. 看 launchctl 视角
launchctl print gui/$(id -u)/com.ai-portal | head -30
```

**根因**：`launchctl kickstart -k` 会发 SIGKILL 杀主进程，Python 的 `finally manager.shutdown()` 跳过 → 子应用进程被 launchd 收养成孤儿。Portal 启动时 `_kill_port_squatter` 会 `lsof + SIGKILL` 兜底清。

**手动清残留**：
```bash
for p in 8787 8797 8888 8891; do
  lsof -ti:$p | xargs -I {} kill -9 {} 2>/dev/null
done
launchctl kickstart -k gui/$(id -u)/com.ai-portal
```

---

## 10. 启动器.command 与 launchd 互不感知

`启动器.command` 是给开发期用的菜单工具，它：
- 用 `python3 app.py &` 启动（不走 launchd）
- 把 PID 存到 `portal/.launcher_pid.json`
- 不关心 launchd 是否在守护

**冲突场景**：launchd 已守护 Portal 在 9090，用户双击 `启动器.command` 选启动 → 第二个 Python 试 bind 9090 失败崩溃。

**正确用法（生产机）**：
- 用 `safe_restart.sh` 或 `launchctl kickstart` 重启
- 用 `tail -f ~/Library/Logs/ai-portal.log` 看日志
- **不要用** `启动器.command`

`启动器.command` 仅在 launchd 未加载（开发机）时使用。

---

## 11. 故障排查速查

| 症状 | 检查 |
|---|---|
| 网站连不上 | `lsof -ti:9090`；`launchctl print gui/$(id -u)/com.ai-portal` |
| 浏览器证书警告 | `cat portal/certs/lan_ip.txt` 与 `ifconfig` 私有段比对 |
| seedance/nano-banana 任务一直 pending | `tail -f portal/state/logs/<app>.log` |
| 子应用反复 unhealthy/restarting | 端口被孤儿占（见第 9 节）；或 sub-app 内部异常 |
| `usage.json` 损坏 | 自动从 `.bak` 恢复，损坏文件被 quarantine 到 `usage.corrupt.<ts>.json` |

## seedance + portrait「附加参考素材」走 TOS

火山方舟 Ark 生成任务对参考素材（image_url / video_url / audio_url）的 URL 字段要求公网 https。两个相关子应用都通过把上传的素材 PUT 到火山对象存储 TOS（私有 bucket）拿到预签名 GET URL（默认 TTL 12 小时），再传给 Ark。bucket 不必公共读 — 预签名 URL 已经把签名带在 query 里，方舟匿名 GET 即可。

**适用范围**：
- seedance：火山方舟 provider 下所有图片/视频/音频参考素材（图片不再用 base64 data URL）
- volcengine-portrait：用户「图2 上传本地图」（extras）—— 资产库里主动创建的 asset 不走这条，继续用 `asset://` scheme

**一次性配置步骤**：

1. 在火山控制台准备 TOS bucket（如果还没有）
   - 地域：华北2(北京) — region 字符串 `cn-beijing`
   - 权限：「私有」就行（**不需要**公共读，代码用预签名 URL）
   - 名称：本项目用的是 `seedance-sd`
2. 子用户 AK/SK 权限：至少包含 `tos:PutObject` 和 `tos:GetObject`（或挂 `TOSFullAccess` 策略），同一对 AK/SK 同时被人像 API 用，所以 ark 权限也得保留
3. 在 `seedance/state/secrets.json` 配 bucket：
   ```json
   {
     "volcengine_api_key": "ark-...",
     "tos_bucket": "seedance-sd",
     "tos_region": "cn-beijing"
   }
   ```
4. 在 `volcengine-portrait/config.json` 加两个字段：
   ```json
   {
     "...": "保留现有所有字段",
     "tos_bucket": "seedance-sd",
     "tos_region": "cn-beijing"
   }
   ```
5. portal admin 通过「火山方舟人像 Key」面板已经配过 AK/SK 的话不用动；portal 启动时会读 portrait config.json 把 AK/SK 通过 env 注入给 seedance 和 portrait 两边
6. 重启 portal（kill portal pid，launchd 自动拉起，所有子应用一并加载新 env）

**验证**：
- seedance 子应用日志：火山 provider 提交任务时应看到 `PUT https://seedance-sd.tos-cn-beijing.volces.com/refmedia/<hex>.<ext>` 后跟 200/201
- portrait：用户在「图2」选「上传本地图」并提交时同样能在 portrait 日志里看到 PUT 行
- 传给 Ark 的 URL 包含 `?X-Tos-Algorithm=TOS4-HMAC-SHA256&...&X-Tos-Signature=...` 这串 query
- 配置缺失时错误消息会指明缺什么字段
