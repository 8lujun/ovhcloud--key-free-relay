# -*- coding: utf-8 -*-
"""
OVHcloud AI Endpoints 免密钥本地中转
====================================

解决什么问题
------------
OVHcloud 的匿名通道本身不需要 key：

    POST https://oai.endpoints.kepler.ai.cloud.ovh.net/v1/chat/completions

但很多客户端（ChatBox、NextChat、Dify、LobeChat、Cherry Studio 等）会强制
要求填写 API Key，不填不让保存。而这些客户端一旦拿到你填的 key，就会自动
拼成 `Authorization: Bearer <你填的内容>` 发出去 —— 只要这个头里带了实际
内容，OVHcloud 网关就当你在用密钥登录，返回 403。

本程序在本机架一个极简中转，把客户端发来的 Authorization 头丢掉：

    客户端  --(key 随便填)-->  http://127.0.0.1:8787/v1
                                ↓  删掉 Authorization 头
                              https://oai.endpoints.kepler.ai.cloud.ovh.net/v1

依赖：无。Python 3.8+ 标准库即可，不需要装任何第三方包。

用法（源码运行）
----------------
    python ovh_free_proxy.py                  # 默认 127.0.0.1:8787，自动开说明页
    python ovh_free_proxy.py --port 8899      # 指定端口
    python ovh_free_proxy.py --host 0.0.0.0   # 局域网内其它设备也能用
    python ovh_free_proxy.py --no-open        # 不自动打开说明页
    python ovh_free_proxy.py --no-check       # 跳过启动自检
    python ovh_free_proxy.py --selftest       # 只跑一次链路自检后退出

用法（Docker）
--------------
    docker build -t ovh-free-relay .
    docker run -d --name ovh-free-relay -p 127.0.0.1:8787:8787 ovh-free-relay
    # 宿主机端口与容器端口不一致时，告诉程序该对外显示哪个地址：
    docker run -d -p 8899:8787 -e OVH_RELAY_PUBLIC_URL=http://127.0.0.1:8899 ovh-free-relay

环境变量（命令行参数优先，容器里通常只用环境变量）
--------------------------------------------------
    OVH_RELAY_HOST         等价于 --host
    OVH_RELAY_PORT         等价于 --port
    OVH_RELAY_PUBLIC_URL   等价于 --public-url，说明页上展示的接口地址

客户端里这样填
--------------
    API 地址 / Base URL :  http://127.0.0.1:8787/v1
    API Key            :  随便填，例如 sk-ovh      （中转会直接丢弃）
    模型名              :  Qwen3.8-27B

启动后浏览器会自动打开一个说明页，上面有本机实际端口、配置填法和运行状态。

支持流式（stream=true）与非流式，SSE 边收边转发。
"""

import argparse
import http.client
import http.server
import json
import os
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser

UPSTREAM_HOST = "oai.endpoints.kepler.ai.cloud.ovh.net"
UPSTREAM_BASE = "/v1"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
MODEL_NAME = "Qwen3.8-27B"
VERSION = "1.4"

# --- 环境变量配置（命令行参数优先，供 Docker / systemd 这类不便改 CMD 的场景）---
#   OVH_RELAY_HOST        监听地址，等价于 --host
#   OVH_RELAY_PORT        监听端口，等价于 --port
#   OVH_RELAY_PUBLIC_URL  说明页上展示给客户端的接口地址，等价于 --public-url
ENV_HOST = os.environ.get("OVH_RELAY_HOST", "").strip()
ENV_PORT = os.environ.get("OVH_RELAY_PORT", "").strip()
ENV_PUBLIC_URL = os.environ.get("OVH_RELAY_PUBLIC_URL", "").strip()

# 展示在说明页 / 横幅里的「客户端该填的地址」。
# 留空则按监听地址推断；容器里宿主机端口与容器端口不一致时（如 -p 8899:8787）
# 就靠 --public-url / OVH_RELAY_PUBLIC_URL 指到正确的地址上。
PUBLIC_URL = ""


def env_port_default() -> int:
    """把 OVH_RELAY_PORT 解析成端口号；不合法就回退到默认端口，不让容器起不来。"""
    if not ENV_PORT:
        return DEFAULT_PORT
    try:
        p = int(ENV_PORT)
    except ValueError:
        p = -1
    if not 0 < p < 65536:
        out("环境变量 OVH_RELAY_PORT=%r 不是 1-65535 的整数，已忽略，改用 %d。"
            % (ENV_PORT, DEFAULT_PORT))
        return DEFAULT_PORT
    return p


def normalize_public_url(raw: str) -> str:
    """把 --public-url / OVH_RELAY_PUBLIC_URL 收口成 .../v1 形式的 Base URL。

    Docker 里宿主机端口常与容器端口不同（-p 8899:8787），说明页按容器内端口
    渲染就会给出错地址，这时用这个参数显式指定；用户少写 http:// 或 /v1 也认。
    """
    u = (raw or "").strip().rstrip("/")
    if not u:
        return ""
    if "://" not in u:
        u = "http://" + u
    if not u.endswith("/v1"):
        u += "/v1"
    return u


