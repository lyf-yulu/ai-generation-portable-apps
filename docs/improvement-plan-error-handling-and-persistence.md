# 错误处理工程化 + 任务持久化 改进方案

## 当前现状分析

### 错误处理现状

**存在的问题：**

1. **无重试机制**：`request_json()` 中的 HTTP 请求失败直接抛异常，不区分瞬态错误（429、5xx、网络超时）和永久错误（400、401、404）

2. **错误信息不够结构化**：
   ```python
   raise RuntimeError(f"HTTP {exc.code}: {raw}")  # 把所有错误都包成 RuntimeError
   ```
   前端和日志无法区分「配额耗尽需要充值」vs「网络抖动重试即可」

3. **无指数退避**：如果加重试，需要避免 429 时疯狂重试加重 rate limit

### 任务持久化现状

**存在的问题：**

1. **JOBS 是纯内存字典**：
   ```python
   JOBS: dict[str, dict[str, Any]] = {}  # 重启后清空
   ```

2. **重启影响**：
   - Portal 重启 → 所有子应用进程被杀 → 运行中任务丢失
   - 用户刷新页面 → 看不到 5 分钟前提交的任务（前端只查当前 workspace 的任务）

3. **统计数据不完整**：
   - `usage.json` 只记录「Portal 看到的 job 创建」，子应用直连 8787 的任务不计入
   - 没有失败率、平均耗时等运维指标

---

## 改进方案 1：错误处理工程化

### 设计目标

- ✅ 自动重试瞬态错误（429、5xx、网络超时）
- ✅ 快速失败永久错误（400、401、403、404）
- ✅ 指数退避避免雪崩
- ✅ 结构化错误信息方便前端展示和日志分析

### 实现方案

#### 1. 定义错误分类

```python
# seedance/app.py 顶部新增

class APIError(Exception):
    """结构化 API 错误，携带 HTTP 状态码和是否可重试标志"""
    def __init__(self, status_code: int, message: str, raw_response: str = ""):
        self.status_code = status_code
        self.message = message
        self.raw_response = raw_response
        super().__init__(f"HTTP {status_code}: {message}")
    
    @property
    def is_retryable(self) -> bool:
        """判断是否值得重试"""
        # 429 限流、408 超时、5xx 服务端错误
        if self.status_code in (408, 429, 500, 502, 503, 504):
            return True
        return False
    
    @property
    def error_category(self) -> str:
        """错误分类，方便前端展示"""
        if self.status_code == 401:
            return "auth_failed"
        elif self.status_code == 403:
            return "permission_denied"
        elif self.status_code == 429:
            return "rate_limited"
        elif 400 <= self.status_code < 500:
            return "client_error"
        elif 500 <= self.status_code < 600:
            return "server_error"
        return "unknown"


class NetworkError(Exception):
    """网络连接失败，通常可重试"""
    pass
```

#### 2. 改造 `request_json` 加入重试逻辑

```python
def request_json(
    method: str, 
    url: str, 
    api_key: str, 
    body: dict[str, Any] | None = None, 
    timeout: int = 600,
    max_retries: int = 6  # 新增参数
) -> dict[str, Any]:
    """
    发送 JSON 请求，自动重试瞬态错误。
    
    重试策略：
    - 429/5xx: 最多重试 6 次，指数退避（1s, 2s, 4s, 8s, 16s, 32s）
    - 网络超时/连接失败: 最多重试 6 次
    - 4xx（除 408/429）: 不重试，立即抛出
    """
    headers = {"Authorization": f"Bearer {api_key}"}
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    
    last_error = None
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                return json.loads(raw) if raw else {}
        
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            error = APIError(exc.code, raw, raw)
            
            # 4xx 非瞬态错误，立即失败
            if not error.is_retryable:
                raise error
            
            # 429/5xx 可重试错误
            last_error = error
            if attempt < max_retries - 1:
                backoff = min(2 ** attempt, 32)  # 最多等 32 秒
                time.sleep(backoff)
                continue
            raise error  # 最后一次重试也失败，抛出
        
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            # 网络错误，可重试
            last_error = NetworkError(f"连接失败 ({exc.__class__.__name__}: {exc})")
            if attempt < max_retries - 1:
                backoff = min(2 ** attempt, 32)
                time.sleep(backoff)
                continue
            raise RuntimeError(f"API 请求失败，已重试 {max_retries} 次: {last_error}") from exc
    
    # 不应该走到这里
    raise last_error or RuntimeError("Unknown error in request_json")
```

#### 3. 在 `run_one` 中捕获并记录错误类型

```python
# 在 run_one() 的 try-except 改为：
try:
    create_result = request_json("POST", create_url, api_key, payload)
    # ...
except APIError as e:
    # 结构化错误，记录到 job
    add_event(job_id, f"Run {index}: API Error [{e.error_category}] {e.message}")
    raise RuntimeError(f"API 调用失败 ({e.error_category}): {e.message}") from e
except NetworkError as e:
    add_event(job_id, f"Run {index}: 网络连接失败，已重试多次")
    raise RuntimeError(f"网络连接失败: {e}") from e
```

