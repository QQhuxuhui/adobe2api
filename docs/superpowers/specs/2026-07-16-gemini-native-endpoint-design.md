# adobe2api Gemini 原生协议入口（接入 sub2api gemini 类账号）

日期：2026-07-16
状态：设计已确认

## 背景

用户的 sub2api 按平台分组：买家走 Gemini 原生协议（`generateContent`）的售卖池只调度
gemini 类账号，gemini APIKey 账号要求上游讲 Gemini 原生协议。adobe2api 目前只有
OpenAI 协议，接不进这个池子。本设计给 adobe2api 加一个 Gemini 原生入口，让 Firefly
的 nano-banana 产能进入现有 gemini 售卖池。

已确认的 sub2api 对接合同（读 fork 源码 `gemini_messages_compat_service.go` 得出）：

- 上游调用：`POST {base_url}/v1beta/models/{mappedModel}:{action}`，
  action ∈ {generateContent, streamGenerateContent, countTokens}，流式加 `?alt=sse`；
- 鉴权：`x-goog-api-key: <api_key>` 头；账号级模型映射可用（恒等映射即可）；
- 账号测活：`streamGenerateContent`，默认测试模型为文本模型 `gemini-3-pro-preview`；
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
错误一律 Google 风格 `{"error":{"code":<int>,"message":...,"status":...}}`：
401 UNAUTHENTICATED、400 INVALID_ARGUMENT、404 NOT_FOUND、429 RESOURCE_EXHAUSTED、
500 INTERNAL。

## 模型映射

| 请求模型名 | 上游 modelId/modelVersion | usage 画像 |
|---|---|---|
| `gemini-3-pro-image`、`gemini-3-pro-image-preview` | `gemini-flash` / `nano-banana-2` | pro |
| `gemini-3.1-flash-image`、`gemini-3.1-flash-image-preview` | `gemini-flash` / `nano-banana-3` | flash |
| 其它任意模型名（如测活的 `gemini-3-pro-preview`） | 不打 Adobe，罐头文本响应 | 文本小额 |

罐头文本：`generateContent`/`streamGenerateContent` 对非图像模型返回一段固定短文本
（正常 candidates 文本结构 + 小额 usageMetadata），使 sub2api 测活零成本、不烧积分。
渠道上只配图像模型名，文档写明此行为。

`GET /v1beta/models` 列出上表 4 个图像模型：`name: "models/<id>"`、`displayName`、
`supportedGenerationMethods: ["generateContent","streamGenerateContent","countTokens"]`。

## 请求解析

- prompt：最后一条 `role:"user"` 的 `content.parts[]` 中 text 拼接；无文本时 400。
- 输入图：同一条 user content 的 `inlineData`（兼容 snake_case `inline_data`）parts，
  base64 + mimeType，最多 6 张，复用 `client.upload_image` 得 source_image_ids。
- `generationConfig.imageConfig.aspectRatio`：白名单 = 该族与 Adobe SUPPORTED_RATIOS
  的交集（1:1, 2:3, 3:2, 3:4, 4:3, 4:5, 5:4, 9:16, 16:9, 21:9）；缺省 `1:1`；
  非法值 400 INVALID_ARGUMENT。
- `imageConfig.imageSize`：`1K`/`2K`/`4K`（大小写不敏感）；缺省 `1K`（Google 官方
  默认）；其它值（含 0.5K，Adobe 无此档）400。

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

流式（`alt=sse`）：生成期间每 15s 输出一行 SSE 注释 `: keepalive\n\n`（标准解析器
忽略，防空闲超时）；完成后输出一个 `data: <完整响应 JSON>\n\n` chunk 后关流。
Gemini 原生 SSE 无 [DONE] 终止符，流结束即完成。

countTokens：`{"totalTokens": <文本 CJK 估算 + 输入图按族每张 token>,
"promptTokensDetails": [...]}`，不打 Adobe。

## usageMetadata 画像（单一真源模块 core/models/gemini_usage.py）

数值与用户 sub2api 伪装器/回填器同源（实测基准）：

| 族 | IMAGE 输出 token（1K/2K/4K） | 输入图 token/张 | thoughts/text 合成 |
|---|---|---|---|
| pro | 1120 / 1120 / 2000 | 560 | text、thoughts 按档位实测区间随机：1K 78–92 / 115–140，2K 80–100 / 145–165，4K 92–112 / 150–170 |
| flash | 1120 / 1680 / 2520 | 1120 | 不合成 thoughts（与真 flash 及回填器口径一致），text 并入 candidates 少量 |

结构规律（与真 pro 一致）：

- `candidatesTokensDetails` 仅一项 `{modality:"IMAGE", tokenCount}`；
- `promptTokensDetails`：TEXT 一项 + 有输入图时 IMAGE 一项；
- `candidatesTokenCount = IMAGE + text`；
- `totalTokenCount = promptTokenCount + candidatesTokenCount + thoughtsTokenCount`；
- `serviceTier: "standard"`。

随机源做成可注入函数变量（测试固定种子）。

## 错误处理

- Token 池配额尽：429 RESOURCE_EXHAUSTED；上游临时错误经重试仍失败：502/500 INTERNAL；
- 请求体非法 JSON / 无 user 文本 / 参数非法：400 INVALID_ARGUMENT；
- 未知 action：404；鉴权失败：401 UNAUTHENTICATED。
- keepalive 期间生成失败：SSE 流里输出 `data: {"error":{...}}` 后关流
  （Gemini 流式错误惯例）。

## 测试

离线单测（`tests/test_gemini_native.py`）：

- 路径拆分与 action 校验；鉴权（头/query/缺失）；
- 画像合成：pro 各档 IMAGE 确定值、text/thoughts 落区间（固定种子）、total 自洽；
  flash 各档 1120/1680/2520；输入图 pro 560、flash 1120 每张；
- imageSize=0.5K 返回 400（Adobe 无此档，不虚报）；
- aspectRatio/imageSize 校验、默认 1:1/1K；
- 罐头文本对非图像模型生效且不调用上游；countTokens 估算。

in-process E2E（打桩 `client.generate`/`upload_image`）：

- 非流式：结构完整、`modelVersion` 回显、base64 可解码；
- 流式：keepalive 注释行 + 末 chunk 完整、无 [DONE]；
- 测活路径：文本模型 streamGenerateContent 返回罐头文本。

## 不做（YAGNI）

- OAuth/Code Assist 包裹格式（用户只用 APIKey 账号型）；
- `0.5K` 尺寸档；`fileData`（URL 引用）输入图；多候选 `candidateCount>1`；
- sub2api 侧任何改动。
