# -*- coding: utf-8 -*-
"""打一个已经在运行的中转，确认整条链路可用。

跟 test_v14.py 的区别：那个脚本自己拉起中转进程、验证「参数矫正」的具体行为；
这个脚本只发请求到**你已经跑起来的**实例，所以既适合本机源码运行，也适合
Docker 部署后确认容器是否真的能用：

    # 本机源码
    python src/tests/smoke_running.py

    # Docker（容器映射在宿主机 8787）
    python src/tests/smoke_running.py http://127.0.0.1:8787

    # 局域网里的另一台设备
    python src/tests/smoke_running.py http://192.168.1.10:8787

检查项：
    1. GET  /health                    本地探针（不碰上游）
    2. GET  /v1/models                 经中转取模型清单（验证 GET 转发）
    3. POST /v1/chat/completions       非流式，且故意塞上游不认的字段，
                                       顺带验证自动矫正（reasoning/thinking/user）
    4. POST /v1/chat/completions       流式（stream=true，验证 SSE 分块转发）
    5. POST /v1/messages               应被拦成 400 中文提示（Anthropic 协议）

匿名限流约每 30 秒 2 次，撞上 429 会等待后重试。退出码 0 = 全过。
"""
import json
import sys
import time
import urllib.error
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8787").rstrip("/")
MODEL = "Qwen3.8-27B"
WAIT_ON_429 = 32
FAILED = []


def call(path, data=None, ctype=None, timeout=180):
    headers = {"Accept": "application/json"}
    if ctype:
        headers["Content-Type"] = ctype
    # 故意带一个假 key：中转应当把它整条剥掉
    headers["Authorization"] = "Bearer sk-ovh-smoke-test"
    req = urllib.request.Request(BASE + path, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        return 0, ("%s: %s" % (type(e).__name__, e)).encode("utf-8")


def with_retry(fn, tries=3):
    for i in range(tries):
        status, body = fn()
        if status != 429:
            return status, body
        print("        429 匿名限流，等 %d 秒后重试（%d/%d）" % (WAIT_ON_429, i + 1, tries))
        time.sleep(WAIT_ON_429)
    return status, body


def check(name, ok, detail=""):
    print("  [%s] %s%s" % ("通过" if ok else "失败", name, ("  " + detail) if detail else ""))
    if not ok:
        FAILED.append(name)


print("目标：%s" % BASE)
print("提示：未运行请先用 python src/ovh_free_proxy.py 或 docker compose up -d 起中转\n")

# 1) 本地探针
status, body = call("/health", timeout=10)
ok = status == 200 and b"ovh-free-proxy" in body
check("GET /health", ok, "HTTP %d" % status)

# 2) 模型清单（经中转的 GET 转发）
status, body = with_retry(lambda: call("/v1/models", timeout=30))
n = 0
if status == 200:
    try:
        n = len(json.loads(body).get("data", []))
    except Exception:
        n = -1
check("GET /v1/models", status == 200 and n > 0, "HTTP %d，模型 %s 个" % (status, n))

# 3) 非流式对话：故意夹带上游不认的字段，自动矫正应当把它们摘掉
payload = {
    "model": MODEL,
    "messages": [{"role": "user", "content": "只回答两个字：收到"}],
    "max_tokens": 24,
    "stream": False,
    "reasoning": {"effort": "none"},   # 嵌套对象，上游整条拒收
    "thinking": True,                  # 上游不认
    "user": "smoke-test",              # 上游不认
}
status, body = with_retry(lambda: call("/v1/chat/completions",
                                       json.dumps(payload).encode(), "application/json"))
detail = "HTTP %d" % status
if status == 200:
    d = json.loads(body)
    reply = (d["choices"][0]["message"].get("content") or "").strip()
    rt = d.get("usage", {}).get("completion_tokens_details", {}).get("reasoning_tokens")
    detail += "，回复 %r，reasoning_tokens=%s" % (reply[:20], rt)
    ok = bool(reply)
else:
    detail += "，" + body[:160].decode("utf-8", "replace").replace("\n", " ")
    ok = False
check("POST /v1/chat/completions（非流式 + 自动矫正）", ok, detail)

# 4) 流式
stream_payload = dict(payload)
for junk in ("reasoning", "thinking", "user"):
    stream_payload.pop(junk, None)
stream_payload["stream"] = True
stream_payload["stream_options"] = {"include_usage": True}
status, body = with_retry(lambda: call("/v1/chat/completions",
                                       json.dumps(stream_payload).encode(), "application/json"))
text = body.decode("utf-8", "replace")
chunks = text.count("data:")
check("POST /v1/chat/completions（流式 SSE）", status == 200 and chunks > 1,
      "HTTP %d，data 分块 %d 个" % (status, chunks))

# 5) Anthropic 协议应被拦下
status, body = call("/v1/messages", b"{}", "application/json")
check("POST /v1/messages 被拦成 400", status == 400 and "Chat Completions" in body.decode("utf-8", "replace"),
      "HTTP %d" % status)

print()
if FAILED:
    print("结果：%d 项失败 -> %s" % (len(FAILED), "，".join(FAILED)))
    sys.exit(1)
print("结果：全部通过，这个中转可以用了（客户端填 %s/v1）" % BASE)
sys.exit(0)
