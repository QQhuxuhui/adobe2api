# 官方协议视频入口(sora + veo)— 设计文档

日期:2026-07-17
状态:已确认(new-api 直连;仅 sora/veo;旧后缀模型保留共存)

## 背景与目标

adobe2api 目前视频生成只有 `/v1/chat/completions` + 后缀式模型 ID(`firefly-sora2-4s-16x9`、`firefly-veo31-8s-16x9-1080p` 等几十个)。用户希望像生图模型(`gpt-image-2`、`gemini-3.1-flash-image`)一样,用**官方模型名 + 官方协议参数**(时长/分辨率/比例)接入 new-api 视频渠道。

调研结论(2026-07-17,依据本机 new-api fork 源码):

- new-api 视频是任务式中继:`POST /v1/videos`(sora 适配器,原样透传请求体,Bearer 鉴权)、`GET /v1/videos/{id}` 轮询、`GET /v1/videos/{id}/content` 拉取内容;Gemini 任务适配器走 `POST {base}/v1beta/models/{m}:predictLongRunning` + `GET {base}/v1beta/{operationName}` 轮询(x-goog-api-key 鉴权),完成后取 `response.generateVideoResponse.generatedSamples[0].video.uri` 作为下载地址(普通 GET 可取即可)。
- new-api 定价源 `basellm ratio_config-v1-base.json` **没有任何视频模型条目**,视频定价需在 new-api 侧按次(`model_price`)自定义。
- 链路决策:视频渠道 new-api **直连** adobe2api,不经 sub2api(免改造;任务式短请求也天然规避反代 30s 超时)。

## 范围

- 做:OpenAI Video API(sora-2 / sora-2-pro)、Gemini Veo(veo-3.1-generate-preview / veo-3.1-fast-generate-preview)两套入口;共享异步任务内核。
- 不做:kling 官方协议(官方 model_name 命名不对齐、JWT 鉴权复杂,继续走 chat 老入口);图生视频(首版仅文生视频,sora `input_reference`/veo image 参数返回 400 明确报错);旧后缀模型的废弃(保留共存)。

## 架构

```
new-api(sora渠道)   → POST /v1/videos ─────────────┐
                      GET  /v1/videos/{id}          │      ┌──────────────────┐
                      GET  /v1/videos/{id}/content  ├────→ │ core/video_tasks │→ 现有生成内核
new-api(gemini渠道) → POST /v1beta/models/{m}:      │      │ VideoTaskStore   │  (run_with_token_retries
                           predictLongRunning       │      │ + 线程池 worker  │   + adobe_client 视频链路)
                      GET  /v1beta/models/{m}/      │      └──────────────────┘
                           operations/{op_id} ──────┘
```

两个协议入口只是同一任务内核的两张协议皮;生成逻辑零改动。

## 组件设计

### core/video_tasks.py(新)

- `VideoTaskRecord`:id、protocol(openai/veo)、model(官方名)、prompt、engine、duration、aspect_ratio、resolution、status(queued/in_progress/completed/failed)、progress(0-100)、error_code/error_message、result_path(本地 mp4)、created_at/completed_at。
- `VideoTaskStore`:内存 dict + JSONL 持久化(`data/video_tasks.jsonl`,append + 启动加载,上限 500 条截断,沿用 RequestLogStore 模式);重启后历史任务可查态,`in_progress` 的启动时标记 failed(worker 不恢复)。
- worker:`ThreadPoolExecutor`(并发数常量 2,后续有需要再配置化),执行现有 chat 视频生成路径完全相同的调用(含 token 重试、进度回调更新 progress、请求日志);产物写入 generated_dir,复用现有文件服务与 `public_image_url`。
- 任务提交接口:`submit(protocol, model, prompt, engine, duration, aspect_ratio, resolution) -> VideoTaskRecord`。

### api/routes/openai_videos.py(新)

- `POST /v1/videos`:Content-Type 为 JSON 或 multipart/form-data 均接受(new-api 原样透传下游客户端的 multipart);字段:
  - `model`:`sora-2` | `sora-2-pro`,必填;
  - `prompt`:必填非空;
  - `seconds`:字符串或数字,4/8/12,默认 4;
  - `size`:`1280x720`/`720x1280`(两模型),`1792x1024`/`1024x1792`(仅 sora-2-pro,分别落 16:9/9:16),默认 `1280x720`;
  - `input_reference` 出现即 400(`unsupported_parameter`)。
- 响应与状态对象(OpenAI video object):`{id:"video_<hex>", object:"video", model, status, progress, seconds, size, created_at, completed_at?, error?:{code,message}}`;status 枚举 queued/in_progress/completed/failed(new-api ParseTaskResult 亦认 pending/processing,不使用)。
- `GET /v1/videos/{id}`:任务不存在返回 404 OpenAI 错误体。
- `GET /v1/videos/{id}/content`:completed 时以 `FileResponse`(video/mp4)返回本地文件;未完成 409,失败 424,不存在 404。
- 鉴权:Bearer / X-API-Key,复用现有 `api_key` 校验;错误体用 OpenAI 风格 `{"error":{"message","type","code"}}`。

