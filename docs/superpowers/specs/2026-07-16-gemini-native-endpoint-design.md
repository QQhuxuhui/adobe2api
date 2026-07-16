# adobe2api Gemini 原生协议入口（接入 sub2api gemini 类账号）

日期：2026-07-16
状态：设计已确认（v2，已按内部复审修正 7 项：SSE 失败语义、contents 字段、
比例白名单、错误码适配、测活白名单、usage 精确公式、输入图限额）

## 背景

用户的 sub2api 按平台分组：买家走 Gemini 原生协议（`generateContent`）的售卖池只调度
gemini 类账号，gemini APIKey 账号要求上游讲 Gemini 原生协议。adobe2api 目前只有
OpenAI 协议，接不进这个池子。本设计给 adobe2api 加一个 Gemini 原生入口，让 Firefly
的 nano-banana 产能进入现有 gemini 售卖池。

已确认的 sub2api 对接合同（读 fork 源码 `gemini_messages_compat_service.go` 得出）：

- 上游调用：`POST {base_url}/v1beta/models/{mappedModel}:{action}`，
  action ∈ {generateContent, streamGenerateContent, countTokens}，流式加 `?alt=sse`；
- 鉴权：`x-goog-api-key: <api_key>` 头；账号级模型映射可用（恒等映射即可）；
- 账号测活：`streamGenerateContent`，默认测试模型 `gemini-2.0-flash`
  （`geminicli.DefaultTestModel`）；
- 流式：SSE 原样转发给买家，逐 chunk 解析 usage（`extractGeminiUsage`）；
- 伪装器触发判据：`modelVersion` 非真 pro 前缀 或 缺 `candidatesTokensDetails[IMAGE]`
  → 本入口返回规范结构即不触发，计费与响应体天然同源，sub2api 零改动。

用户已拍板：两档模型都上；默认 imageSize=1K（跟 Google 一致）；usage 模仿真模型画像。

## 端点

新文件 `api/routes/gemini_native.py`，挂到现有 FastAPI app（同端口同 token 池）：

| 端点 | 行为 |
|---|---|
| `POST /v1beta/models/{model}:generateContent` | 非流式生图 |
| `POST /v1beta/models/{model}:streamGenerateContent`（`?alt=sse`） | SSE 流式生图 |
| `POST /v1beta/models/{model}:countTokens` | 纯估算，不打 Adobe |
| `GET /v1beta/models` | Gemini 官方结构模型列表 |
| `GET /v1beta/models/{model}` | 单模型查询（同一张表） |

FastAPI 路径段含冒号：用单段参数捕获后按 `:` 拆分 model 与 action；未知 action 返回
404 Google 错误。

鉴权：`x-goog-api-key` 头或 `?key=` 查询参数，校验 `config.api_key`。

### 错误码适配层（复审 #4）

**不能直接复用 `_run_with_token_retries`**：该函数把 QuotaExhausted 与
UpstreamTemporary 都转成 HTTP 503、AuthError 转 401，且抛的是 FastAPI 默认
`{"detail":...}`，均不符合 Gemini 合同。本入口需要一个专用适配层：

- 生图仍复用 `run_with_token_retries`（拿轮询/重试/标记能力），但用它的
  **底层 run_once 返回值 + 捕获领域异常**，不经过 app.py 里那套 HTTPException 映射。
  具体做法：在本路由内 try/except 捕获 `QuotaExhaustedError`/`AuthError`/
  `UpstreamTemporaryError`/`HTTPException`/通用 `Exception`，各自映射为 Google 错误。
