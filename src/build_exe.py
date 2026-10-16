# -*- coding: utf-8 -*-
"""把 ovh_free_proxy.py 打包成 Windows 单文件 exe。

用法（必须用带 PyInstaller 的解释器）：
    C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe build_exe.py

产物：
    06_code/dist/OVH免密钥中转.exe
    06_code/dist/使用说明.txt
    06_code/dist/OVH免密钥中转-v1.4.zip   （exe + 说明，方便分发）
"""
import os
import shutil
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
CODE_DIR = os.path.dirname(HERE)
SRC = os.path.join(CODE_DIR, "ovh_free_proxy.py")
DIST = os.path.join(CODE_DIR, "dist")
WORK = os.path.join(HERE, "_build")
ICON = os.path.join(HERE, "icon.ico")
EXE_NAME = "OVH免密钥中转"
VERSION = "1.4"


# ------------------------------------------------------------------ 图标
def make_icon(path: str) -> str:
    """用品牌蓝画一个圆角方块 + 双向箭头。"""
    from PIL import Image, ImageDraw

    S = 256
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    d.rounded_rectangle([6, 6, S - 6, S - 6], radius=58,
                        fill=(49, 128, 223, 255))
    W = 16
    WHITE = (255, 255, 255, 255)

    # 上：向右的箭头
    d.line([(72, 100), (172, 100)], fill=WHITE, width=W)
    d.polygon([(198, 100), (170, 82), (170, 118)], fill=WHITE)
    # 下：向左的箭头
    d.line([(184, 156), (84, 156)], fill=WHITE, width=W)
    d.polygon([(58, 156), (86, 138), (86, 174)], fill=WHITE)

    img.save(path, format="ICO",
             sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
    return path


# ------------------------------------------------------------------ 说明
README = """OVHcloud 免密钥中转  v__VER__
============================================

它是什么
--------
让那些「必须填 API Key 才让保存」的客户端，也能用上 OVHcloud 的免密钥
免费大模型通道（Qwen3.8-27B）。程序在本机把客户端请求里的 key 删掉，
再转发给 OVHcloud。

用法（三步）
------------
1. 双击  __EXE__
2. 浏览器会自动打开一个说明页（也可手动访问 http://127.0.0.1:8787/）
3. 在客户端里按说明页填：

       接口地址  ->  http://127.0.0.1:8787/v1
       API Key   ->  随便填，比如 sk-ovh
       模型名    ->  Qwen3.8-27B

   然后正常跟模型对话就行。关掉那个黑色窗口即停止。

命令行参数（可选，命令行里运行 exe 时用）
----------------------------------------
  --port 8899      换端口（默认 8787；被占用会自动往后找）
  --host 0.0.0.0   让手机/平板等局域网设备也能用
                   （这种情况下，其它设备把 127.0.0.1 换成这台电脑的局域网 IP）
  --no-open        启动时不自动打开说明页
  --no-check       跳过启动时的链路自检
  --debug          诊断模式：把客户端发来的原始请求（路径/请求头/正文）
                   全部打到控制台。配不上时用它，一看就知道错在哪
  --no-fix         关闭参数自动矫正（默认开启，见下面「v1.4 新增」）
  --selftest       只跑一次自检就退出，用来确认程序能不能用

为什么会有 v1.4（本次实测新增）
-------------------------------
读者反馈：客户端报「Provider rejected the model request.」，说明页「最近请求」
表里对应的是一条 400，上游原文是：

  feature 'extra arguments: {"reasoning":{"effort":"none"}}' is not currently supported

真凶是 ZCode 发的一个**嵌套对象** reasoning。实测四条硬结论：

1) reasoning 对象整条被拒 —— 不是取值问题
   实测 {"effort":"xhigh"}、{"effort":"none"}、{"enabled":false} 全部 400。
   所以只能整体摘掉，不能"改个值再发"。
   -> v1.4 摘掉它之前，会先把里面的 effort 提升到顶层的 reasoning_effort，
      免得把客户端的"关思考"意图一起扔掉。

2) reasoning_effort = "none" 是合法的（官方报错里没写，但实测有效）
   实测同一道题：
      不填                -> reasoning 247 字
      reasoning_effort=none   -> reasoning   0 字，1.2 秒返回  <- 真的关掉了
      reasoning_effort=high   -> 400
      reasoning_effort=minimal-> 400
   -> v1.4 只把**真正非法**的值（high / minimal 等）改成 xhigh，
      none 原样保留。v1.3 会把 none 也改成 xhigh，等于把省电模式强行拧到最重档，
      是错的，这里修掉。

3) 上游不认的其它顶层字段（会 400）
   thinking / enable_thinking / user / extra_body / provider。
   metadata 也认，但值必须全是字符串，否则 422。
   -> v1.4 会把这些字段摘掉。

4) stream_options 必须和 stream=true 成对出现，否则 400。
   -> v1.4 在 stream 不是 true 时会移除 stream_options。

改了什么，说明页的「最近请求」表里会用绿色标出来。想自己看的，用 --no-fix
关掉自动矫正，让上游把原始报错吐出来。

状态码对照（实测，2026-10-12）
    200  正常
    400  上游拒绝了请求体。原因见上面「为什么会有 v1.4」。
         出错的请求，表里会附「上游原文」和「请求体摘要」，照着改就行
    403  请求里带了 key（中转正常情况下不会出现）
    404  地址不对 或 模型名不存在。两种典型：
           少了 /v1       -> /chat/completions
           多了一层 /v1   -> /v1/v1/chat/completions
         模型名写错或前后带空格也会 404（model_not_found）
    422  请求体字段类型不对（如 metadata 的值给了数字）
    429  匿名层限流，等 30 秒

ZCode 这类客户端特别注意
------------------------
- 新建自定义 Provider 时，「API 格式 / API 类型」必须选 Chat Completions。
  选成 Anthropic 会往 /v1/messages 发，OVHcloud 这条通道不认，直接 404。
- 接口地址末尾带不带 /v1 都可以，本程序会自动纠正重复的 /v1。
- API Key 栏必填，填一个空格即可（比如就敲一下空格）。
- 报 400 时，先看说明页「最近请求」表里的「上游原文」——它就是答案。
  上面四种原因 v1.4 已自动处理。

客户端报错配不上？先看这两个地方
--------------------------------
1) 说明页下半部分的「最近请求」表。每条都记着：客户端发了什么、拿到什么
   状态码；凡是非 200，还会附两块内容 ——
       「请求体」  客户端发来的字段与消息结构
       「上游原文」OVHcloud 返回的原始报错（已剥掉套娃的 JSON）
   先看上游原文，它直接说了哪里不对；再看请求体，对着改客户端。
2) 控制台（加 --debug 启动时会打得更详细，连请求头都打）。

常见问题
--------
Q: 双击后提示「Windows 已保护你的电脑」或杀软报毒？
A: 这是 PyInstaller 打包的未签名程序的常见误报。点「更多信息」→「仍要运行」；
   或把 exe 加进杀软白名单。本程序是纯本地脚本，只访问 ovhcloud 一个域名。

Q: 客户端报「连接失败 / Provider rejected the model request」？
A: 这类提示是客户端的万能文案，服务端返回任何非 200 都会显示成它。
   打开说明页，看「最近请求」表里那条的状态码，以及下面的「上游原文」，
   它会直接告诉你原因。最常见的 400 原因见上面「为什么会有 v1.4」，
   v1.3 都已自动处理；装了 v1.3 之后还报，就把那一整屏发出来。

Q: 报 429 ？
A: 匿名层限流很低，约每 30 秒只放行 2 次。等半分钟再试。
   注意：中转不绕过限流（限流按你的上网 IP 算），它只解决 key 这一栏。

Q: 报 403 ？
A: 说明请求里的 key 没被剥干净。确认客户端里填的接口地址是
   http://127.0.0.1:8787/v1（而不是 ovhcloud 的地址）。

Q: 报 400 invalid HTTP header ？
A: 这个头格式不对。正常不该出现（中转会整条丢弃），出现说明地址填错了。

Q: 端口被占用？
A: 程序会自动往后找端口，实际端口显示在说明页和控制台里。也可 --port 指定。

Q: 怎么确认它在工作？
A: 控制台会逐条打印转发日志，例如：
       [11:52:03]  POST /v1/chat/completions                200  1.42s
   说明页的「最近请求」表也会实时更新。

Q: 改完配置要重启吗？
A: 要。客户端改完 Provider 设置后重启一次，让它重新加载。

安全说明
--------
- 默认只监听本机 127.0.0.1，其它设备访问不到。
- 不保存任何对话内容，不写任何文件，除 ovhcloud 外不访问任何网络。
- 核心是单文件纯标准库 Python，可自行审计。


来源：李叔AI 公众号
"""


def write_readme() -> str:
    p = os.path.join(DIST, "使用说明.txt")
    body = README.replace("__VER__", VERSION).replace("__EXE__", EXE_NAME + ".exe")
    with open(p, "w", encoding="utf-8") as f:
        f.write(body)
    return p


def build_zip(readme_path: str) -> str:
    zp = os.path.join(DIST, "%s-v%s.zip" % (EXE_NAME, VERSION))
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(os.path.join(DIST, EXE_NAME + ".exe"), EXE_NAME + ".exe")
        z.write(readme_path, "使用说明.txt")
    return zp


def main() -> int:
    if not os.path.exists(SRC):
        print("找不到源文件：%s" % SRC)
        return 1

    print("[1/4] 生成图标 ...")
    make_icon(ICON)
    print("      -> %s  %.1f KB" % (ICON, os.path.getsize(ICON) / 1024))

    print("[2/4] PyInstaller 打包（约需 30-60 秒）...")
    if os.path.isdir(WORK):
        shutil.rmtree(WORK, ignore_errors=True)
    cmd = [sys.executable, "-m", "PyInstaller",
           "--onefile", "--console", "--clean", "--noconfirm",
           "--name", EXE_NAME,
           "--icon", ICON,
           "--distpath", DIST, "--workpath", WORK, "--specpath", WORK,
           SRC]
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    tail = r.stdout.decode("utf-8", "replace").strip().splitlines()[-6:]
    for line in tail:
        print("      " + line)
    if r.returncode != 0:
        print("打包失败，退出码 %d" % r.returncode)
        return r.returncode

    exe = os.path.join(DIST, EXE_NAME + ".exe")
    if not os.path.exists(exe):
        print("打包结束但没找到 exe：%s" % exe)
        return 1
    print("      -> %s  %.1f MB" % (exe, os.path.getsize(exe) / 1024 / 1024))

    print("[3/4] 写使用说明 ...")
    readme = write_readme()
    print("      -> %s" % readme)

    print("[4/4] 打 zip ...")
    zp = build_zip(readme)
    print("      -> %s  %.1f MB" % (zp, os.path.getsize(zp) / 1024 / 1024))

    print()
    print("完成。dist 目录：")
    for f in sorted(os.listdir(DIST)):
        print("  %-34s %8.1f KB" % (f, os.path.getsize(os.path.join(DIST, f)) / 1024))
    return 0


if __name__ == "__main__":
    sys.exit(main())
