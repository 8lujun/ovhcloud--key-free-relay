# -*- coding: utf-8 -*-
"""验证 v1.4 中转：修正 v1.3 的两处错误处理。

v1.3 的问题（本轮实测发现）：
  1) 只摘了顶层 reasoning_effort / thinking，没管**嵌套的 reasoning 对象**；
     ZCode 正是发这个，上游对整条 reasoning 都是 400。
  2) 把 reasoning_effort="none" 改写成 "xhigh" 是错的 ——
     none 实测是合法的"关思考"（reasoning 0 字、1.2 秒），改写成 xhigh 反而加重。

本脚本验证 v1.4 的三条关键行为：
  A. ZCode 原始请求体（reasoning:{effort:none} + reasoning_effort:"none"）
     直连 400 -> 过中转 200，且 **reasoning_effort 保持 none 不被改写**
  B. 只带嵌套 reasoning（无顶层）时，effort 应被提升成顶层
  C. 非法值 high 仍应被改写成 xhigh

限流约每 30 秒 2 次，所以真打上游的用例之间隔 17 秒。
"""
import importlib.util
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PROXY = os.path.join(os.path.dirname(HERE), "ovh_free_proxy.py")
PY = sys.executable
PORT = 8798
BASE = "http://127.0.0.1:%d" % PORT
MODEL = "Qwen3.8-27B"
UPSTREAM = "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1/chat/completions"
GAP = 17

MSGS = [{"role": "system", "content": "你是助手"},
        {"role": "user", "content": "只回两个字：收到"}]


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
    """直连上游（不带鉴权头），用来做对照组。"""
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    data = json.dumps(body).encode()
    r = urllib.request.Request(UPSTREAM, data=data, method="POST",
                               headers={"Content-Type": "application/json",
                                        "Accept-Encoding": "identity"})
    try:
        with urllib.request.urlopen(r, timeout=timeout, context=ctx) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def load_mod():
    spec = importlib.util.spec_from_file_location("ofp", PROXY)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# ZCode 真实请求体（从用户的报错日志里还原）
ZCODE_BODY = {
    "model": MODEL,
    "messages": MSGS,
    "max_completion_tokens": 64,
    "reasoning": {"effort": "none"},
    "reasoning_effort": "none",
    "stream": True,
    "stream_options": {"include_usage": True},
    "thinking": {"type": "enabled"},
    "enable_thinking": True,
}


