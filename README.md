# OVHcloud 免密钥中转（ovh-free-relay）

给那些**强制要求填 API Key** 的 AI 客户端，接上 OVHcloud 的**免密钥免费**大模型通道。

在本地起一个中转服务：客户端把请求发给 `127.0.0.1`，中转把请求里的 key 头整条剥掉，再转发给 OVHcloud。客户端以为自己在跟一个正常的 OpenAI 兼容接口说话，实际上一个 key 都没用到。

```
你的客户端  ──►  http://127.0.0.1:8787/v1  ──►  OVHcloud 免密钥端点
                （中转：剥掉 Authorization）      oai.endpoints.kepler.ai.cloud.ovh.net/v1
```

> 本仓库所有结论均为 2026-10-12 在本机逐条实测得出，非转述。实测脚本见 `src/tests/`。

---

## 一、这个通道是什么

| 项目 | 值 |
|---|---|
| 接口地址 | `https://oai.endpoints.kepler.ai.cloud.ovh.net/v1` |
| 模型名 | `Qwen3.8-27B`（大小写不敏感，回包会归一化成这个写法） |
| API Key | **不需要**。不发、发空值、发空格都返回 200 |
| 鉴权 | 匿名。带上任何**实际内容**的 key 反而 403 |
| 上下文 | 262,144 tokens |
| 挂牌价 | $0.47 / 百万 input，$3.19 / 百万 output |
| 限流 | 约**每 30 秒放行 2 次**（突发式，按你的上网 IP 算） |

`GET /v1/models` 免鉴权可读，匿名返回 23 条，其中 11 个可聊天。

**它解决什么问题**：有些客户端（如 ZCode）的 Provider 配置里 API Key 是必填项，不给就不让保存。中转存在的唯一意义就是把这一栏糊弄过去。

**它不解决什么问题**：不绕过限流。限流是按 IP 算的，中转只解决 key 这一栏。

---

## 二、快速开始

### 方式 A：直接用 exe（推荐，Windows）

1. 下载 `dist/OVH免密钥中转.exe`
2. 双击。浏览器会自动打开说明页 `http://127.0.0.1:8787/`
3. 在客户端里填：

```
接口地址  ->  http://127.0.0.1:8787/v1
API Key   ->  随便填，比如 sk-ovh
模型名    ->  Qwen3.8-27B
```

关掉那个黑色窗口即停止。

### 方式 B：跑源码

```bash
python src/ovh_free_proxy.py
```

零第三方依赖，纯标准库。Python 3.8+ 即可。

### 方式 C：Docker

```bash
docker compose up -d --build
```

等价的手写命令：

```bash
docker build -t ovh-free-relay:1.4 .
docker run -d --name ovh-free-relay -p 127.0.0.1:8787:8787 --restart unless-stopped ovh-free-relay:1.4
```

容器内监听 `0.0.0.0:8787`，映射到宿主机 `127.0.0.1:8787`，客户端填法与上面一致，
说明页在 <http://127.0.0.1:8787/>，日志用 `docker compose logs -f` 看。

镜像里只有 `python:3.12-slim` + 一个 `.py`，不装第三方依赖，amd64 / arm64 都能直接构建。
宿主机端口与容器端口不一致（例如 `-p 8899:8787`）时，用 `OVH_RELAY_PUBLIC_URL` 告诉程序
说明页上该显示哪个地址。细节与排错见 `docs/Docker使用说明.md`。

---

## 三、命令行参数

| 参数 | 说明 |
|---|---|
| `--port 8899` | 换端口（默认 8787；被占用会自动往后找） |
| `--host 0.0.0.0` | 让手机/平板等局域网设备也能用（其它设备把 `127.0.0.1` 换成这台电脑的局域网 IP） |
| `--no-open` | 启动时不自动打开说明页 |
| `--no-check` | 跳过启动时的链路自检 |
| `--debug` | 诊断模式：把客户端发来的原始请求（路径/请求头/正文）全部打到控制台 |
| `--no-fix` | 关闭参数自动矫正（默认开启） |
| `--selftest` | 只跑一次自检就退出 |
| `--strict-port` | 端口被占用时直接失败，不自动往后找 |
| `--public-url http://192.168.1.10:8899` | 指定说明页/横幅上展示给客户端的接口地址（默认按监听地址推断）；Docker 里宿主机端口与容器端口不一致时用它 |

也支持用环境变量代替参数，方便容器 / systemd 这类不便改启动命令的场景：
`OVH_RELAY_HOST`、`OVH_RELAY_PORT`、`OVH_RELAY_PUBLIC_URL`（**命令行参数优先**）。

说明页还提供 `/health`、`/api/stats`、`/api/logs` 三个接口，以及一张**「最近请求」表**：绿=已自动矫正，红=附上游原始报错。

---

## 四、实测硬结论（本机逐条验证）

### 4.1 鉴权三态

| 客户端发的头 | 结果 |
|---|---|
| 不发 `Authorization` | **200** |
| `Authorization: Bearer`（无值） | **200** |
| `Authorization: Bearer ` +（空格/Tab） | **200** |
| `Authorization: Bearer sk-xxx`（任何实内容） | **403** |
| 裸值 / 空值（非 Bearer 格式） | **400** `invalid HTTP header` |

→ 结论：**空格 key 就能过**。早期流传的「只能走代码调用」是错的。

### 4.2 推理字段（这块最容易踩坑）