#### 4. 前端错误提示优化

修改 `seedance/static/app.js`，根据错误类型显示友好提示：

```javascript
// 在 pollJob() 的错误处理中：
if (job.status === 'failed' && job.errors && job.errors.length) {
  const firstError = job.errors[0];
  let userMessage = firstError;
  
  // 识别错误类型，给出友好提示
  if (firstError.includes('auth_failed') || firstError.includes('401')) {
    userMessage = '❌ API Key 无效或已过期，请检查配置';
  } else if (firstError.includes('rate_limited') || firstError.includes('429')) {
    userMessage = '⏱️ 请求过于频繁，请稍后再试（已自动重试多次仍失败）';
  } else if (firstError.includes('permission_denied') || firstError.includes('403')) {
    userMessage = '🚫 权限不足或配额已用完，请联系管理员';
  } else if (firstError.includes('server_error') || firstError.includes('5xx')) {
    userMessage = '⚠️ API 服务暂时不可用，请稍后重试';
  } else if (firstError.includes('网络连接失败')) {
    userMessage = '🌐 网络连接失败，请检查网络或 API 地址';
  }
  
  this.statusText = userMessage;
}
```

---

## 改进方案 2：任务持久化

### 设计目标

- ✅ 任务历史持久化到磁盘，重启后不丢失
- ✅ 支持跨 workspace 查询（用户可以看到自己的所有历史任务）
- ✅ 为统计页提供更丰富的数据（失败率、平均耗时、用户分布）

### 实现方案

#### 1. 新增任务历史持久化文件

```python
# seedance/app.py 顶部新增

TASK_HISTORY_FILE = STATE_DIR / "task_history.jsonl"  # 用 JSONL 格式，每行一个任务
TASK_HISTORY_LOCK = threading.Lock()

def append_task_to_history(job_id: str, job_data: dict[str, Any]) -> None:
    """追加任务到历史文件（JSONL 格式，每行一个 JSON 对象）"""
    try:
        # 只保存关键字段，不保存 events（太长）
        record = {
            "job_id": job_id,
            "username": job_data.get("username", ""),
            "workspace_id": job_data.get("workspace_id", ""),
            "provider": job_data.get("provider", ""),
            "model": job_data.get("model", ""),
            "prompt": (job_data.get("prompt") or "")[:200],  # 截断长提示词
            "status": job_data.get("status", ""),
            "total": job_data.get("total", 0),
            "done": job_data.get("done", 0),
            "errors_count": len(job_data.get("errors", [])),
            "results_count": len(job_data.get("results", [])),
            "created_at": job_data.get("created_at", ""),
            "started_at": job_data.get("started_at"),
            "finished_at": job_data.get("finished_at"),
            "duration_seconds": job_data.get("finished_at", 0) - job_data.get("started_at", 0) if job_data.get("finished_at") else None,
        }
        with TASK_HISTORY_LOCK:
            with TASK_HISTORY_FILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        # 持久化失败不影响任务执行
        print(f"Warning: failed to append task history: {e}")


def load_task_history(username: str = "", limit: int = 100) -> list[dict[str, Any]]:
    """从历史文件加载任务列表（按时间倒序）"""
    try:
        if not TASK_HISTORY_FILE.exists():
            return []
        
        tasks = []
        with TASK_HISTORY_LOCK:
            with TASK_HISTORY_FILE.open("r", encoding="utf-8") as f:
                for line in f:
                    try:
                        task = json.loads(line.strip())
                        if username and task.get("username") != username:
                            continue  # 按用户过滤
                        tasks.append(task)
                    except json.JSONDecodeError:
                        continue  # 跳过损坏的行
        
        # 按 finished_at 或 created_at 倒序
        tasks.sort(key=lambda t: t.get("finished_at") or t.get("created_at") or "", reverse=True)
        return tasks[:limit]
    
    except Exception as e:
        print(f"Warning: failed to load task history: {e}")
        return []
```

#### 2. 在任务完成时写入历史

```python
# 在 run_job() 的任务完成处修改：

def run_job(...):
    try:
        # ... 原有逻辑 ...
        
        # 任务完成后，持久化到历史文件
        final_job["status"] = final_status
        update_activity(activity_id, status=final_status, result=final_job, finished_at=time.time())
        
        # 新增：持久化任务历史
        append_task_to_history(job_id, final_job)
        
        add_event(job_id, "Finished")
        report_final_to_portal(job_id, final_status)
    except Exception as exc:
        set_job(job_id, status="failed", errors=[str(exc)], finished_at=time.time())
        with JOBS_LOCK:
            failed_job = json.loads(json.dumps(JOBS[job_id]))
        
        # 新增：即使失败也要记录
        append_task_to_history(job_id, failed_job)
        
        update_activity(activity_id, status="failed", result=failed_job, finished_at=time.time())
        # ...
```

