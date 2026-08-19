"""无限画布后端契约验证（对运行中的实例发真实请求）。

用法：
    cd infinite-canvas
    PORTAL_INTERNAL_TOKEN=devtoken DYLD_LIBRARY_PATH=/opt/homebrew/opt/expat/lib \
      ../.venv/bin/uvicorn app_fastapi:app --host 127.0.0.1 --port 8894 &
    python3 tests/verify_contracts.py

覆盖：Portal 签名校验（含篡改/过期/无头三种拒绝）、存档 CRUD 与
PROJECT_CONFLICT 乐观锁、归属隔离、素材上传与 Range、SPA 回退。
不覆盖真实生成（会产生费用），那部分见 docs/infinite-canvas/03 的验证章节。
"""

import hashlib, hmac, json, time, urllib.parse, urllib.request, io, uuid

BASE = "http://127.0.0.1:8894"
TOKEN = "devtoken"

def headers(username="张三", user_id="u-1", is_admin=False, ts=None, bad_sig=False):
    u = urllib.parse.quote(username, safe="")
    t = str(int(time.time()) if ts is None else ts)
    msg = f"{t}:{'1' if is_admin else '0'}:{u}".encode()
    sig = hmac.new(TOKEN.encode(), msg, hashlib.sha256).hexdigest()
    if bad_sig: sig = "0"*64
    h = {"X-Username": u, "X-Portal-User-Id": user_id, "X-Portal-Ts": t, "X-Portal-Sig": sig}
    if is_admin: h["X-Is-Admin"] = "1"
    return h

def call(method, path, body=None, hdrs=None, raw=None, ctype="application/json"):
    h = dict(headers() if hdrs is None else hdrs)
    data = None
    if body is not None:
        data = json.dumps(body).encode(); h["Content-Type"] = ctype
    if raw is not None:
        data = raw
    req = urllib.request.Request(BASE+path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req) as r:
            payload = r.read()
            try: return r.status, json.loads(payload) if payload else None, dict(r.headers)
            except Exception: return r.status, payload, dict(r.headers)
    except urllib.error.HTTPError as e:
        payload = e.read()
        try: return e.code, json.loads(payload) if payload else None, dict(e.headers)
        except Exception: return e.code, payload, dict(e.headers)

results = []
def check(name, cond, detail=""):
    results.append((name, cond, detail))
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {detail if not cond else ''}")

# 1 身份
s, b, _ = call("GET", "/api/v1/session")
check("session 正确签名 200 + 中文还原", s==200 and b.get("username")=="张三" and b.get("user_id")=="u-1", f"{s} {b}")
s, b, _ = call("GET", "/api/v1/session", hdrs={})
check("session 无头 401", s==401 and b.get("code")=="unauthorized", f"{s} {b}")
s, b, _ = call("GET", "/api/v1/session", hdrs=headers(bad_sig=True))
check("session 篡改签名 401", s==401, f"{s}")
s, b, _ = call("GET", "/api/v1/session", hdrs=headers(ts=int(time.time())-200))
check("session 过期时间戳 401", s==401, f"{s}")
s, b, _ = call("GET", "/api/v1/session", hdrs=headers(is_admin=True))
check("admin 角色识别", s==200 and b.get("role")=="admin", f"{s} {b}")

# 2 projects
pid = "proj-" + uuid.uuid4().hex[:8]
doc = {"id": pid, "title": "测试画布", "nodes": [], "connections": [], "viewport": {"x":0,"y":0,"k":1}, "graphSchemaVersion": 1}
s, b, _ = call("POST", "/api/v1/projects", doc)
check("创建项目 201 + version=1", s==201 and b["version"]==1, f"{s} {b}")
s, b, _ = call("POST", "/api/v1/projects", doc)
check("重复创建幂等 200", s==200 and b["version"]==1, f"{s} {b}")
s, b, _ = call("GET", "/api/v1/projects")
check("列表含新项目", s==200 and any(p["project"]["id"]==pid for p in b["projects"]), f"{s}")
doc2 = dict(doc, title="改过的标题", expected_version=1)
s, b, _ = call("PUT", f"/api/v1/projects/{pid}", doc2)
check("更新 version→2", s==200 and b["version"]==2, f"{s} {b}")
s, b, _ = call("PUT", f"/api/v1/projects/{pid}", doc2)
check("旧版本号 409 PROJECT_CONFLICT", s==409 and b.get("code")=="PROJECT_CONFLICT", f"{s} {b}")
s, b, _ = call("GET", f"/api/v1/projects/{pid}", hdrs=headers(user_id="u-2"))
check("归属隔离：他人 404", s==404, f"{s}")

