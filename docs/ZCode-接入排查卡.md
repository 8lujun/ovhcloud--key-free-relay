# ZCode 接 OVHcloud 免密钥通道 · 排查卡

> 实测时间：2026-10-12 12:40 前后，累计 42 组对照
> 服务端状态：正常（`Qwen3.8-27B` 在目录中，无鉴权头请求 HTTP 200 正常出字）

---

## 一、先记住一件事

**「Provider rejected the model request.」是客户端的万能错误文案。**

服务端返回的**任何非 200**，ZCode 都会显示成这一句。它本身不告诉你是哪儿错了。
所以第一步不是猜，是**看到底返回了什么状态码、什么原文**。

怎么看到？两条路：

- 接本机中转（第四节），说明页下半部分的**「最近请求」**表会记下每一次请求的
  状态码、**请求体摘要**和**上游返回的原文**（已剥掉套娃 JSON）。报错配不上时，
  看一眼这张表就有答案。
- 或用 `OVH免密钥中转.exe --debug` 启动，控制台会把原始请求整个打出来。

服务端本身没问题（本轮重新实测）：

| 测试 | 结果 |
|---|---|
| `GET /v1/models` 不带任何鉴权头 | **200**，23 个模型，`Qwen3.8-27B` 在列 |
| `POST /v1/chat/completions` 不发 Authorization 头 | **200**，正常出字 |
| `POST` 带 `Authorization: Bearer `（尾空格） | **200** |

---

## 二、按可能性从高到低，逐条对

### ① 报 400：请求体里有上游不认的东西 —— 实测挖出四个真凶

只要客户端拿到的不是 200，说明页里就会看到 400。**400 不是"格式乱"，是上游明确
指出了下面四件事之一**，报错原文会写清楚是哪一个。

#### 真凶 1：嵌套的 `reasoning` 对象（ZCode 实际撞的就是这个）

Vercel AI SDK 系客户端会发**两个**推理参数：

```json
{ "reasoning": {"effort": "none"}, "reasoning_effort": "none" }
```

上游对**整个 `reasoning` 对象**是拒收的 —— 不是取值问题：

```
feature 'extra arguments: {"reasoning":{"effort":"none"}}' is not currently supported
```

| 请求体里写的 | 上游返回 |
|---|---|
| `reasoning: {"effort":"none"}` | **400**（上面那句原文） |
| `reasoning: {"effort":"xhigh"}` | **400** ← 换合法值也一样被拒 |
| `reasoning: {"enabled":false}` | **400** |
| 完全不写 `reasoning` | **200** |

所以只能**整体摘掉**它。

→ v1.4 中转摘掉它之前，会先把里面的 `effort` 提升到顶层的 `reasoning_effort`，
免得把客户端的意图一起扔掉。

#### 真凶 2：推理强度取值不对

这个网关官方报错里写着只认 **`xhigh`（默认）/ `medium` / `low`**：

```
Unexpected reasoning effort high. Supported types are xhigh (default), medium, and low.
```

但实测还有**第四个值 `none`** —— 官方文案里没写，却是**真正管用的"关思考"开关**。
同一道题实测：

| `reasoning_effort` | 返回 | `reasoning` 字数 | completion tokens | 耗时 |
|---|---|---|---|---|
| 不写 | 200 | 247 字 | 147 | 2.25 s |
| **`none`** | 200 | **0 字** | **14** | **1.21 s** |
| `xhigh` | 200 | 226 字 | 108 | 1.77 s |
| `medium` | 200 | 140 字 | 130 | 2.24 s |
| `high` | **400** | — | — | — |
| `minimal` | **400** | — | — | — |

**合法集合 = `xhigh` / `medium` / `low` / `none`。**

麻烦在于：这些值往往不在客户端设置项里，你在界面上改不了。
→ v1.4 中转只把**真正非法**的值（`high`、`minimal` 等）改成 `xhigh`，
**`none` 原样保留**。

#### 真凶 3：塞了上游不认识的顶层字段

这个网关只要见到不认识的顶层字段就 400，并且会把它列出来：

```
feature 'extra arguments: {"user":"u1"}' is not currently supported
```

实测被拒收的字段：`thinking`、`enable_thinking`、`user`、`extra_body`、`provider`，
外加上面的 `reasoning`。另外 `metadata` 是认的，但**值必须全是字符串**，
给了数字会返回 422：`metadata.a: invalid type: integer 1, expected a string`。

实测放行的字段（可以放心带）：`temperature`、`top_p`、`max_tokens`、
`max_completion_tokens`、`n`、`store`、`seed`、`stop`、`stream`、`stream_options`、
`tools`、`tool_choice`、`parallel_tool_calls`、`response_format`、
`frequency_penalty`、`presence_penalty`。

→ v1.4 中转会把上面那些陌生字段摘掉。

#### 真凶 4：`stream_options` 和 `stream` 没配对

`stream_options` 只有在 `stream=true` 时才能出现，否则：

```
Stream options can only be defined when `stream=True`. (parameter=stream_options)
```

`stream=true` + `stream_options` 实测 **200**，SSE 正常。

→ v1.4 中转在 `stream` 不是 true 时会移除 `stream_options`。

**一句话**：装 v1.4 中转，这四条全自动处理，你不用去翻客户端的设置。改了什么，
说明页会**用绿色标出来**（"已自动矫正"那一行）。

> ⚠️ 顺带勘误：v1.3 曾把 `reasoning_effort: "none"` 也当成非法值改成 `xhigh`。
> 那是错的 —— 等于把省电模式强行拧到最重档（思考 226 字 vs 0 字，token 差 10 倍）。
> v1.4 已修正。**还在用 v1.3 的请换 v1.4。**