def main():
    print("=" * 100)
    print("v1.4 验证   %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 100)

    print("[0] 语法检查")
    rc = subprocess.run([PY, "-m", "py_compile", PROXY], capture_output=True)
    print("    py_compile ->", rc.returncode, rc.stderr.decode("utf-8", "replace")[:400])
    if rc.returncode:
        return 1

    m = load_mod()
    print("    VERSION =", m.VERSION)
    print("    REASONING_EFFORT_SUPPORTED =", m.REASONING_EFFORT_SUPPORTED)

    print()
    print("[1] fix_body 单测：ZCode 真实请求体")
    nb, ch = m.fix_body(json.dumps(ZCODE_BODY).encode())
    print("    改动 %d 处：" % len(ch))
    for c in ch:
        print("       -", c)
    after = json.loads(nb.decode())
    print("    结果顶层字段:", sorted(after.keys()))
    print("    reasoning 还在吗:", "reasoning" in after)
    print("    reasoning_effort 最终值 = %r  <- 必须仍是 'none'"
          % after.get("reasoning_effort"))

    print()
    print("[2] fix_body 单测：其余分支")
    branches = [
        ("仅嵌套 reasoning:{effort:'medium'}",
         {"model": MODEL, "messages": [], "reasoning": {"effort": "medium"}},
         "reasoning_effort"),
        ("嵌套 high + 顶层 high",
         {"model": MODEL, "messages": [],
          "reasoning": {"effort": "high"}, "reasoning_effort": "high"},
         "reasoning_effort"),
        ("嵌套 minimal（上游也拒）",
         {"model": MODEL, "messages": [], "reasoning": {"effort": "minimal"}},
         "reasoning_effort"),
        ("顶层 high 无嵌套",
         {"model": MODEL, "messages": [], "reasoning_effort": "high"},
         "reasoning_effort"),
        ("嵌套 reasoning 为字符串（畸形）",
         {"model": MODEL, "messages": [], "reasoning": "none"},
         "reasoning"),
        ("完全干净",
         {"model": MODEL, "messages": [{"role": "user", "content": "hi"}]},
         None),
    ]
    for label, body, key in branches:
        nb2, ch2 = m.fix_body(json.dumps(body).encode())
        got = json.loads(nb2.decode()).get("reasoning_effort") if key else "(无改动)"
        print("    %-34s 改动%d  最终 %s = %r" % (label, len(ch2), key or "", got))
        for c in ch2:
            print("         ·", c)

    print()
    print("[3] pretty_err 仍正常")
    for s in ['{"message":"feature \'extra arguments: {\\"reasoning\\":{\\"effort\\":\\"none\\"}}\' is not currently supported"}',
              '{"message":"API rate limit exceeded","request_id":"abc"}']:
        print("    ", repr(m.pretty_err(s))[:160])

    print()
    print("[4] 启动 v1.4（--no-check --no-open --strict-port）")
    p = subprocess.Popen([PY, PROXY, "--port", str(PORT), "--no-open",
                          "--no-check", "--strict-port"],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    ready = False
    t0 = time.time()
    while time.time() - t0 < 25:
        try:
            with urllib.request.urlopen(BASE + "/health", timeout=3) as r:
                if r.status == 200:
                    ready = True
                    break
        except Exception:
            time.sleep(0.4)
    print("    就绪:", ready, "%.1fs" % (time.time() - t0))
    if not ready:
        p.terminate()
        print(p.stdout.read().decode("utf-8", "replace")[-900:])
        return 1
    with urllib.request.urlopen(BASE + "/health", timeout=5) as r:
        print("    version:", json.loads(r.read().decode()).get("version"))

    print()
    print("[5] 本地拦截（不耗配额）：/v1/messages")
    st, b = req("/v1/messages", "POST", {"model": MODEL,
                                         "messages": [{"role": "user", "content": "hi"}]})
    print("    status=%s %s" % (st, b.decode("utf-8", "replace")[:120]))

    print()
    print("[6] 对照组：ZCode 原始请求体直连上游 -> 应 400")
    st, b = direct(ZCODE_BODY)
    print("    直连 status=%s  %s" % (st, b.decode("utf-8", "replace")[:200].replace("\n", " ")))

    print()
    print("[7] 关键验证 A：同一请求体过中转 -> 应 200，且 reasoning_effort 保持 none")
    print("    等 %d 秒避开限流..." % GAP)
    time.sleep(GAP)
    st, b = req("/v1/chat/completions", "POST", ZCODE_BODY,
                headers={"Authorization": "Bearer sk-should-be-dropped"})
    print("    中转 status=%s" % st)
    txt = b.decode("utf-8", "replace")
    print("    回包前 200 字:", txt[:200].replace("\n", " "))
    if st == 200:
        # 流式：数一下 SSE 事件
        print("    SSE 事件数:", txt.count("data:"), "| 含 [DONE]:", "[DONE]" in txt)
    with urllib.request.urlopen(BASE + "/api/logs", timeout=5) as r:
        last = json.loads(r.read().decode())["items"][-1]
    print("    日志 status:", last.get("s"), "| ms:", last.get("ms"))
    print("    日志 fix  :")
    for c in (last.get("fix") or []):
        print("         ·", c)
    print("    日志 err  :", repr(last.get("err"))[:180])

    print()
    print("[8] 验证 B：仅嵌套 reasoning:{effort:'medium'}（无顶层）-> 应提升为 medium")
    print("    等 %d 秒..." % GAP)
    time.sleep(GAP)
    st, b = req("/v1/chat/completions", "POST",
                {"model": MODEL, "messages": MSGS, "reasoning": {"effort": "medium"},
                 "max_tokens": 64},
                headers={"Authorization": "Bearer sk-x"})
    print("    status=%s" % st)
    with urllib.request.urlopen(BASE + "/api/logs", timeout=5) as r:
        last = json.loads(r.read().decode())["items"][-1]
    for c in (last.get("fix") or []):
        print("         ·", c)
    print("    err:", repr(last.get("err"))[:150])

    print()
    print("[9] 说明页文案已更新")
    rq = urllib.request.Request(BASE + "/", headers={"Accept": "text/html"})
    with urllib.request.urlopen(rq, timeout=5) as r:
        html = r.read().decode("utf-8", "replace")
    for k in ["reasoning", "xhigh / medium / low / none", "v%s" % m.VERSION]:
        print("    含 %-28s %s" % (k, k in html))

    p.terminate()
    try:
        p.wait(timeout=8)
    except Exception:
        p.kill()
    print()
    print("[10] 已停止")
    print("=" * 100)
    return 0


if __name__ == "__main__":
    sys.exit(main())
