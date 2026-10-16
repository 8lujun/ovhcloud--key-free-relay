# -*- coding: utf-8 -*-
"""验证打包后的 exe（v1.4）带上了新矫正逻辑。

核心用例：ZCode 那份真实请求体，直连上游 400，过 exe 中转而 200，
且 reasoning_effort 保持 'none' 不被改写。
"""
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
EXE = os.path.join(os.path.dirname(HERE), "dist", "OVH免密钥中转.exe")
PORT = 8799
BASE = "http://127.0.0.1:%d" % PORT
MODEL = "Qwen3.8-27B"
UPSTREAM = "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1/chat/completions"
GAP = 17

ZCODE_BODY = {
    "model": MODEL,
    "messages": [{"role": "system", "content": "你是助手"},
                 {"role": "user", "content": "只回两个字：收到"}],
    "max_completion_tokens": 64,
    "reasoning": {"effort": "none"},
    "reasoning_effort": "none",
    "stream": True,
    "stream_options": {"include_usage": True},
    "thinking": {"type": "enabled"},
    "enable_thinking": True,
}


def req(path, method="GET", body=None, headers=None, timeout=180):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method)
    if body is not None:
        r.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        r.add_header(k, v)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def direct(body, timeout=200):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    r = urllib.request.Request(UPSTREAM, data=json.dumps(body).encode(), method="POST",
                               headers={"Content-Type": "application/json",
                                        "Accept-Encoding": "identity"})
    try:
        with urllib.request.urlopen(r, timeout=timeout, context=ctx) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def main():
    print("=" * 100)
    print("exe v1.4 验证   %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 100)
    if not os.path.exists(EXE):
        print("exe 不存在:", EXE)
        return 1
    print("[1] exe:", os.path.getsize(EXE) / 1024 / 1024, "MB")

    p = subprocess.Popen([EXE, "--port", str(PORT), "--no-open", "--no-check",
                          "--strict-port"],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    ready, t0 = False, time.time()
    while time.time() - t0 < 40:
        try:
            with urllib.request.urlopen(BASE + "/health", timeout=3) as r:
                if r.status == 200:
                    ready = True
                    break
        except Exception:
            time.sleep(0.5)
    print("[2] 就绪:", ready, "%.1fs" % (time.time() - t0))
    if not ready:
        p.terminate()
        return 1
    with urllib.request.urlopen(BASE + "/health", timeout=5) as r:
        h = json.loads(r.read().decode())
    print("    version =", h.get("version"), "| service =", h.get("service"))

    print("[3] 说明页")
    rq = urllib.request.Request(BASE + "/", headers={"Accept": "text/html"})
    with urllib.request.urlopen(rq, timeout=5) as r:
        html = r.read().decode("utf-8", "replace")
    for k in ["v1.4", "reasoning", "xhigh / medium / low / none"]:
        print("    含 %-28s %s" % (k, k in html))

    print("[4] 对照组：ZCode 原始请求体直连上游 -> 应 400")
    st, b = direct(ZCODE_BODY)
    print("    直连 status=%s  %s" % (st, b.decode("utf-8", "replace")[:170].replace("\n", " ")))

    print("[5] 关键：同一请求体过 exe -> 应 200，reasoning_effort 保持 none")
    print("    等 %d 秒避开限流..." % GAP)
    time.sleep(GAP)
    st, b = req("/v1/chat/completions", "POST", ZCODE_BODY,
                headers={"Authorization": "Bearer sk-should-be-dropped"})
    txt = b.decode("utf-8", "replace")
    print("    中转 status=%s | SSE 事件 %d | [DONE] %s"
          % (st, txt.count("data:"), "[DONE]" in txt))
    with urllib.request.urlopen(BASE + "/api/logs", timeout=5) as r:
        last = json.loads(r.read().decode())["items"][-1]
    print("    日志 status:", last.get("s"), "| ms:", last.get("ms"))
    for c in (last.get("fix") or []):
        print("         ·", c)
    rewritten = any("reasoning_effort" in c for c in (last.get("fix") or []))
    print("    是否误改 reasoning_effort:", rewritten, "  <- 必须是 False")

    print("[6] 仅嵌套 reasoning:{effort:'medium'}（无顶层）-> 应提升")
    print("    等 %d 秒..." % GAP)
    time.sleep(GAP)
    st, b = req("/v1/chat/completions", "POST",
                {"model": MODEL, "reasoning": {"effort": "medium"},
                 "messages": ZCODE_BODY["messages"], "max_tokens": 64},
                headers={"Authorization": "Bearer sk-x"})
    print("    status=%s" % st)
    with urllib.request.urlopen(BASE + "/api/logs", timeout=5) as r:
        last = json.loads(r.read().decode())["items"][-1]
    for c in (last.get("fix") or []):
        print("         ·", c)

    print("[7] 本地拦截：/v1/messages -> 400 中文提示")
    st, b = req("/v1/messages", "POST", {"model": MODEL,
                                         "messages": [{"role": "user", "content": "hi"}]})
    print("    status=%s %s" % (st, b.decode("utf-8", "replace")[:110]))

    print("[8] 关闭")
    p.terminate()
    try:
        p.wait(timeout=10)
        print("    已退出")
    except Exception:
        p.kill()
        print("    强杀")
    print("=" * 100)
    return 0


if __name__ == "__main__":
    sys.exit(main())