# 只放行 /v1 下的路径，避免把本机变成开放代理
ALLOWED_PREFIXES = ("/v1/",)
ALLOWED_EXACT = ("/v1",)

# 已确认上游不存在的路径 -> 直接给出可读的提示，不白跑一趟还要吃配额
KNOWN_BAD = {
    "/v1/messages":
        "你用的是 Anthropic 协议（/v1/messages），OVHcloud 这条通道只认 OpenAI 协议。"
        "请到客户端的 Provider 设置里把「API 格式 / API 类型」改成 Chat Completions 再试。",
}

# 最近请求记录（环形），用于诊断
LOG_MAX = 20
_LOGS = []
_LOGS_LOCK = threading.Lock()

# 详细模式：把客户端发来的原始请求全部打到控制台
DEBUG = False

# 上游状态码的中文含义，直接写进日志，省得再去查
STATUS_HINT = {
    200: "",
    400: "上游拒绝了请求体（看下面「上游原文」，多为 tools / 消息结构 / 参数不合法）",
    401: "鉴权失败",
    403: "带上了 key，匿名层拒绝",
    404: "路径不对，或模型名不存在",
    429: "匿名层限流，等 30 秒再来",
    500: "上游内部错误",
    502: "连不上上游",
}

# 出错时最多记多少字符的请求体 / 上游原文，够定位又不撑爆页面
ERR_MAX = 600
BODY_MAX = 1500


# 实测被上游拒收的顶层字段（报 "feature 'extra arguments: ...' is not currently supported"）
#
# 注意 reasoning：上游对这个**嵌套对象**是整条拒收的，连 {effort:"xhigh"}、
# {enabled:false} 都 400（实测 2026-10-12）。所以它必须整体摘掉，
# 但摘之前要把里面的 effort 提升成顶层的 reasoning_effort，否则会把
# 客户端的"关思考"意图弄丢。
UNSUPPORTED_FIELDS = ("thinking", "enable_thinking", "user", "extra_body", "provider")

# 这个网关的推理强度取值。
#   官方报错只列出 xhigh (默认) / medium / low，
#   但实测 none 也是合法的：reasoning 字段 0 字、1.2 秒返回，等于关掉思考。
#   而 high / minimal 都会被 400，所以不能照搬 OpenAI 的取值。
REASONING_EFFORT_SUPPORTED = ("xhigh", "medium", "low", "none")

# 是否在转发前自动矫正参数（可用 --no-fix 关掉）
FIX = True


def fix_body(body):
    """把客户端发来的、上游不认的参数就地矫正。

    返回 (新的 body, 改动说明列表)。没有可改的就原样返回。
    依据全部来自本机实测，不是猜测。
    """
    if not body:
        return body, []
    try:
        jb = json.loads(body.decode("utf-8", "replace"))
    except Exception:
        return body, []
    if not isinstance(jb, dict):
        return body, []

    changes = []

    # 0) 嵌套的 reasoning 对象：上游整条拒收（实测连 {effort:"xhigh"} 都 400）。
    #    先把它里面的 effort 提升到顶层 reasoning_effort，再删掉整个对象——
    #    否则客户端"关思考"（effort=none）的意图会被一起扔掉。
    if "reasoning" in jb:
        nested = jb.pop("reasoning")
        hint = nested.get("effort") if isinstance(nested, dict) else None
        hoisted = ""
        if isinstance(hint, str) and hint.strip() and "reasoning_effort" not in jb:
            jb["reasoning_effort"] = hint.strip().lower()
            hoisted = "，其 effort=%r 已提升到 reasoning_effort" % (hint,)
        changes.append("已移除上游不支持的字段 reasoning（嵌套对象）%s" % hoisted)

    # 1) 推理强度：只认 xhigh / medium / low / none，其它值（含客户端常发的 high）归到默认
    if "reasoning_effort" in jb:
        v = jb["reasoning_effort"]
        if not isinstance(v, str) or v.strip().lower() not in REASONING_EFFORT_SUPPORTED:
            jb["reasoning_effort"] = "xhigh"
            changes.append("reasoning_effort: %r 不被支持，已改为 'xhigh'" % (v,))

    # 2) stream_options 只能跟 stream=true 成对出现
    if "stream_options" in jb and not jb.get("stream"):
        jb.pop("stream_options")
        changes.append("stream_options: 已移除（stream 不是 true）")

    # 3) 上游明确拒收的顶层字段
    for k in UNSUPPORTED_FIELDS:
        if k in jb:
            jb.pop(k)
            changes.append("已移除上游不支持的字段 %s" % k)

    # 4) metadata 的值必须全是字符串，否则上游 422
    md = jb.get("metadata")
    if isinstance(md, dict) and not all(isinstance(v, str) for v in md.values()):
        jb.pop("metadata")
        changes.append("metadata: 已移除（值必须是字符串）")

    if not changes:
        return body, []
    return json.dumps(jb, ensure_ascii=False).encode("utf-8"), changes