---

### ② 报 404：API 格式选成了 Anthropic

ZCode 新建 Provider 时如果「API 格式 / API 类型」选了 Anthropic，
它会把请求发到 `/v1/messages`。实测：

```
POST /v1/messages      -> 404  {"error":{"code":"unknown_url", ...}}
POST /v1/v1/messages   -> 404
```

OVHcloud 这条通道**只提供 OpenAI 协议**，没有 Anthropic 端点。

**改法**：Provider 设置里把「API 格式 / API 类型」改成 **Chat Completions**，然后重启 ZCode。

### ③ 报 404：Base URL 里的 `/v1` 写多或写少了

| 你填的地址 | 实测结果 |
|---|---|
| `https://oai.endpoints.kepler.ai.cloud.ovh.net/v1` | ✅ 正常 |
| `https://oai.endpoints.kepler.ai.cloud.ovh.net`（客户端不补 /v1） | ❌ 404 `unknown_url` |
| 填到 `/v1` 但客户端又拼一层，变成 `/v1/v1/chat/completions` | ❌ 404 `unknown_url` |

两种 404 的原文都是：
`Unknown request URL: POST /xxx. Please check the URL for typos.`

**省事解法**：接本机中转，它把三种填法全部归一化。

### ④ 报 403：API Key 栏 —— 填**一个空格**

ZCode 的 Key 栏是必填的。但 OVHcloud 匿名层的放行条件很反直觉：

| Key 栏内容 | 实测 |
|---|---|
| 空（客户端不让你存） | — |
| `sk-xxx` / `no-key` / 任何有内容的字符串 | ❌ **403** `Forbidden: authentication failed.` |
| **一个空格** | ✅ **200** |

实测 `Bearer `（尾空格）、`Bearer  `（两个空格）、`Bearer   `（三个空格）、
`Bearer\t`（Tab）**全部 200**。

**改法**：Key 栏里敲一下空格就行。

### ⑤ 报 404：模型名

- 必须严格是 `Qwen3.8-27B`
- **前后不能有空格**（实测前导空格 → 404 `model_not_found`：
  `` The model ` Qwen3.8-27B` does not exist. ``）
- 大小写**不敏感**（小写 `qwen3.8-27b` 也能通，服务端会把回包 model 归一化成 `Qwen3.8-27B`）

### ⑥ 报 429：限流

匿名层约**每 30 秒放行 2 次**。调试时连着点几下就会撞上。原文：
`{"message":"API rate limit exceeded","request_id":"..."}`

实测节奏（间隔 10 秒连打 10 次）：
`[200, 200, 429, 429, 429, 200, 200, 429, 429, 429]`

**改法**：等 30 秒。这不是配置问题。

---

## 三、另外两个已确认、但不常撞的坑

| 情况 | 上游返回 |
|---|---|
| 消息里只有 system、没有 user | **400** `No user query found in messages.` |
| 请求体不是合法 JSON / 顶层不是对象 | **422** `Failed to deserialize the JSON body into the target type` |
| 带 `tools` 定义请求 | 200，但**单次实测响应耗时 29.7 秒**；客户端超时若设得偏短也可能失败 |

---

## 四、最省事的路子：接本机中转（v1.4）

不想逐条排查，就用配套的中转程序。它做五件事：

1. **丢掉 Authorization 头** —— Key 栏随便填，403 从此不会出现
2. **路径归一化** —— `/v1/v1/...` 折叠、漏写的 `/v1` 补上，Base URL 怎么填都能通
3. **协议提醒** —— 若客户端发来 Anthropic 的 `/v1/messages`，直接返回一句中文提示，
   0.05 秒，不用你猜，也不白吃上游配额
4. **参数自动矫正**（v1.4 增强）—— 摘掉上游不认的字段（含**嵌套 `reasoning` 对象**，
   并把它里面的 `effort` 提升到顶层）、把 `reasoning_effort` 的**非法**取值改成
   `xhigh`（`none` 保留）、`stream_options` 与 `stream` 不配对时移除，**400 四真凶一次解决**
5. **上游原文回显** —— 非 200 时把上游返回的原文和请求体摘要记在说明页上，
   排错不用再靠猜

客户端里这样填：

```
接口地址  ->  http://127.0.0.1:8787/v1      （末尾带不带 /v1 都行）
API Key   ->  随便填，例如 sk-ovh
模型名    ->  Qwen3.8-27B
```

说明页下半部分那张**「最近请求」**表，正常请求一行带过；出错的请求会多出两块：

- **请求体**（客户端发来的字段与消息结构）
- **上游原文**（OVHcloud 返回的原始报错，套娃 JSON 已剥掉）

被自动矫正过的请求，还会多一行**绿色的"已自动矫正"**，写清楚改了什么。

想自己对比原始报错的，用 `--no-fix` 关掉自动矫正：

```
OVH免密钥中转.exe --no-fix
```

---

## 五、状态码速查

| 码 | 含义 | 对应动作 |
|---|---|---|
| 200 | 正常 | — |
| 400 | 上游拒绝请求体 | 四个真凶见第二节①；或协议不匹配；或消息里没有 user |
| 403 | 请求带了 key | Key 栏清空或只留一个空格 |
| 404 | 地址或模型名不对 | 查 `/v1` 层级；查模型名拼写和空格 |
| 422 | 字段类型不对 | 如 `metadata` 的值给了数字 |
| 429 | 限流 | 等 30 秒 |
| 502 | 连不上上游 | 查本机网络 / 代理 |

---

来源：李叔AI 公众号