- 统一 Google 错误结构 `{"error":{"code":<httpStatus>,"message":<str>,"status":<ENUM>}}`：

  | 领域情况 | HTTP | status |
  |---|---|---|
  | 鉴权失败（缺/错 key） | 401 | UNAUTHENTICATED |
  | 请求非法（JSON/参数/无文本/未知 action） | 400 | INVALID_ARGUMENT |
  | 未知模型（列表/单查/生图非白名单） | 404 | NOT_FOUND |
  | Token 配额尽（QuotaExhaustedError） | 429 | RESOURCE_EXHAUSTED |
  | 上游临时错误重试仍失败（UpstreamTemporaryError） | 503 | UNAVAILABLE |
  | 账号全失效（AuthError） | 401 | UNAUTHENTICATED |
  | 其它未预期 | 500 | INTERNAL |

  注：与初版不同，临时错误用 **503 UNAVAILABLE**（对齐 Adobe 侧现状与 Google
  惯例），不再写 500/502。所有这些都在 **SSE 开流之前** 决定（见「生成与响应」），
  因此状态码可正确设置。

## 模型映射

| 请求模型名 | 上游 modelId/modelVersion | usage 画像 |
|---|---|---|
| `gemini-3-pro-image`、`gemini-3-pro-image-preview` | `gemini-flash` / `nano-banana-2` | pro |
| `gemini-3.1-flash-image`、`gemini-3.1-flash-image-preview` | `gemini-flash` / `nano-banana-3` | flash |
| 测活白名单文本模型（见下） | 不打 Adobe，罐头文本响应 | 文本小额 |
| 其它未知模型名 | — | 404 NOT_FOUND |

### 测活白名单（复审 #5）

sub2api gemini 账号测活默认用 **`gemini-2.0-flash`**（`DefaultTestModel`，非初版
误写的 `gemini-3-pro-preview`），走 `streamGenerateContent`。因此罐头文本**仅**对
一张明确白名单生效：

```
TEST_TEXT_MODELS = {
    "gemini-2.0-flash", "gemini-2.5-flash",
    "gemini-3-pro-preview", "gemini-3.1-pro-preview",  # 兼容其它测活配置
}
```

- 命中白名单：`generateContent`/`streamGenerateContent` 返回固定短文本
  （见 usage 章节的精确值），不调用上游；
- **未命中且非图像模型：404 NOT_FOUND**——与 `GET /models/{model}` 语义一致，
  拼写错误不被静默吞掉。

`GET /v1beta/models` 列出 4 个图像模型 + 白名单文本模型：`name:"models/<id>"`、
`displayName`、`supportedGenerationMethods:["generateContent","streamGenerateContent","countTokens"]`。
`GET /v1beta/models/{model}` 对表内返回该条，表外 404。

## 请求解析（复审 #2）

Gemini 原生请求体顶层是 **`contents[]`**（必填数组），另有可选 `systemInstruction`。
Adobe `client.generate` 只接受单 prompt 字符串 + 若干输入图，因此必须**显式扁平化**，
不得静默丢弃系统指令或历史：

- **prompt**：`systemInstruction.parts[].text`（若有）+ **所有** `contents[].parts[].text`
  按顺序拼接（换行分隔）。空文本且无输入图时 400 INVALID_ARGUMENT。
  （多轮编辑语义有损是 Adobe 单 prompt 上游的固有限制，但至少不丢内容。）
- **输入图**：遍历 **所有** `contents[].parts[]` 收集 `inlineData`（兼容 snake_case
  `inline_data`）；不处理 `fileData`（URL 引用，YAGNI）。按出现顺序取前 6 张。
- `generationConfig.imageConfig.aspectRatio`：**按上游族独立白名单**（见 #3），
  缺省 `1:1`；非白名单值 400。
- `imageConfig.imageSize`：`1K`/`2K`/`4K`（大小写不敏感）；缺省 `1K`（Google 官方
  默认）；其它值（含 `0.5K`，Adobe 无此档）400。

### 输入图资源限制（复审 #7，复用 OpenAI 路径口径）

- MIME 白名单：`image/jpeg|jpg|png|webp`，`_normalize_image_mime` 归一化，
  非白名单 400；
- 严格 base64 解码（`validate=True`），失败 400；
- 单图 ≤ 10MB（沿用 `app.py` 现值）；**新增**单请求输入图总量 ≤ 30MB，超出 400；
- 最多 6 张（超出部分忽略，不报错，与 OpenAI 路径一致）。