def pretty_err(text: str) -> str:
    """把上游套娃式的报错拆成人话。

    这个网关的报错长这样，message 里又是一层 JSON 字符串：

        {"message":"{\\"error\\":{\\"message\\":\\"Stream options can only be
         defined when `stream=True`. (parameter=stream_options)\\",...}}"}

    这里一路剥到最里面的那句，读者才看得懂。
    """
    s = (text or "").strip()
    for _ in range(4):
        try:
            obj = json.loads(s)
        except Exception:
            return s
        if not isinstance(obj, dict):
            return s
        if isinstance(obj.get("error"), dict):
            inner = obj["error"].get("message")
            if isinstance(inner, str) and inner.strip():
                return inner.strip()
        msg = obj.get("message")
        if isinstance(msg, str) and msg.strip():
            s = msg.strip()
            continue
        return s
    return s


def summarize_body(body):
    """把请求体摘成一份能一眼看出结构的摘要。"""
    info = {}
    if not body:
        return info
    try:
        jb = json.loads(body.decode("utf-8", "replace"))
    except Exception:
        info["解析"] = "不是合法 JSON"
        return info
    if not isinstance(jb, dict):
        info["解析"] = "顶层不是对象（是 %s）" % type(jb).__name__
        return info
    info["顶层字段"] = ", ".join(sorted(jb.keys()))
    msgs = jb.get("messages")
    if isinstance(msgs, list):
        roles = []
        for m in msgs:
            if isinstance(m, dict):
                c = m.get("content")
                ct = "数组" if isinstance(c, list) else ("空" if c in ("", None) else "文本")
                roles.append("%s/%s" % (m.get("role", "?"), ct))
        info["消息"] = " | ".join(roles[:8]) + ("…" if len(roles) > 8 else "")
    if isinstance(jb.get("tools"), list):
        info["tools"] = "%d 个" % len(jb["tools"])
    if "tool_choice" in jb:
        info["tool_choice"] = str(jb["tool_choice"])[:40]
    if "stream" in jb:
        info["stream"] = str(bool(jb["stream"]))
    return info


def log_add(rec: dict) -> None:
    with _LOGS_LOCK:
        _LOGS.append(rec)
        if len(_LOGS) > LOG_MAX:
            del _LOGS[0]


def log_snapshot():
    with _LOGS_LOCK:
        return list(_LOGS)


def normalize_v1(path: str) -> str:
    """把客户端各种写法统一收口成 /v1/...。

    客户端的 Base URL 填法五花八门，三种都会遇到：
        Base URL 填到根，路径拼 /chat/completions   -> 少了 /v1，要补
        Base URL 含 /v1，客户端又拼完整路径          -> 变成 /v1/v1/...，要折叠
        Base URL 含 /v1，路径只拼 /chat/completions  -> 本来就对，不动
    这里一次全处理掉，读者怎么填都能通。
    """
    if "?" in path:
        p, q = path.split("?", 1)
        q = "?" + q
    else:
        p, q = path, ""

    # 压掉重复斜杠
    while "//" in p:
        p = p.replace("//", "/")

    # 补上缺失的 /v1 前缀
    if not p.startswith("/v1"):
        p = "/v1" + p

    # 折叠重复的 /v1 前缀
    while p.startswith("/v1/v1"):
        p = p[len("/v1"):]

    return p + q

CTX = ssl.create_default_context()

# 这些头由本程序自己决定，不转发给上游
DROP_HEADERS = {
    "authorization", "proxy-authorization", "cookie",
    "host", "content-length", "connection", "keep-alive",
    "transfer-encoding", "accept-encoding", "upgrade",
    "te", "trailer", "expect",
}

_STATE = {"started": time.time(), "ok": 0, "fail": 0, "upstream": "unknown"}
_STATE_LOCK = threading.Lock()