**顶层 `reasoning_effort`**：

| 取值 | 结果 |
|---|---|
| 不填 | 200，默认 `xhigh`，reasoning 247 字 |
| `none` | **200**，reasoning **0 字**，1.21 秒返回 —— 这才是真正的「关思考」开关 |
| `xhigh` / `medium` / `low` | 200 |
| `high` / `minimal` | **400** |

> 官方文案里没写 `none`，但实测合法。v1.3 曾把 `none` 误改成 `xhigh`——等于把省电模式强行拧到最重档，token 差 10 倍。v1.4 已修正。

**嵌套 `reasoning` 对象**：**整条黑名单**。`{"effort":"xhigh"}`、`{"effort":"none"}`、`{"enabled":false}` 全部 400，报错原文：

```
feature 'extra arguments: {"reasoning":{"effort":"none"}}' is not currently supported
```

→ 只能整条摘掉，不能「改个值再发」。v1.4 会先把里面的 `effort` 提升到顶层 `reasoning_effort`，再删除整个对象，免得把客户端的「关思考」意图一起扔掉。

**其它会被拒的顶层字段**：`thinking`、`enable_thinking`、`user`、`extra_body`、`provider`。

**还有两条**：`metadata` 的值必须全是字符串（给数字 → 422）；`stream_options` 必须与 `stream=true` 成对出现，否则 400。

**响应里推理字段叫 `reasoning`，不是 `reasoning_content`。**

### 4.3 状态码对照

| 码 | 含义 |
|---|---|
| 200 | 正常 |
| 400 | 上游拒绝请求体，原因见 4.2。说明页会附「上游原文」和「请求体摘要」 |
| 403 | 请求里带了 key（中转正常不会出现） |
| 404 | 地址不对或模型名不存在。典型：少了 `/v1`，或多了一层 `/v1/v1`，或模型名前后带空格 |
| 422 | 请求体字段类型不对 |
| 429 | 匿名限流，等 30 秒 |

---

## 五、v1.4 的自动矫正做了什么

客户端发来的请求，中转在转发前会：

1. 路径归一化 —— 折叠重复的 `/v1/v1`，缺 `/v1` 的补上
2. 拦截 `/v1/messages`（Anthropic 协议）—— OVHcloud 这条通道不认，直接返回中文提示，告诉你去改客户端
3. 参数矫正 —— 把 `reasoning.effort` 提升到顶层再摘除嵌套对象；摘掉陌生顶层字段；`none` 原样保留；`stream_options` 不配对时移除
4. 整条剥掉 `Authorization` 头
5. 非 200 时记录上游原文（已剥掉套娃 JSON）与请求体摘要

改了什么，说明页的「最近请求」表里会用绿色标出来。想自己看原始报错，用 `--no-fix` 关掉自动矫正。

---

## 六、ZCode 这类客户端特别注意

- 新建自定义 Provider 时，「API 格式 / API 类型」**必须选 Chat Completions**。选成 Anthropic 会往 `/v1/messages` 发，直接 404。
- 接口地址末尾带不带 `/v1` 都可以，本程序会自动纠正重复的 `/v1`。
- API Key 栏必填的话，**填一个空格**即可。
- 报 400 时，先看说明页「最近请求」表里的「上游原文」——它就是答案。

详见 `docs/ZCode-接入排查卡.md`。

---

## 七、自己编译

```bash
pip install pyinstaller pillow
python src/build_exe.py
```

产物在 `dist/`。打包脚本内置版本号常量 `VERSION`，改它即可。

打包出来的 exe 是 PyInstaller 单文件，跑起来是**两个进程**（引导 + 主体），别误判成重复实例。

---

## 八、安全说明

- 默认只监听 `127.0.0.1`，其它设备访问不到
- 不保存任何对话内容，不写任何文件，除 OVHcloud 外不访问任何网络
- 核心是单文件纯标准库 Python，可自行审计
- Docker 镜像同样默认只把端口绑在宿主机 `127.0.0.1`，并以非 root 用户运行；程序无状态，不需要挂卷

> 杀软报毒：PyInstaller 打包的未签名程序的常见误报。点「更多信息」→「仍要运行」，或加白名单。

---

## 九、目录结构

```
.
├── src/
│   ├── ovh_free_proxy.py      # 中转主程序（纯标准库）
│   ├── build_exe.py           # PyInstaller 打包脚本
│   ├── icon.ico
│   └── tests/                 # 实测脚本（test_v14.py 行为验证 / smoke_running.py 链路体检）
├── docs/
│   ├── 使用说明.txt            # 随 exe 分发的说明
│   ├── ZCode-接入排查卡.md     # 面向读者的排错卡
│   └── Docker使用说明.md       # Docker 部署、配置与排错
├── Dockerfile                 # 容器镜像（python:3.12-slim + 单文件程序）
├── docker-compose.yml         # 编排示例（默认只绑宿主机 127.0.0.1）
├── .dockerignore
├── dist/                      # 编译产物（exe + zip）
└── assets/                    # 说明页预览图
```

---

## 十、免责声明

OVHcloud 的免密钥通道是其公开端点，本仓库只是把客户端请求里的 key 头剥掉，**没有破解、没有绕过鉴权、没有伪造身份**。限流、可用性、模型清单均由 OVHcloud 单方面决定，随时可能变化。请遵守其服务条款，勿用于高频刷量。

本项目仅用于技术研究与学习交流。

---

来源：李叔AI 公众号
