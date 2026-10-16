# Docker 部署说明

给「OVHcloud 免密钥中转」的容器化跑法。中转本体是单文件纯标准库 Python，所以镜像里
只有一个 `.py` 和一个 `python:3.12-slim`，不装 pip 包、不编译，构建几秒完成。

默认行为与源码运行**完全一致**（同样的自动矫正、同样的说明页、同样的限流），
只按容器环境额外加了两个开关：`--strict-port`（端口占用直接失败）和 `--no-open`（容器里没有浏览器）。

---

## 一、前置条件

- Docker 20.10+（或 Docker Desktop）；用 `docker compose` 需要 Compose v2
- 宿主机能直连 `oai.endpoints.kepler.ai.cloud.ovh.net:443`
- 官方 `python:3.12-slim` 同时提供 **amd64 与 arm64**，所以 x86 服务器、Apple Silicon、树莓派都能直接构建

---

## 二、快速开始

### 方式 A：docker compose（推荐）

```bash
docker compose up -d --build     # 构建并后台启动
docker compose logs -f           # 看启动横幅和逐条请求日志
docker compose down              # 停止并删除容器
```

### 方式 B：docker 命令

```bash
docker build -t ovh-free-relay:1.4 .

docker run -d --name ovh-free-relay \
  -p 127.0.0.1:8787:8787 \
  --restart unless-stopped \
  ovh-free-relay:1.4

docker logs -f ovh-free-relay
```

起来之后，宿主机浏览器打开 <http://127.0.0.1:8787/> 就是那张说明页（含「最近请求」表）。

### 客户端里这样填

| 项 | 值 |
|---|---|
| 接口地址 / Base URL | `http://127.0.0.1:8787/v1` |
| API Key | 随便填，例如 `sk-ovh`（中转直接丢弃） |
| 模型名 | `Qwen3.8-27B` |

> 客户端如果跟中转**不在同一台机器**上（手机、另一台电脑），把 `127.0.0.1` 换成宿主机的局域网 IP，
> 并且按下面第四节把端口放开、把展示地址指对。

---

## 三、容器里的配置项

| 环境变量 | 等价参数 | 默认值 | 说明 |
|---|---|---|---|
| `OVH_RELAY_HOST` | `--host` | `0.0.0.0` | 容器内必须留在 `0.0.0.0`，否则端口映射进不来 |
| `OVH_RELAY_PORT` | `--port` | `8787` | 容器内监听端口。改了它，`-p` 右边也要跟着改 |
| `OVH_RELAY_PUBLIC_URL` | `--public-url` | 空 | **说明页上展示给客户端的地址**，见下一节 |

命令行参数优先级高于环境变量。想加 `--no-fix`、`--debug`、`--no-check` 这类开关，
覆盖 `command` 即可（compose 里取消注释那一行，或 `docker run` 时另写）：

```bash
docker run -d --name ovh-free-relay -p 127.0.0.1:8787:8787 \
  ovh-free-relay:1.4 \
  python /app/src/ovh_free_proxy.py --strict-port --no-open --no-fix --debug
```

### 关于 `OVH_RELAY_PUBLIC_URL`（容器里最容易踩的一个坑）

说明页会写「接口地址：`http://<监听地址>:<监听端口>/v1`」。容器里的监听端口是 8787，
但**宿主机映射出去的端口不一定是 8787**。这时候页面上的地址就是错的，客户端照抄会连不上。

两种处理：

1. **让两边端口一样**（推荐）：`-p 127.0.0.1:8787:8787`，什么都不用配。
2. **端口不一样就显式指定**：

```bash
docker run -d --name ovh-free-relay \
  -p 8899:8787 \
  -e OVH_RELAY_PUBLIC_URL=http://127.0.0.1:8899 \
  ovh-free-relay:1.4
```

写成 `192.168.1.10:8899` 这种省略 `http://`、省略 `/v1` 的也行，程序会自动补齐成
`http://192.168.1.10:8899/v1`。

---

## 四、让局域网里的手机 / 平板也能用

1. 端口映射改成不限定回环地址：

```yaml
    ports:
      - "8787:8787"      # 原来是 "127.0.0.1:8787:8787"
```

2. 把展示地址指到宿主机的局域网 IP：

```yaml
    environment:
      OVH_RELAY_PUBLIC_URL: "http://192.168.1.10:8787"
```

3. 客户端里填 `http://192.168.1.10:8787/v1`。

> **安全提醒**：这一步等于把中转开放给整个网段（公司网、宿舍网、公共 Wi-Fi 都算）。
> 中转只允许转发到 `/v1` 下的路径，但它**不做任何鉴权**——谁能访问这个端口，谁就能用你这条
> 匿名通道刷你 IP 的配额。不放心的就保持默认的 `127.0.0.1` 绑定。

---

## 五、常用运维

```bash
docker compose ps                       # 看状态与健康检查结果
docker compose logs -f --tail 100       # 跟随日志
docker stats ovh-free-relay             # CPU / 内存（纯标准库单进程，常驻内存很小）
curl http://127.0.0.1:8787/health       # 本地探针
curl http://127.0.0.1:8787/api/stats    # 成功/失败计数、运行时长
curl http://127.0.0.1:8787/api/logs     # 最近 20 条请求（含上游原始报错）
docker exec -it ovh-free-relay sh       # 进容器（镜像里没有 curl，用 python 或 wget 测）
docker compose up -d --build            # 改完代码后重建
```