### 比例白名单（复审 #3，按上游族独立维护）

初版统一列 10 个比例是错的：`payloads.py:size_from_ratio`（nano 路径）只认 9 个键、
缺省回退 16:9，会造成「尺寸按 16:9、载荷仍发原比例」的自相矛盾请求。按 catalog 实际
支持维护：

| 族 | 允许 aspectRatio |
|---|---|
| nano-banana-2 / pro（gemini-3-pro-image） | `1:1, 16:9, 9:16, 4:3, 3:4`（= RATIO_SUFFIX_MAP） |
| nano-banana-3（gemini-3.1-flash-image） | 上列 5 个 + `1:8, 1:4, 4:1, 8:1`（超长比例） |

实现约束：白名单里的每个比例都**必须**在 `size_from_ratio` 对应档位有精确条目
（已核对：上述比例在 payloads.py 的 nano map 中均存在）。校验在请求解析阶段做，
不落到 payload 层的默默回退。

## 生成与响应

复用 `run_with_token_retries` + `client.generate`（与 chat 路径同一条上游链路与
重试/标记逻辑）。生成图片同样落盘 `generated_dir`（与现有存储核算一致）。

非流式响应：

```json
{
  "candidates": [{
    "content": {"parts": [{"inlineData": {"mimeType": "image/png", "data": "<b64>"}}],
                 "role": "model"},
    "finishReason": "STOP", "index": 0
  }],
  "usageMetadata": { ... 见下 },
  "modelVersion": "<请求的模型名>",
  "responseId": "<uuid>"
}
```

流式（`alt=sse`，复审 #1）：采用**先生成再开流**。Adobe 生图是一次性阻塞调用
（非逐 token），所以：

1. 在**写任何 HTTP 状态/字节之前**完成整个生成（含 token 轮询重试）；
2. 生成**失败** → 走错误码适配层返回真正的 Google 错误（400/404/429/503/500，
   HTTP 状态正确，非 SSE）；
3. 生成**成功** → 此时才 `c.Status(200)` + `Content-Type: text/event-stream`，
   输出单个 `data: <完整响应 JSON>\n\n` 后关流。Gemini 原生 SSE 无 `[DONE]`，
   流结束即完成。

不再发「生成期间 keepalive + 流内 error」——那会先提交 200 再暴露业务失败，
与 sub2api 零改动冲突（其流式分支不识别流内 error 对象，仍返回成功 ForwardResult）。

**权衡（已确认接受）**：生成期间（通常 5–30s）连接无字节下发，极端慢生成有代理空闲
超时风险；一般代理/网关空闲超时 60s+，可接受。`client.generate_timeout` 本身也是上限。

countTokens：`{"totalTokens": <文本 estimate + 输入图 token>,
"promptTokensDetails":[{TEXT},{IMAGE?}]}`，按下方公式，遍历全部 contents，不打 Adobe。

## usageMetadata 画像（复审 #6，单一真源模块 core/models/gemini_usage.py）

所有计费参与值必须精确写死，公式如下（与用户 sub2api 伪装器/回填器实测基准同源）。

**输入侧（三族通用）**：

- `text_in = 提示词 CJK 感知估算`（复用 resolver 的 `_count_text_tokens`：CJK 1 token/字，
  其余 ~4 字符/token，下限 1）。
- `img_in = 输入图张数 × 每张单价`：pro 560、flash 1120。
- `promptTokenCount = text_in + img_in`；
  `promptTokensDetails = [{TEXT: text_in}] (+ {IMAGE: img_in} 当 img_in>0)`。

**输出侧 IMAGE token（按 imageSize 档位，确定值）**：

| 族 | 1K | 2K | 4K |
|---|---|---|---|
| pro（gemini-3-pro-image） | 1120 | 1120 | 2000 |
| flash（gemini-3.1-flash-image） | 1120 | 1680 | 2520 |

