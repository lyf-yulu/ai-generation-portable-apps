# 既有缺陷：nano-banana 失败任务产生幽灵图片计数

**发现日期**：2026-08-14
**发现场景**：接入无限画布时做统计链路真实验证，顺带发现
**状态**：**未修复** —— 需要用户决策，且涉及生产图片主链路
**与无限画布的关系**：无。画布的翻译层不受影响（见文末）

---

## 现象

nano-banana 任务因 API 鉴权失败而整体失败时，仍向 Portal 报告 `done: 1`，
Portal 据此在 `by_user[date][user]["nano-banana"]["images"]` 记入 1 张
**根本不存在的图片**。

实测（2026-08-14，绕过 Portal 直接调用 nano-banana）：

```
POST /api/jobs/json  → {"ok":true,"job_id":"a477a1e0..."}
GET  /api/jobs/a477a1e0...
  status: failed
  done:   1        ← 问题在此
  total:  1
  errors: ["[auth_failed] {...\"message\":\"无效的令牌\"...}"]
  results: []      ← 实际零产出
```

## 根因

`nano-banana/app.py:1939-1961` 的四个分支中，**成功与三种异常路径全都**
执行 `JOBS[job_id]["done"] += 1`：

```python
try:
    result = future.result()
    with LOCK:
        JOBS[job_id]["results"].append(result)
        JOBS[job_id]["done"] += 1          # :1943 成功
except APIError as exc:
    with LOCK:
        JOBS[job_id]["errors"].append(error_msg)
        JOBS[job_id]["done"] += 1          # :1949 API 错误
except NetworkError as exc:
    with LOCK:
        JOBS[job_id]["errors"].append(error_msg)
        JOBS[job_id]["done"] += 1          # :1955 网络错误
except Exception as exc:
    with LOCK:
        JOBS[job_id]["errors"].append(str(exc))
        JOBS[job_id]["done"] += 1          # :1960 其他异常
```

即 `done` 在 nano-banana 里的语义是**"跑完的轮次数"**，而 Portal 的契约要求
它是**"真实产出件数"**。两边对同一个字段的理解不一致。

Portal 侧的预设写得很明确（`portal/app.py:1003-1010`）：

> `done` is the count of items the sub-app actually produced …
> A pure failure reports done=0.
> We record the real `done` verbatim. Do NOT floor it to max(1, ...) —
> that turned a 0-output failure into one phantom image in by_user.images

Portal 已经很小心地不做 `max(1,...)` 兜底，但兜底与否都救不了上游直接报 1。

## 影响链路

```
nano-banana 任务失败 → done=1
  ↓ report_final_to_portal(job_id, "failed")
Portal finalize_job() 回滚 daily[date]["nano-banana"]["jobs"]  ← 这一半是对的
  ↓ 但 finalize 刻意不把任务移出 _pending_jobs（portal/app.py:1071-1076）
Portal _job_poll_loop() 仍会轮询到该任务
  ↓ 读到 status=failed、done=1
  ↓ if done > 0:  _add_user_stat(..., images=done)   ← portal/app.py:1027,1036
by_user[date][user]["nano-banana"]["images"] += 1     ← 幽灵计数
```

注意 `daily.jobs` 和 `by_user.images` 是两个**不相交**的计数器，走两条独立
路径（finalize 管前者，轮询管后者）。所以失败任务的"任务数"被正确回滚了，
但"图片数"没有 —— 只看 `daily.jobs` 不会发现这个问题。

## 影响范围（尚未量化）

`portal/state/usage.json` 中 `by_user` 的 `nano-banana.images` 存在偏高，
偏高量 = 历史上失败的 nano-banana 运行轮次数。

2026-08-14 当日快照：`daily["nano-banana"] = {requests: 24999, jobs: 162}`，
`by_user["高大王"]["nano-banana"] = {images: 161}`。当日图片 Key 处于失效
状态（t8star 报"该令牌状态不可用"、Chiyun 报"无效的令牌"），因此这 161 中
**可能有相当比例是幽灵计数**，但我没有逐条核对 —— 判断需要 JSONL 明细
（`portal/state/logs/`，保留 30 天）。

**未做量化的原因**：这超出了本次接入任务的范围，且修正历史数据是不可逆
操作，应由用户决定。

## 可选修复方案

### 方案 A：改 nano-banana（推荐，治本）

只在真正产出时增加 `done`，失败分支不加：

```python
except APIError as exc:
    with LOCK:
        JOBS[job_id]["errors"].append(error_msg)
        # 不再 done += 1
```

但 `done` 同时被前端用作进度显示（`done/total`），改了会让失败任务的进度条
停在 0/1 而不是 1/1。需要同步引入独立的 `finished` 字段给进度用，`done`
只保留"产出件数"语义。**改动面涉及生产图片主链路的前端与后端，需要回归。**

同样的模式要检查 seedance（`seedance/app.py` 的对应位置）是否也存在。

### 方案 B：改 Portal（治标，改动面小）

轮询时不只看 `done`，同时要求上游 `results` 非空：

```python
produced = len(data.get("results") or []) or 0
if done > 0 and produced > 0:
    ...
```

但 seedance 与 nano-banana 的 `results` 结构不同（前者平铺、后者嵌套
`images[]`），要按 app 分别取长度，Portal 会因此知道子应用的内部结构 ——
与现有"子应用自报字段、Portal 不解析结构"的分层相悖。

### 方案 C：只修正历史数据，不改代码

依据 `portal/state/logs/` 的 JSONL 明细重算 `by_user`。治标不治本，
新的失败仍会继续累积。

## 无限画布为什么不受影响

翻译层的 `done` 按**实际摄取成功的文件数**计算，而不是转发上游的 `done`
（`infinite-canvas/translate.py` 的 `_ingest_results` 返回 `len(items)`）：

- 上游报 `done=1` 但 `results` 为空 → 摄取到 0 个文件 → 我们报 `done=0`
- 上游部分成功（3 成 1 败）→ 摄取到 3 个文件 → 我们报 `done=3`

实测同一个失效 Key 场景下，画布任务正确报告 `done: 0`、`status: failed`，
Portal 不会记入图片数。