# 3 assets — 造一个最小 PNG
png = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000a49444154789c6360000002000100" "05fe02fe" "a9f9a4c50000000049454e44ae426082")
boundary = "----canvasverify"
parts = []
parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"kind\"\r\n\r\nreference\r\n".encode())
parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"media_type\"\r\n\r\nimage\r\n".encode())
parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"t.png\"\r\nContent-Type: image/png\r\n\r\n".encode() + png + b"\r\n")
parts.append(f"--{boundary}--\r\n".encode())
body = b"".join(parts)
h = dict(headers()); h["Content-Type"] = f"multipart/form-data; boundary={boundary}"
s, b, _ = call("POST", "/api/v1/assets", hdrs=h, raw=body)
ok = s==200 and isinstance(b, dict) and b.get("kind")=="reference" and b.get("status")=="active" and b.get("media_type")=="image" and str(b.get("mime_type","")).startswith("image/") and isinstance(b.get("size_bytes"), int) and b["size_bytes"]>=1
check("上传素材：六字段齐全且 mime 前缀正确", ok, f"{s} {b}")
aid = b.get("asset_id") if isinstance(b, dict) else None

if aid:
    s, b, _ = call("GET", f"/api/v1/assets/{aid}")
    check("素材元数据", s==200 and b.get("asset_id")==aid, f"{s} {b}")
    # Range
    h2 = dict(headers()); h2["Range"] = "bytes=0-9"
    req = urllib.request.Request(BASE+f"/api/v1/assets/{aid}/content", headers=h2)
    try:
        with urllib.request.urlopen(req) as r:
            check("Range 请求 206 + Content-Range", r.status==206 and "Content-Range" in r.headers, f"{r.status} {dict(r.headers)}")
    except urllib.error.HTTPError as e:
        check("Range 请求 206 + Content-Range", False, f"HTTP {e.code}")
    s, b, _ = call("GET", f"/api/v1/assets/{aid}/content", hdrs=headers(user_id="u-2"))
    check("素材归属隔离：他人 404", s==404, f"{s}")

# 空文件拒绝
parts = [f"--{boundary}\r\nContent-Disposition: form-data; name=\"media_type\"\r\n\r\nimage\r\n".encode(),
         f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"e.png\"\r\nContent-Type: image/png\r\n\r\n".encode() + b"" + b"\r\n",
         f"--{boundary}--\r\n".encode()]
s, b, _ = call("POST", "/api/v1/assets", hdrs=h, raw=b"".join(parts))
check("空文件被拒", s==400, f"{s} {b}")

# 4 其他端点
s, b, _ = call("GET", "/api/v1/models")
check("models 返回真实目录", s==200 and len(b.get("models",[]))>0, f"{s} {str(b)[:80]}")
s, b, _ = call("GET", "/api/v1/prompt-skills")
check("prompt-skills 空", s==200 and b=={"skills": []}, f"{s}")
s, b, _ = call("GET", "/api/v1/activity/assets")
check("activity/assets", s==200 and "assets" in b, f"{s}")

# 5 SPA
req = urllib.request.Request(BASE+"/canvas/whatever")
try:
    with urllib.request.urlopen(req) as r:
        html = r.read().decode("utf-8", "ignore")
        check("SPA 深层路由回退 index.html", r.status==200 and "infinite-canvas/assets" in html, f"{r.status}")
except Exception as e:
    check("SPA 深层路由回退 index.html", False, str(e))
req = urllib.request.Request(BASE+"/assets/"+__import__("os").listdir(__import__("os").path.join(__import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))), "static", "assets"))[0])
try:
    with urllib.request.urlopen(req) as r:
        check("静态资源直出", r.status==200, f"{r.status}")
except Exception as e:
    check("静态资源直出", False, str(e))

print("\n" + "="*60)
bad = [n for n,c,_ in results if not c]
print(f"{len(results)-len(bad)}/{len(results)} 通过")
if bad:
    print("失败：")
    for n in bad: print("  -", n)