部署完想一条命令确认「容器真的能用」，用仓库自带的链路体检脚本（它会故意塞上游不认的字段、
发一次流式请求、撞上 429 会自己等）：

```bash
python src/tests/smoke_running.py http://127.0.0.1:8787
```

全过就说明中转可用，客户端可以填 `http://127.0.0.1:8787/v1` 了。

镜像自带的健康检查打的是容器内 `/health`（本地端点，**不消耗上游配额**）：

```
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3
```

`docker compose ps` 里显示 `unhealthy` 一般不是上游问题，而是容器里的进程没在监听那个端口
（端口被别的进程占了、或者 `OVH_RELAY_PORT` 被改成了别的值）。

---

## 六、这个镜像里做了什么

| 做法 | 原因 |
|---|---|
| `FROM python:3.12-slim`，只 `COPY src/ovh_free_proxy.py` | 纯标准库，不需要装依赖；镜像小、构建快、攻击面小 |
| `PYTHONUNBUFFERED=1` | 不加则 stdout 被块缓冲，`docker logs` 里看不到横幅和请求日志 |
| `CMD ... --strict-port` | 容器里绝不能让程序「自动往后找端口」，否则映射出去的端口指向空气 |
| `CMD ... --no-open` | 容器里没有浏览器，`webbrowser.open` 无意义 |
| 构建期 `ssl.create_default_context()` 自检 | 上游走 HTTPS，镜像缺 CA 根证书会 TLS 失败，构建期就报出来 |
| `useradd relay` + `USER relay` | 程序不写任何文件，不需要 root；容器被攻破也拿不到 root |
| 不挂 volume | 程序不保存对话、不落盘，无状态，删了重建即可 |

想要更严一点，可以在 compose 里放开这两行（程序不需要写文件系统）：

```yaml
    read_only: true
    tmpfs:
      - /tmp
```

---

## 七、已知限制

- **限流按公网 IP 算，不由容器决定**。约每 30 秒放行 2 次请求；容器、本机 exe、同一路由器后面的
  手机，全都共用这一份额度。中转不绕过限流，它只解决客户端强制填 key 这一栏。
- **不读 `HTTP_PROXY` / `HTTPS_PROXY`**。程序用 `http.client` 直连上游，企业代理环境下需要自己在
  网络层解决（例如给容器配代理侧车或改宿主机的出口）。
- **只转发到固定的 OVHcloud 上游**，不能当中转去连别的服务。
- 上游模型清单、限流策略、端点可用性都由 OVHcloud 单方面决定，随时可能变化。
- 未签名的第三方镜像容易被企业镜像仓库/杀软拦截；本项目镜像是本地构建的，没有推送任何公共仓库。

---

## 八、排错

| 现象 | 原因 | 处理 |
|---|---|---|
| `docker logs` 一片空白 | 自己改了 `command` 又没保留环境变量，stdout 被块缓冲 | 保留镜像里的 `PYTHONUNBUFFERED=1`，或在命令里加 `python -u` |
| 容器起来立刻退出，日志有「启动失败」 | `--strict-port` 下 8787 被占用 | 改 `OVH_RELAY_PORT`，同时同步 `-p` 与 `OVH_RELAY_PUBLIC_URL` |
| `restart: unless-stopped` 下反复重启 | 同上，进程一起就退 | `docker compose logs --tail 50` 看那句「启动失败」的具体原因 |
| 客户端连不上，但容器是 Up | 端口映射只绑了 `127.0.0.1`，客户端在别的机器上 | 见第四节，改 `ports` 与 `OVH_RELAY_PUBLIC_URL` |
| 说明页地址不对 | 宿主机端口 ≠ 容器端口 | 设 `OVH_RELAY_PUBLIC_URL` |
| 客户端报 429 | 匿名限流，约 30 秒 2 次 | 等半分钟；换 IP 才有新额度 |
| 客户端报 400，页面「最近请求」有红色行 | 请求体被上游拒了 | 看那一行的**上游原文**；也可用 `--no-fix --debug` 关掉自动矫正复现 |
| 客户端报 404 | 少了 `/v1`、多了 `/v1/v1`、模型名不对 | 中转会自动折叠路径，先核对模型名 `Qwen3.8-27B` |
| 客户端报 415 | 客户端没带 `Content-Type: application/json`（中转原样转发） | 换客户端或检查其自定义头设置 |

完整的上游行为实测结论（鉴权三态、`reasoning_effort` 取值、必拒字段等）见仓库根目录 `README.md` 第四节。

---

## 九、这份 Docker 支持的验证情况

- **已实测**（在 Windows 宿主机上，用源码跑，命令行与容器内 `CMD` 完全一致）：
  - `--strict-port --no-open` 启动；`/health`、`/api/stats`、`/api/logs` 均 200
  - `GET /v1/models` 经中转发上游 → 200，23 个模型
  - `POST /v1/chat/completions` 非流式 → 200，`reasoning` 嵌套对象 + `thinking` + `user` 被正确摘除，
    `reasoning_effort=none` 生效（回包 `reasoning_tokens: 0`）
  - `stream=true` → 200，SSE 分块正常转发
  - `/v1/messages` → 400 中文提示；环境变量 `OVH_RELAY_PORT`、`OVH_RELAY_PUBLIC_URL` 生效
- **未实测**：`docker build` / `docker run` 本身——本次改动所在的机器上**没有安装 Docker**。
  Dockerfile 只用到了 `python:3.12-slim`、`useradd`、`HEALTHCHECK` 这些标准写法，但第一次构建
  仍建议先跑一次 `docker compose up --build` 确认。
