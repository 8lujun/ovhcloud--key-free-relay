# syntax=docker/dockerfile:1
#
# OVHcloud 免密钥中转 —— 容器镜像
#
#   中转本体是单文件纯标准库 Python，所以镜像里只需要 python + 一个 .py，
#   不装 pip 包、不编译、构建很快（秒级）。
#
#   构建：docker build -t ovh-free-relay:1.4 .
#   运行：docker run -d --name ovh-free-relay -p 127.0.0.1:8787:8787 ovh-free-relay:1.4
#   浏览器打开 http://127.0.0.1:8787/ 看说明页；docker logs -f ovh-free-relay 看日志。
#
#   官方 python 镜像同时提供 amd64 与 arm64，树莓派/NAS/Apple Silicon 直接可用。
#
FROM python:3.12-slim

LABEL org.opencontainers.image.title="OVHcloud 免密钥中转" \
      org.opencontainers.image.description="剥掉客户端强填的 Authorization 头，转发到 OVHcloud 匿名通道（OpenAI 兼容）" \
      org.opencontainers.image.version="1.4" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://gitee.com/yangfancms/ovhcloud--key-free-relay"

# PYTHONUNBUFFERED 必须开：否则 docker logs 里看不到启动横幅和逐条请求日志
# （stdout 被块缓冲，容器里看着像「没有任何输出」）。
# 监听地址必须是 0.0.0.0，容器里的 127.0.0.1 外面访问不到。
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONIOENCODING=utf-8 \
    OVH_RELAY_HOST=0.0.0.0 \
    OVH_RELAY_PORT=8787 \
    OVH_RELAY_PUBLIC_URL=""

WORKDIR /app

COPY src/ovh_free_proxy.py /app/src/ovh_free_proxy.py

# 上游走 HTTPS，镜像里缺 CA 根证书会直接 TLS 失败 —— 构建时就把它验掉，
# 别等运行时才发现（官方 python:*-slim 自带 ca-certificates，正常都会过）。
RUN python -c "import ssl; ssl.create_default_context()" \
    || { echo "错误：镜像内没有可用的 CA 根证书。请在 Dockerfile 里补上：" >&2; \
         echo "      RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates" >&2; \
         exit 1; }

# 非 root 运行：程序不写任何文件，用不到写权限
RUN useradd --create-home --shell /usr/sbin/nologin relay \
    && chown -R relay:relay /app
USER relay

EXPOSE 8787

# 启动自检、参数矫正等行为与源码运行完全一致，仅按容器环境加两个开关：
#   --strict-port  端口被占用就直接失败。容器里不能「自动往后找端口」，
#                  否则映射出去的 8787 指向空气，排查起来很费劲。
#   --no-open      容器里没有浏览器，别去调 webbrowser。
# 想看上游原始报错（关掉自动矫正），运行时覆盖 command 加 --no-fix 即可。
CMD ["python", "/app/src/ovh_free_proxy.py", "--strict-port", "--no-open"]

# 探针打本地 /health，不消耗上游的匿名配额。
# 端口从环境变量读，改了 OVH_RELAY_PORT 也不用同步改这里。
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('OVH_RELAY_PORT','8787')+'/health', timeout=4).status==200 else 1)"
