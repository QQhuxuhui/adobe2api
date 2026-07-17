# new-api 视频任务协议入口（Sora + Veo）设计文档

日期：2026-07-17
状态：已确认（new-api 直连；仅 Sora/Veo；旧后缀模型保留共存）

## 背景与目标

adobe2api 当前只通过 `/v1/chat/completions` 和后缀模型 ID 提供视频生成，例如
`firefly-sora2-4s-16x9`、`firefly-veo31-8s-16x9-1080p`。本设计新增 new-api 当前
任务适配器能够直接调用的两套异步协议入口：

- Sora 任务适配器：`POST /v1/videos`、`GET /v1/videos/{id}`、
  `GET /v1/videos/{id}/content`；
- Gemini 任务适配器：`POST /v1beta/models/{model}:predictLongRunning`、
  `GET /v1beta/{operationName}`。

这里的“兼容”以本机 new-api fork 当前实现为准，是 OpenAI/Google 协议的受控子集，
不是完整官方能力声明。OpenAI 已宣布 Videos API 和 Sora 2 于 2026-09-24 下线，但
new-api 的 Sora 任务适配器仍把这套协议作为渠道契约；本服务背后调用 Adobe，不依赖
OpenAI Videos API 的存续。

官方与本地依据：

- [OpenAI 视频生成](https://developers.openai.com/api/docs/guides/video-generation)
- [OpenAI 弃用公告](https://developers.openai.com/api/docs/deprecations/)
- [Google Veo 3.1](https://ai.google.dev/gemini-api/docs/veo)
- 本机 new-api `relay/channel/task/sora` 与 `relay/channel/task/gemini` 适配器

## 方案选择

评估过三种方案：

1. **适配当前 new-api fork（选定）**：不修改 new-api，按其请求、轮询和下载行为实现，
   对 Adobe 不支持的官方参数返回明确 400；
2. 同时升级 new-api 到 2026 年最新 OpenAI/Google 完整协议：需要跨仓库修改计费、参数
   枚举和下载代理，且 Adobe 不支持全部能力，本期不做；
3. 继续只使用 chat 后缀模型：改动最少，但无法作为 new-api 原生视频任务渠道，本期不选。

链路采用 new-api 直连 adobe2api，不经过 sub2api。生产地址必须使用 HTTPS 或受信任
私网地址，禁止通过公网明文 HTTP 传输渠道密钥和提示词。

## 范围

- 做：`sora-2`、`sora-2-pro`、`veo-3.1-generate-preview`、
  `veo-3.1-fast-generate-preview` 四个模型；共享异步任务内核；任务日志与积分测量；
- 不做：Kling 官方协议、图生视频、Veo 4K、Sora 16/20 秒、视频扩展/编辑、webhook；
- 保留：现有 chat 视频模型和生成行为；
- 所有未支持的非空媒体/安全参数必须返回 400，不能静默忽略。

## 架构

```text
new-api Sora  ─┐                         ┌─ VideoTaskStore（JSONL）
               ├─ 协议路由 ─ VideoTaskManager ─ 有界任务准入
new-api Gemini ┘                         └─ 2 个 worker
                                                │
                                    VideoGenerationService
                                                │
                              Token 重试 + AdobeClient.generate_video
                                                │
                           generated_dir + 请求日志 + 积分测量
```

### `core/video_generation.py`（新）

提取 `generate_video_file(...)`，负责现有 chat 路由与新任务 worker 共同需要的部分：

- 调用 `AdobeClient.generate_video`；
- 使用 `.video.tmp` 下载，成功后按响应 MIME 确定 mp4/webm/ogv 扩展名并原子改名；
- 失败时删除临时文件；
- 调用现有生成文件容量记账和清理回调；
- 返回真实 `result_path`、`result_mime` 和上游元数据。

Token 选择/重试、协议状态和日志不放入该函数，避免它依赖 HTTP `Request`。

### `core/video_tasks.py`（新）

`VideoTaskRecord` 持久化字段：任务 ID、protocol、model、prompt_preview、engine、duration、
aspect_ratio、resolution、requested_size、status、progress、error_code、error_message、
result_path、result_mime、result_url、log_id、created_at、started_at、completed_at。完整
prompt 和 negative prompt 仅保存在等待执行的内存 `VideoTaskSpec` 中，不写 JSONL。

`VideoTaskStore`：

- 内存字典 + `data/video_tasks.jsonl` append/upsert；
- 启动时按 ID 取最后状态，坏行忽略；
- 定期压缩为最多 500 个终态任务，queued/in_progress 任务不得被淘汰；
- 进程启动时把遗留 queued 和 in_progress 都标记 failed，错误码
  `service_restarted`，本期不恢复执行；
- 所有读写由锁保护，持久化失败不得让内存状态伪装成成功提交。

`VideoTaskManager`：

- `ThreadPoolExecutor(max_workers=2)`；
- 通过非阻塞 `BoundedSemaphore(22)` 将“执行中 + 排队”限制为 22 个，满时提交返回 429；
- 提交成功后才返回任务对象；worker 结束必须释放准入槽；
- 应用 shutdown 时停止接收任务，把尚未开始的任务标记为 failed，再执行
  `shutdown(cancel_futures=True)`；
- GET 必须同时校验任务 protocol 和 model，跨协议或路径模型不匹配按 404 处理。

worker 使用与现有生成相同的 Token 轮换、配额、鉴权恢复和临时错误重试策略，但不复用
已经结束的 HTTP `Request`。任务提交日志先记录 queued；worker 完成后使用相同 log_id
显式 upsert 最终状态、Token 信息、预览 URL，并调用 `CreditsTracker.complete` 测量积分。
失败任务调用 `CreditsTracker.finish(..., completed=False)`，不能产生成功积分记录。

积分 cost key 复用相同参数对应的现有后缀模型 ID，日志展示模型仍为官方模型名。

## Sora 协议入口

新建 `api/routes/openai_videos.py`：

### `POST /v1/videos`

- 接受 JSON 与 multipart/form-data，解析前请求体最大 1 MiB；
- `model`：`sora-2` 或 `sora-2-pro`；
- `prompt`：必填非空；
- `seconds`：字符串或数字，支持 4/8/12，默认 4；
- `size`：
  - 两个模型：`1280x720`、`720x1280`，映射 Adobe 720p；
  - 仅 pro：`1792x1024`、`1024x1792`，这是当前 new-api fork 的旧枚举，分别映射
    Adobe 1080p 的实际 1920x1080、1080x1920；状态对象仍回显请求的 size；
- `input_reference`、`characters` 或其他媒体输入非空时返回 400
  `unsupported_parameter`。

成功固定返回 HTTP 200 和 OpenAI video object：

```json
{
  "id": "video_<hex>",
  "object": "video",
  "model": "sora-2",
  "status": "queued",
  "progress": 0,
  "seconds": "4",
  "size": "1280x720",
  "created_at": 0
}
```

### 查询与下载

- `GET /v1/videos/{id}`：仅返回 protocol=openai 的任务；不存在返回 OpenAI 404；
- `GET /v1/videos/{id}/content`：completed 时按真实 MIME `FileResponse`；queued/
  in_progress 返回 409，failed 返回 424，文件已清理返回 404；
- 鉴权使用 Bearer 或 X-API-Key，复用服务 `api_key`；
- 错误体统一为 `{"error":{"message","type","code"}}`。

## Gemini Veo 协议入口

扩展 `api/routes/gemini_native.py`，为 `GeminiModelSpec` 增加模型级 supported actions，
避免 video 模型落入现有图片 `generateContent` 分支：

- `veo-3.1-generate-preview` → `veo31-standard`；
- `veo-3.1-fast-generate-preview` → `veo31-fast`；
- 两者只支持 `predictLongRunning`，模型列表返回相同 generation method；
- 对 video 模型调用 generateContent/countTokens/streamGenerateContent 返回 404。

### `POST /v1beta/models/{model}:predictLongRunning`

- JSON 请求体最大 1 MiB；
- `instances` 必须恰好一个元素，`instances[0].prompt` 必填非空；
- image/video/referenceImages/lastFrame 等媒体输入出现即返回 400；
- `parameters.aspectRatio`：16:9/9:16，默认 16:9；
- `parameters.durationSeconds`：4/6/8，默认 8；
- `parameters.resolution`：720p/1080p，默认 720p；1080p 仅允许 8 秒；4K 因 Adobe
  当前链路不支持而明确返回 400；
- `negativePrompt` 保存到任务并透传 Adobe Veo 参数；
- 非空 `personGeneration` 返回 400 `Unsupported parameter: personGeneration`，不静默
  改变调用方安全语义；
- camelCase 和 snake_case 均兼容。

响应固定 HTTP 200：

```json
{"name":"models/{model}/operations/{op_id}"}
```

### `GET /v1beta/models/{model}/operations/{op_id}`

- 必须匹配 protocol=veo 且任务 model 与路径 model 相同；
- 进行中：`{name, done:false, metadata:{progressPercent}}`；
- 成功：`response.generateVideoResponse.generatedSamples[0].video.uri` 指向真实扩展名的
  HTTPS/私网生成文件 URL；
- 失败：`{name, done:true, error:{code:13,message}}`；
- 不存在返回 Google 404 错误体；
- 鉴权使用现有 `x-goog-api-key`。

new-api 的 Gemini 下载代理会给结果 URI 追加渠道 key。返回 URI 预置无敏感值的
`key=proxy` 查询参数，同时 new-api 仍发送 x-goog-api-key 请求头，从而避免真实渠道密钥
出现在下载 URL 和访问日志中。

## 日志与中间件

扩展请求操作识别：

- `videos.create`、`videos.get`、`videos.content`；
- `gemini.predictLongRunning`、`gemini.operations.get`。

submit 路由设置“外部管理日志”标志，由任务管理器先使用任务 log_id 写 queued 记录，
中间件不再为该请求重复写终态；worker 最后覆盖同一记录。轮询/下载请求可以独立记录，
但不得覆盖提交任务的生成结果记录。日志只保存 prompt preview，不保存完整 prompt、
negative prompt 或请求媒体内容。

## new-api 计费与部署

配置两个渠道，base URL 使用 `https://adobe2api.example.com` 或私网服务名：

- Sora 渠道：模型 `sora-2`、`sora-2-pro`；
- Gemini 任务渠道：模型 `veo-3.1-generate-preview`、
  `veo-3.1-fast-generate-preview`，Gemini version 保持 `v1beta`。

本机 new-api 对 Sora 默认按
`model_price * seconds * size倍率` 计费，高尺寸倍率为 1.666667。因此推荐把 Sora
`model_price` 配为每秒基础价，不设置 `TASK_PRICE_PATCH`。只有明确希望固定按次收费时，
才把模型加入 `TASK_PRICE_PATCH`，此时 model_price 才是整次价格。

Gemini 任务适配器目前没有把 durationSeconds 写入 PriceData，按 model_price 固定单次
收费。若不改 new-api，只能按业务选定的统一单次价格配置，不能声称按秒精确计费。

## 错误处理

- 参数/协议错误：提交前返回各协议 400；
- 准入容量满或任务存储失败：不创建半任务，返回 429 或 500；
- Token 配额、鉴权和临时错误：按现有策略换号重试；最终失败写任务、日志和协议错误；
- 生成失败：删除 `.video.tmp`；
- 文件被容量清理后：任务状态保持 completed，但 content 返回明确 404；
- 关闭/重启：未开始和执行中任务最终可观察为 failed，不永久停在 queued。

## 测试计划

新增 `tests/test_video_tasks.py`、`tests/test_openai_videos.py`，扩展
`tests/test_gemini_native.py`、日志和积分测试：

- 参数矩阵、默认值、JSON/multipart、1 MiB 限制和不支持字段；
- Sora 720/高尺寸映射以及 Veo 1080p 必须 8 秒；
- queued → in_progress → completed/failed，全协议状态与真实 MIME 下载；
- 有界队列满返回 429、槽位释放、并发更新；
- JSONL 去重加载、坏行、压缩、queued/in_progress 重启失败；
- protocol/model 归属校验和跨协议 ID 拒绝；
- 临时文件失败清理、生成文件被清理后的 404；
- submit 最终日志 upsert、Token 信息与积分完成/失败路径；
- Gemini 每模型 action 白名单和日志 operation/model 解析；
- 返回 Veo URI 不泄露真实渠道 key；
- 现有 chat 视频路径回归；
- new-api 两类渠道真实 smoke test：提交、轮询、new-api content 代理下载。

## 验收标准

- new-api 无源码改动即可完成 Sora 和 Veo 文生视频全链路；
- 所有任务在成功、失败、容量满和重启后都有确定终态；
- 后台生成最终日志包含预览、Token 和积分消耗；
- 不通过公网 HTTP 或结果 URI 泄露渠道密钥；
- 现有 chat 视频和图片接口行为不回归；
- 专项测试与完整 pytest 通过。