**text / thoughts 合成**：

- pro：`text_out` 与 `thoughtsTokenCount` 按档位实测区间**随机整数**（含端点）：
  1K text∈[78,92] thoughts∈[115,140]；2K text∈[80,100] thoughts∈[145,165]；
  4K text∈[92,112] thoughts∈[150,170]。
- flash：`text_out = 0`，`thoughtsTokenCount = 0`（真 flash 生图不产 thoughts，
  与回填器口径一致）。

**装配（三族通用）**：

- `candidatesTokenCount = IMAGE_out + text_out`；
- `candidatesTokensDetails = [{modality:"IMAGE", tokenCount: IMAGE_out}]`；
- `totalTokenCount = promptTokenCount + candidatesTokenCount + thoughtsTokenCount`；
- `serviceTier = "standard"`。

**罐头文本模型（测活白名单）的 usage（确定值，不随机）**：
`promptTokenCount = text_in`（对测活提示词估算）、`candidatesTokenCount = 罐头文本
token 估算`（对固定短文本用同一 `_count_text_tokens`，写死一个常量如 12）、
`thoughtsTokenCount = 0`、无 IMAGE 明细、`serviceTier = "standard"`。

随机源做成可注入函数变量（`var gemini_usage_rand`），测试固定种子保证确定性。

## 错误处理

见「错误码适配层」表。要点：所有失败都在 **SSE 开流前** 决定（先生成再开流），
因此永远能返回正确 HTTP 状态 + Google 错误结构；不存在「流内 error」路径。
Token 配额尽→429，上游临时错误→503，参数/JSON/无文本→400，未知 model/action→404，
鉴权→401，其它→500。

## 测试

离线单测（`tests/test_gemini_native.py`）：

- 路径拆分与 action 校验；鉴权（头/query/缺失 → 401）；
- 请求解析（#2）：`contents[]` 多轮 + `systemInstruction` 文本全部拼入 prompt、
  跨 turn 收集 inlineData；`content`（错字段/缺 contents）→ 400；
- 画像合成（#6）：pro 各档 IMAGE 确定值、text/thoughts 落区间（固定种子）、total 自洽；
  flash 各档 1120/1680/2520 且 thoughts=0；输入图 pro 560、flash 1120 每张；
  罐头 usage 确定值；
- 比例白名单（#3）：pro 允许 5 个、flash 允许 9 个；给 pro 传 `1:8` → 400；
  给某族传其白名单外比例 → 400（不回退 16:9）；默认 1:1；
- imageSize：默认 1K；`0.5K`/非法 → 400；
- 测活白名单（#5）：`gemini-2.0-flash` 返回罐头文本且不调用上游；未知模型名 → 404；
- 错误码适配（#4）：桩 QuotaExhaustedError→429、UpstreamTemporaryError→503、
  AuthError→401，且响应体是 Google `{error:{code,message,status}}` 非 `{detail}`；
- 输入图限额（#7）：>10MB 单图→400、总量>30MB→400、非白名单 MIME→400、坏 base64→400。

in-process E2E（打桩 `client.generate`/`upload_image`）：

- 非流式成功：结构完整、`modelVersion` 回显、base64 可解码、usageMetadata 自洽；
- 流式成功（#1）：先生成后开流，输出单个完整 `data:` chunk、无 keepalive、无 [DONE]；
- 流式失败（#1）：桩 generate 抛 UpstreamTemporaryError → 返回 **HTTP 503 + Google 错误**，
  未进入 SSE（Content-Type 非 event-stream）；
- 测活路径：`gemini-2.0-flash` streamGenerateContent 返回罐头文本 SSE。

## 不做（YAGNI）

- OAuth/Code Assist 包裹格式（用户只用 APIKey 账号型）；
- `0.5K` 尺寸档；`fileData`（URL 引用）输入图；多候选 `candidateCount>1`；
- sub2api 侧任何改动。