# ------------------------------------------------------------------ 说明页
PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OVHcloud 免密钥中转 · 已启动</title>
<style>
  :root{--primary:#3180df;--accent:#1c4e8e;--soft:#eef5fc;
        --border:#e3eaf2;--text:#2f3437;--sub:#5f666d}
  *{box-sizing:border-box}
  body{margin:0;background:#f7fafd;color:var(--text);
       font:16px/1.75 "Microsoft YaHei","PingFang SC",system-ui,sans-serif}
  .wrap{max-width:840px;margin:0 auto;padding:46px 22px 72px}
  .badge{display:inline-block;padding:5px 15px;border-radius:999px;
         background:var(--primary);color:#fff;font-size:13px;font-weight:700;
         letter-spacing:.6px}
  h1{font-size:34px;line-height:1.32;color:var(--accent);margin:16px 0 10px;
     letter-spacing:-.4px}
  .lead{margin:0 0 30px;color:var(--sub)}
  .card{background:#fff;border:1px solid var(--border);border-radius:16px;
        padding:26px 28px;margin-bottom:18px}
  h2{font-size:19px;color:var(--accent);margin:0 0 16px}
  table{width:100%;border-collapse:collapse}
  th{text-align:left;font-weight:600;color:var(--sub);font-size:14px;
     width:200px;padding:14px 0;vertical-align:top;
     border-bottom:1px solid var(--border)}
  td{padding:14px 0;border-bottom:1px solid var(--border);vertical-align:top}
  tr:last-child th,tr:last-child td{border-bottom:none}
  code{font-family:Consolas,Menlo,monospace;background:var(--soft);
       color:var(--accent);padding:3px 9px;border-radius:6px;font-size:14px;
       word-break:break-all}
  button{border:1px solid var(--border);background:#fff;color:var(--accent);
         border-radius:8px;padding:4px 13px;font-size:13px;cursor:pointer;
         font-family:inherit;margin-left:8px;transition:background .15s}
  button:hover{background:var(--soft)}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(148px,1fr));
        gap:12px}
  .stat{background:var(--soft);border-radius:12px;padding:15px 17px}
  .stat .k{font-size:13px;color:var(--sub);margin-bottom:5px}
  .stat .v{font-size:22px;font-weight:700;color:var(--accent);line-height:1.3}
  .flow{display:flex;align-items:center;gap:11px;flex-wrap:wrap;font-size:15px}
  .flow .m{color:var(--sub)}
  .flow b{background:var(--soft);color:var(--accent);padding:6px 14px;
          border-radius:10px;border:1px solid var(--border)}
  .arrow{color:var(--primary);font-weight:700}
  .note{margin:16px 0 0;font-size:14px;color:var(--sub)}
  .logs{font:13px/1.7 Consolas,Menlo,monospace}
  .logitem{border-bottom:1px dashed var(--border);padding:2px 0 6px}
  .logitem:last-child{border-bottom:none}
  .logrow{display:flex;gap:10px;padding:7px 0;align-items:baseline}
  .logrow .tm{color:var(--sub);font-size:12px}
  .logrow .st{font-weight:700;min-width:34px}
  .logrow .st.ok{color:#1a7f4b}
  .logrow .st.bad{color:#c0392b}
  .logrow .pth{color:var(--accent);flex:1;word-break:break-all}
  .logrow .nt{color:var(--sub);font-size:12px}
  .er{font-size:12px;line-height:1.65;color:#9c3b22;background:#fdf4f0;
      border-left:3px solid #d3836a;border-radius:0 8px 8px 0;
      padding:7px 11px;margin:2px 0 4px 44px;white-space:pre-wrap;
      word-break:break-all}
  .er b{color:#8a2f18}
  .rq{font-size:12px;color:var(--sub);margin:0 0 4px 44px;
      word-break:break-all}
  .fx{font-size:12px;line-height:1.65;color:#1a6b4a;background:#f1fbf6;
      border-left:3px solid #66b997;border-radius:0 8px 8px 0;
      padding:7px 11px;margin:2px 0 4px 44px;word-break:break-all}
  .fx b{color:#155c3e}
  .fp{font-size:13px;color:#98a2ad;margin-top:26px;text-align:center}
</style>
</head>
<body>
<div class="wrap">
  <span class="badge">中转已启动</span>
  <h1>OVHcloud 免密钥中转</h1>
  <p class="lead">正在这台电脑上运行。它把客户端请求里的 key 删掉，再转发给 OVHcloud 的匿名通道。</p>

  <div class="card">
    <h2>在客户端里这样填</h2>
    <table>
      <tr><th>接口地址（Base URL）</th>
          <td><code>__URL__</code><button onclick="cp('__URL__',this)">复制</button></td></tr>
      <tr><th>模型名</th>
          <td><code>__MODEL__</code><button onclick="cp('__MODEL__',this)">复制</button></td></tr>
      <tr><th>API Key</th>
          <td>随便填，比如 <code>sk-ovh</code>。中转会直接丢弃，绝不发给上游。</td></tr>
    </table>
  </div>

  <div class="card">
    <h2>运行状态</h2>
    <div class="grid">
      <div class="stat"><div class="k">上游通道</div><div class="v" id="up">检测中…</div></div>
      <div class="stat"><div class="k">已转发成功</div><div class="v" id="ok">0</div></div>
      <div class="stat"><div class="k">上游报错</div><div class="v" id="bad">0</div></div>
      <div class="stat"><div class="k">已运行</div><div class="v" id="upt">—</div></div>
    </div>
    <p class="note">上游报错看下面那张「最近请求」表：<b>429</b> 是限流（约每 30 秒放行 2 次），<b>403</b> 是 key 没被剥干净，<b>404</b> 是地址或模型名不对，<b>400</b> 是请求体被上游拒绝。凡是非 200，表里都会附上<b>上游返回的原文</b>和<b>请求体摘要</b>，照着改就行。</p>
  </div>

  <div class="card">
    <h2>数据怎么走的</h2>
    <div class="flow">
      <span class="m">客户端（key 随便填）</span><span class="arrow">→</span>
      <b>本机中转 · 删掉 Authorization 头</b><span class="arrow">→</span>
      <span class="m">OVHcloud 匿名通道</span>
    </div>
    <p class="note">中转不绕过限流（限流按你的上网 IP 计），它只解决客户端强制填 key 这一件事。本程序不上传任何数据，也不会保存你的对话内容。</p>
    <p class="note">转发前还会做三件事：把客户端塞的<b>上游不支持的字段</b>摘掉（如 thinking / enable_thinking / provider / <b>reasoning</b> 这个嵌套对象），把<b>推理强度</b>归到上游认的值（只认 xhigh / medium / low / none），以及把<b>重复的 /v1</b> 折叠掉。改了什么会写在下面那张表里，用 <code>--no-fix</code> 可关闭。</p>
  </div>

  <div class="card">
    <h2>最近请求（客户端发来什么，这里就记什么）</h2>
    <div id="logs" class="logs"><p class="note">还没有请求。客户端连上来之后，这里会逐条出现。</p></div>
    <p class="note">出错的行下面会跟两块内容：<b>请求体</b>（客户端发来的字段与消息结构）和<b>上游原文</b>（OVHcloud 返回的原始报错）。
      排错时先看上游原文，它直接说了哪里不对；再看请求体，对着改客户端。整屏发给客服/群里，一眼能定位。</p>
  </div>

  <p class="fp">ovh-free-proxy v__VERSION__ · 仅供本机使用 · 关闭这个窗口即可停止</p>
</div>
<script>
function cp(t, el){
  function done(){var o = el.textContent; el.textContent = '已复制';
    setTimeout(function(){el.textContent = o}, 1200)}
  function fb(){var i = document.createElement('textarea'); i.value = t;
    document.body.appendChild(i); i.select();
    try{document.execCommand('copy'); done()}catch(e){}
    document.body.removeChild(i)}
  if (navigator.clipboard && navigator.clipboard.writeText){
    navigator.clipboard.writeText(t).then(done, fb);
  } else { fb(); }
}
function tick(){
  fetch('/api/stats').then(function(r){return r.json()}).then(function(s){
    document.getElementById('up').textContent =
      s.upstream === 'ok' ? '正常' : (s.upstream === 'fail' ? '不通' : '未检测');
    document.getElementById('ok').textContent = s.ok;
    document.getElementById('bad').textContent = s.fail;
    document.getElementById('upt').textContent = s.uptime;
  }).catch(function(){});
  tickLogs();
}
function esc(t){return String(t).replace(/[&<>"]/g, function(c){
  return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
function tickLogs(){
  fetch('/api/logs').then(function(r){return r.json()}).then(function(d){
    if(!d.items || !d.items.length){return;}
    var h = '';
    for (var i = d.items.length - 1; i >= 0; i--){
      var it = d.items[i];
      var cls = (it.s === 200) ? 'ok' : 'bad';
      var label = it.m + ' ' + it.p;
      if (it.model && it.model !== '?') { label += '  [' + it.model + ']'; }
      h += '<div class="logitem">'
        + '<div class="logrow">'
        + '<span class="tm">' + esc(it.t) + '</span>'
        + '<span class="st ' + cls + '">' + esc(it.s) + '</span>'
        + '<span class="pth">' + esc(label) + '</span>'
        + '<span class="nt">' + (it.ms ? it.ms + 'ms ' : '') + esc(it.note || '') + '</span>'
        + '</div>';
      if (it.req) {
        var parts = [];
        for (var k in it.req) { parts.push(k + ': ' + it.req[k]); }
        h += '<div class="rq">请求体 ' + esc(parts.join(' · ')) + '</div>';
      }
      if (it.fix && it.fix.length) {
        h += '<div class="fx"><b>已自动矫正</b>　' + esc(it.fix.join('；')) + '</div>';
      }
      if (it.err) {
        h += '<div class="er"><b>上游原文</b>　' + esc(it.err) + '</div>';
      }
      h += '</div>';
    }
    document.getElementById('logs').innerHTML = h;
  }).catch(function(){});
}
tick(); setInterval(tick, 2000);
</script>
</body>
</html>
"""


def render_page(port: int) -> str:
    url = PUBLIC_URL or "http://127.0.0.1:%d/v1" % port
    return (PAGE.replace("__URL__", url)
                .replace("__MODEL__", MODEL_NAME)
                .replace("__VERSION__", VERSION))


# ------------------------------------------------------------------ 工具
def find_free_port(host: str, start: int, tries: int = 20):
    """从 start 开始找一个能绑上的端口。"""
    for i in range(tries):
        p = start + i
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind((host, p))
            return p
        except OSError:
            continue
        finally:
            s.close()
    return None


def set_console_title(text: str) -> None:
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleTitleW(text)
    except Exception:
        pass


def out(msg: str = "") -> None:
    try:
        sys.stdout.write(msg + "\n")
        sys.stdout.flush()
    except Exception:
        pass


def human_uptime(sec: float) -> str:
    sec = int(sec)
    if sec < 60:
        return "%d 秒" % sec
    if sec < 3600:
        return "%d 分钟" % (sec // 60)
    return "%d 小时 %d 分" % (sec // 3600, (sec % 3600) // 60)


# ------------------------------------------------------------------ 请求处理
class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ovh-free-proxy/%s" % VERSION

    # 自带日志关掉，自己打更短的一行
    def log_message(self, fmt, *args):
        pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _send_bytes(self, code, payload: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self._cors()
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_json(self, code, obj) -> None:
        self._send_bytes(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                         "application/json; charset=utf-8")

    def _send_html(self, text: str) -> None:
        self._send_bytes(200, text.encode("utf-8"), "text/html; charset=utf-8")

    # ---------- 预检 ----------
    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---------- 路由 ----------
    def do_GET(self):
        path = self.path.split("?", 1)[0]

        if path in ("/", "/ui", "/index.html"):
            accept = self.headers.get("Accept", "")
            if path != "/" or "text/html" in accept:
                self._send_html(render_page(self.server.server_address[1]))
            else:
                self._send_json(200, self._health())
            return

        if path == "/health":
            self._send_json(200, self._health())
            return

        if path == "/api/stats":
            with _STATE_LOCK:
                snap = dict(_STATE)
            self._send_json(200, {
                "upstream": snap["upstream"],
                "ok": snap["ok"],
                "fail": snap["fail"],
                "uptime": human_uptime(time.time() - snap["started"]),
                "version": VERSION,
            })
            return

        if path == "/api/logs":
            self._send_json(200, {"items": log_snapshot()})
            return

        self._relay(path)

    def do_POST(self):
        self._relay(self.path.split("?", 1)[0])

    def _health(self):
        with _STATE_LOCK:
            snap = dict(_STATE)
        return {
            "ok": True,
            "service": "ovh-free-proxy",
            "version": VERSION,
            "port": self.server.server_address[1],
            "upstream": "https://%s%s" % (UPSTREAM_HOST, UPSTREAM_BASE),
            "upstream_state": snap["upstream"],
            "note": "Authorization 头已被剥掉，以匿名身份转发",
        }

    # ---------- 转发 ----------
    def _relay(self, raw_path: str):
        method = self.command
        ua = (self.headers.get("User-Agent") or "")[:44]

        # 1) 路径归一化：折叠客户端重复拼出的 /v1
        path = normalize_v1(raw_path)
        if path != raw_path:
            out("       路径归一化：%s -> %s" % (raw_path, path))

        # 2) 已知走不通的协议，直接给可读提示，不白白消耗上游配额
        if path in KNOWN_BAD:
            msg = KNOWN_BAD[path]
            log_add({"t": time.strftime("%H:%M:%S"), "m": method, "p": raw_path,
                     "s": 400, "ms": 0, "ua": ua,
                     "note": "协议不匹配（Anthropic 格式）"})
            self._mark(fail=True)
            out("[%s]  %-4s %-30s  拒绝  %s" % (time.strftime("%H:%M:%S"), method, raw_path, msg[:40]))
            self._send_json(400, {"error": {"type": "invalid_request_error",
                                            "code": "wrong_api_style",
                                            "message": msg}})
            return

        if not (path in ALLOWED_EXACT or path.startswith(ALLOWED_PREFIXES)):
            log_add({"t": time.strftime("%H:%M:%S"), "m": method, "p": raw_path,
                     "s": 403, "ms": 0, "ua": ua, "note": "路径不在 /v1 下，已拦截"})
            self._send_json(403, {"error": "path not allowed: %s" % raw_path})
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        body = self.rfile.read(length) if length > 0 else None

        # 参数矫正：上游不认的字段/取值，转发前就地改掉
        fix_log = []
        if body and FIX:
            new_body, changes = fix_body(body)
            if changes:
                body = new_body
                fix_log = changes
                for c in changes:
                    out("        参数矫正   : %s" % c)

        auth = self.headers.get("Authorization") or ""
        dropped_key = bool(auth.strip().split(" ", 1)[-1].strip()) if auth else False

        model_name = "?"
        if body:
            try:
                jb = json.loads(body.decode("utf-8", "replace"))
                model_name = (jb.get("model") if isinstance(jb, dict) else None) or "?"
            except Exception:
                model_name = "(解析失败)"

        req_info = summarize_body(body)

        if DEBUG:
            out("  ---- 收到 %s %s" % (method, raw_path))
            out("       User-Agent : %s" % (ua or "(空)"))
            out("       Auth 头    : %s" % (("已剥掉: " + auth[:24] + ("…" if len(auth) > 24 else "")) if auth else "(未发送)"))
            out("       model      : %r" % model_name)
            if body:
                out("       body       : %s" % body[:600].decode("utf-8", "replace").replace("\n", " "))

        fwd = {k: v for k, v in self.headers.items()
               if k.lower() not in DROP_HEADERS}
        fwd["Accept-Encoding"] = "identity"
        if body is not None:
            fwd["Content-Length"] = str(len(body))

        t0 = time.time()
        try:
            conn = http.client.HTTPSConnection(
                UPSTREAM_HOST, 443, timeout=600, context=CTX)
            conn.request(method, path, body=body, headers=fwd)
            resp = conn.getresponse()
        except Exception as exc:
            self._mark(fail=True)
            out("[%s]  %-4s %-34s  连接上游失败: %s"
                % (time.strftime("%H:%M:%S"), method, path,
                   type(exc).__name__))
            log_add({"t": time.strftime("%H:%M:%S"), "m": method, "p": path,
                     "s": 502, "ms": int((time.time() - t0) * 1000), "ua": ua,
                     "model": model_name, "note": "连不上上游"})
            self._send_json(502, {"error": "无法连接上游 %s: %s: %s"
                                          % (UPSTREAM_HOST, type(exc).__name__, exc)})
            return

        ctype = resp.getheader("Content-Type", "") or ""
        is_stream = "event-stream" in ctype.lower()

        # 出错时先把上游原文读出来 —— 这是定位问题的关键，绝不能丢
        err_text = ""
        err_body = None
        if resp.status >= 400 and not is_stream:
            try:
                err_body = resp.read()
            except Exception:
                err_body = b""
            err_text = pretty_err(err_body[:ERR_MAX * 4].decode("utf-8", "replace"))[:ERR_MAX]

        self._mark(fail=resp.status >= 400)
        out("[%s]  %-4s %-34s  %3d  %.2fs%s"
            % (time.strftime("%H:%M:%S"), method, path, resp.status,
               time.time() - t0, "  (流式)" if is_stream else ""))
        if err_text:
            out("        上游原文 : %s" % err_text[:300].replace("\n", " "))
        if resp.status >= 400 and req_info:
            out("        请求体   : %s" % json.dumps(req_info, ensure_ascii=False)[:300])

        note = STATUS_HINT.get(resp.status, "")
        if path != raw_path:
            note = ("[已归一化] " + note).strip()

        rec = {"t": time.strftime("%H:%M:%S"), "m": method, "p": raw_path,
               "s": resp.status, "ms": int((time.time() - t0) * 1000),
               "ua": ua, "model": model_name,
               "dropped": dropped_key, "stream": is_stream,
               "note": note}
        if err_text:
            rec["err"] = err_text
        if req_info:
            rec["req"] = req_info
            if resp.status >= 400 and body:
                rec["body"] = body[:BODY_MAX].decode("utf-8", "replace")
        if fix_log:
            rec["fix"] = fix_log
        log_add(rec)

        self.send_response(resp.status)
        self.send_header("Content-Type", ctype or "application/json")
        for k, v in resp.getheaders():
            if k.lower() in ("content-type", "content-length", "transfer-encoding",
                             "connection", "content-encoding"):
                continue
            self.send_header(k, v)
        self._cors()

        if is_stream:
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            try:
                while True:
                    chunk = resp.read1(4096)
                    if not chunk:
                        break
                    self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            data = err_body if err_body is not None else resp.read()
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

        try:
            conn.close()
        except Exception:
            pass

    @staticmethod
    def _mark(fail: bool):
        with _STATE_LOCK:
            if fail:
                _STATE["fail"] += 1
            else:
                _STATE["ok"] += 1


class ThreadingServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# ------------------------------------------------------------------ 自检
def selftest(port: int, timeout: int = 30):
    """经本机中转发一次请求，验证整条链路。返回 (是否通过, 说明)。"""
    payload = json.dumps({
        "model": MODEL_NAME,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 8,
        "stream": False,
    }).encode("utf-8")
    req = urllib.request.Request(
        "http://127.0.0.1:%d/v1/chat/completions" % port,
        data=payload, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer sk-ovh-selftest"})   # 故意带个假 key
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            dt = time.time() - t0
            r.read()
            with _STATE_LOCK:
                _STATE["upstream"] = "ok"
            return True, "通过（HTTP %d，%.2f 秒，假 key 已被剥掉）" % (r.status, dt)
    except urllib.error.HTTPError as e:
        det = e.read()[:120].decode("utf-8", "replace").replace("\n", " ")
        ok = False
        if e.code == 429:
            return True, "通过（HTTP 429 —— 匿名层限流，说明链路是通的，等半分钟即可）"
        with _STATE_LOCK:
            _STATE["upstream"] = "fail"
        return ok, "失败（HTTP %d：%s）" % (e.code, det)
    except Exception as e:
        with _STATE_LOCK:
            _STATE["upstream"] = "fail"
        return False, "失败（%s: %s）" % (type(e).__name__, str(e)[:80])


# ------------------------------------------------------------------ 启动
def banner(host, port, url, show_ok=None) -> str:
    line = "=" * 62
    parts = [
        line,
        "  OVHcloud 免密钥中转  v%s" % VERSION,
        line,
        "  本机地址   :  %s" % url,
    ]
    if PUBLIC_URL and PUBLIC_URL != url:
        parts.append("  对外地址   :  %s  （--public-url 指定，客户端填这个）" % PUBLIC_URL)
    parts += [
        "  转发目标   :  https://%s/v1" % UPSTREAM_HOST,
        "  已丢弃请求头:  Authorization / Cookie",
        "",
        "  客户端里这样填：",
        "      接口地址  ->  %s" % url,
        "      API Key   ->  随便填，例如 sk-ovh",
        "      模型名    ->  %s" % MODEL_NAME,
        "",
        "  三个常见坑（全部已由本中转自动处理）：",
        "      1) ZCode 一类客户端要把「API 格式」选成 Chat Completions，",
        "         若选成 Anthropic，请求会发到 /v1/messages，上游不认。",
        "      2) 接口地址末尾带不带 /v1 都行，本中转会自动纠正重复的 /v1。",
        "      3) 上游会对整个 reasoning 对象报 400（连 {effort:xhigh} 都拒），",
        "         本中转会摘掉它，并把里面的 effort 提升成顶层的 reasoning_effort。",
        "         取值只认 xhigh / medium / low / none，high、minimal 会被 400。",
        "         （装 --no-fix 可关闭自动矫正，用来看上游原始报错）",
        "",
        "  说明页     :  %s" % ("http://%s:%d/" % (host, port)),
    ]
    if show_ok is not None:
        parts.append("  链路自检   :  %s" % ("✓ " if show_ok[0] else "× ") + show_ok[1])
    parts += ["", "  关闭这个窗口（或按 Ctrl+C）即可停止", line]
    return "\n".join(parts)


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    ap = argparse.ArgumentParser(
        description="OVHcloud 免密钥通道的本地中转（剥掉客户端强填的 Authorization 头）")
    ap.add_argument("--host", default=ENV_HOST or DEFAULT_HOST,
                    help="监听地址，默认 127.0.0.1（环境变量 OVH_RELAY_HOST）；"
                         "填 0.0.0.0 可让局域网其它设备使用")
    ap.add_argument("--port", type=int, default=env_port_default(),
                    help="监听端口，默认 8787（环境变量 OVH_RELAY_PORT）；"
                         "被占用时自动往后找")
    ap.add_argument("--public-url", default=ENV_PUBLIC_URL,
                    help="说明页/横幅里展示的接口地址，默认按监听地址推断"
                         "（环境变量 OVH_RELAY_PUBLIC_URL）；容器里宿主机端口与"
                         "容器端口不一致时用它，例如 http://192.168.1.10:8899")
    ap.add_argument("--strict-port", action="store_true",
                    help="端口被占用时直接报错退出，不自动换端口")
    ap.add_argument("--no-open", action="store_true", help="不自动打开说明页")
    ap.add_argument("--no-check", action="store_true", help="跳过启动时的链路自检")
    ap.add_argument("--debug", action="store_true",
                    help="诊断模式：把客户端发来的原始请求（路径/请求头/body）全部打出来")
    ap.add_argument("--no-fix", action="store_true",
                    help="关闭参数自动矫正（默认开启：会摘掉上游不支持的字段、修正推理强度等）")
    ap.add_argument("--selftest", action="store_true",
                    help="只跑一次链路自检然后退出（用于确认程序可用）")
    args = ap.parse_args(argv)

    global DEBUG
    DEBUG = bool(args.debug)

    global FIX
    FIX = not args.no_fix

    global PUBLIC_URL
    PUBLIC_URL = normalize_public_url(args.public_url)

    host = args.host or DEFAULT_HOST
    display_host = "127.0.0.1" if host in ("0.0.0.0", "") else host

    if args.strict_port:
        port = args.port
    else:
        port = find_free_port(host, args.port)
        if port is None:
            out("端口 %d 起连续 20 个都被占用，请用 --port 指定其它端口。" % args.port)
            return 1

    try:
        srv = ThreadingServer((host, port), Handler)
    except OSError as exc:
        out("启动失败：%s" % exc)
        out("换个端口试试：--port 8899")
        return 1

    url = "http://%s:%d/v1" % (display_host, port)

    # --- 只要自检 ---
    if args.selftest:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        out("正在自检：经本机中转发一次请求到上游…")
        ok, msg = selftest(port)
        out("自检结果：" + ("通过" if ok else "失败") + "  " + msg)
        if port != args.port:
            out("（端口 %d 被占用，本次实际使用 %d）" % (args.port, port))
        srv.shutdown()
        return 0 if ok else 1

    # --- 正常启动 ---
    if port != args.port:
        out("提示：端口 %d 被占用，已自动改用 %d。" % (args.port, port))

    check_result = None
    if not args.no_check:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        ok, msg = selftest(port)
        check_result = (ok, msg)
        srv.shutdown()
        # shutdown 后需重建 server，避免地址被占用的窗口期
        srv = ThreadingServer((host, port), Handler)

    set_console_title("OVHcloud 免密钥中转 · %s" % url)
    out(banner(display_host, port, url, check_result))

    if not args.no_open:
        threading.Timer(0.8, lambda: webbrowser.open("http://%s:%d/" % (display_host, port))).start()

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        out("\n已停止。")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