#### 3. 新增 API 端点：查询历史任务

```python
# 在 Handler 类中新增：

def handle_history_get(self) -> None:
    """GET /api/history?username=xxx&limit=50"""
    query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
    username = query.get("username", [""])[0]
    limit = int(query.get("limit", ["100"])[0])
    
    tasks = load_task_history(username=username, limit=limit)
    
    self.send_response(200)
    self.send_header("Content-Type", "application/json; charset=utf-8")
    self.end_headers()
    self.wfile.write(json.dumps({"tasks": tasks}, ensure_ascii=False).encode("utf-8"))
```

#### 4. 前端新增「历史任务」Tab

在 `seedance/static/index.html` 的 `.sub-tabs` 中加第三个按钮：

```html
<div class="sub-tabs" style="margin-bottom:10px">
  <button type="button" class="tabBtn" :class="{ isActive: wsTab === 'jobs' }" @click="switchJobsTab()">任务</button>
  <button type="button" class="tabBtn" :class="{ isActive: wsTab === 'activity' }" @click="switchActivityTab()">活动</button>
  <button type="button" class="tabBtn" :class="{ isActive: wsTab === 'history' }" @click="switchHistoryTab()">历史</button>
</div>

<!-- 新增历史任务面板 -->
<div v-show="wsTab==='history'">
  <div class="resultHeader"><h2>历史任务 <button @click="loadHistory()" style="font-size:11px;padding:3px 10px;margin-left:8px;cursor:pointer">刷新</button></h2></div>
  <div v-if="historyTasks.length===0" style="color:#697386;font-size:12px;padding:12px 0">暂无历史记录</div>
  <div v-for="t in historyTasks" :key="t.job_id" style="border:1px solid #e2e8f0;border-radius:8px;padding:10px;margin-bottom:10px">
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">
      <span :class="'tag tag-'+t.status">{{ t.status }}</span>
      <span style="font-size:12px;flex:1">{{ t.prompt }}</span>
      <span style="font-size:11px;color:#697386">{{ t.created_at }}</span>
    </div>
    <div style="font-size:11px;color:#697386">
      耗时: {{ t.duration_seconds ? Math.round(t.duration_seconds) + 's' : 'N/A' }} | 
      完成: {{ t.results_count }}/{{ t.total }} | 
      错误: {{ t.errors_count }}
    </div>
  </div>
</div>
```

在 `seedance/static/app.js` 中新增方法：

```javascript
data() {
  return {
    // ... 现有字段
    wsTab: 'jobs',  // 'jobs' | 'activity' | 'history'
    historyTasks: [],
  };
},

methods: {
  switchHistoryTab() {
    this.wsTab = 'history';
    this.loadHistory();
  },
  
  async loadHistory() {
    try {
      const res = await this.api('/api/history?limit=50');
      if (res.ok) {
        const data = await res.json();
        this.historyTasks = data.tasks || [];
      }
    } catch (e) {
      console.error('Failed to load history:', e);
    }
  },
}
```

---

## 改进优先级

| 改进项 | 用户价值 | 工作量 | 风险 | 建议优先级 |
|--------|----------|--------|------|-----------|
| **错误分类 + 友好提示** | ⭐⭐⭐⭐⭐ 用户知道为啥失败 | 0.5d | 低 | 🔥 P0 |
| **HTTP 请求自动重试** | ⭐⭐⭐⭐⭐ 减少瞬态失败 | 0.5d | 中（需要测试） | 🔥 P0 |
| **任务历史持久化** | ⭐⭐⭐⭐ 重启不丢任务 | 1d | 低 | 🟡 P1 |
| **历史任务 Tab** | ⭐⭐⭐ 方便查旧任务 | 0.5d | 低 | 🟡 P1 |

**建议实施顺序：**
1. 先做错误分类 + 自动重试（P0，立竿见影，1 天完成）
2. 再做任务持久化 + 历史 Tab（P1，1.5 天完成）

---

## 同步到其他子应用

改完 Seedance 后，同样的模式可以复制到：
- `nano-banana/app.py`
- `dreamina/app.py`
- `volcengine-portrait/app.py`

每个子应用复制粘贴核心逻辑（`APIError`、`request_json` 重试、`append_task_to_history`），保持一致的错误处理和持久化行为。

---

## 需要你确认的问题

1. **重试次数**：我设的 6 次（最长等 63 秒），会不会太激进？改成 3 次（最长 7 秒）？
2. **历史文件大小**：JSONL 无限追加会不会太大？要不要加定期归档（如保留最近 30 天）？
3. **网络超时的 timeout**：当前 `request_json` 默认 600 秒，要不要针对不同操作分别设置（创建任务 60s，轮询状态 30s）？