### Gemini Veo 入口(扩展 api/routes/gemini_native.py)

- 模型注册:`veo-3.1-generate-preview`(engine veo31-standard)、`veo-3.1-fast-generate-preview`(engine veo31-fast),family="video";出现在 `/v1beta/models` 列表,`supportedGenerationMethods: ["predictLongRunning"]`;对这两个模型请求 generateContent 返回 404(现有行为)。
- `POST /v1beta/models/{model}:predictLongRunning`:
  - body:`instances`(非空数组,取 [0].prompt,必填非空)、`parameters{aspectRatio 16:9|9:16(默认16:9), durationSeconds 4|6|8(默认8,数字), resolution 720p|1080p(默认720p), negativePrompt(透传), personGeneration(忽略)}`;字段名 camel/snake 均收(复用 `_config_field`);image 输入出现即 400。
  - 响应:`{"name":"models/{model}/operations/{op_id}"}`。
- `GET /v1beta/models/{model}/operations/{op_id}`(FastAPI 路由按 `{model}/operations/{op}` 匹配,即 new-api 拼的 `{base}/v1beta/{operationName}`):
  - 进行中:`{name, done:false, metadata:{progressPercent}}`;
  - 成功:`{name, done:true, response:{"@type":"...GenerateVideoResponse", generateVideoResponse:{generatedSamples:[{video:{uri:<public_image_url 的 mp4 地址>}}]}}}`;
  - 失败:`{name, done:true, error:{code:13, message}}`;
  - 不存在:404(google 错误体)。
- 鉴权:`x-goog-api-key`(现有 `require_api_key`)。

## 模型映射表

| 官方名 | 官方参数 | Adobe 内部 |
|---|---|---|
| sora-2 | seconds 4/8/12;size 1280x720→16:9,720x1280→9:16 | upstream `openai:firefly:colligo:sora2`,duration,ratio |
| sora-2-pro | 同上,另收 1792x1024→16:9、1024x1792→9:16 | upstream `openai:firefly:colligo:sora2-pro` |
| veo-3.1-generate-preview | durationSeconds 4/6/8;resolution 720p/1080p;aspectRatio 16:9/9:16 | engine veo31-standard,`google:firefly:colligo:veo31` |
| veo-3.1-fast-generate-preview | 同上 | engine veo31-fast,`google:firefly:colligo:veo31-fast` |

非法组合(sora seconds=6、veo durationSeconds=12、不认识的 size 等)返回各协议标准 400,错误信息指明合法取值。

## new-api 侧配置(文档交付)

- 渠道①:类型 sora,base_url=`http://159.195.13.15:6001`,密钥=服务 api_key,模型 `sora-2`,`sora-2-pro`。
- 渠道②:类型 gemini(任务/视频),同 base_url,模型 `veo-3.1-generate-preview`,`veo-3.1-fast-generate-preview`。
- 定价:`model_price` 按次自定义(basellm 表无视频条目,`seconds` 不参与 new-api 计费)。参考官方牌价折算按次:sora-2 ≈ $0.10/s、sora-2-pro ≈ $0.30–0.50/s、veo-3.1 ≈ $0.40/s、veo-3.1-fast ≈ $0.15/s;建议按 12s/8s 最长档定价保底。

## 错误处理

- 上游配额/失效:沿用 run_with_token_retries 的重试与换号;最终失败落任务 failed + 映射协议错误体(sora `error.code`:`quota_exceeded`/`generation_failed`;veo `error.message`)。
- 任务表满/worker 队列满:提交即 429(协议各自错误体)。
- content 在文件被清理(现有存储清理机制)后请求:404 + 明确信息。

## 测试计划

pytest(新增 `tests/test_openai_videos.py`、扩展 `tests/test_gemini_native.py`):

- 参数校验矩阵:model/seconds/size/durationSeconds/resolution/aspectRatio 合法与非法值、默认值、snake_case 兼容、multipart 提交、input_reference/image 拒绝。
- 任务态机:mock 生成内核,queued→in_progress→completed/failed 全链;operations 响应三态;content 端点 200/409/424/404。
- 映射:官方参数 → engine/duration/ratio/resolution 断言。
- 持久化:JSONL 重启加载、in_progress 标记 failed、截断。

实测:new-api 配两个渠道走通 sora-2 与 veo-3.1 全链路(提交→轮询→取片)。

## 明确不做(YAGNI)

- kling 官方协议、图生视频、视频 remix/续写、webhook 回调、按秒计费上报(new-api 不支持)。
