# /v1/images/edits（OpenAI 兼容图生图）设计

日期：2026-07-17

## 背景

new-api（sparkcode）渠道 #185 把 `gpt-image-2` 指向本服务，但本服务只有
`/v1/images/generations`，没有 `/v1/images/edits`。下游用户（如 silence）
的图生图/改图请求打到 185 得到 404 空响应；new-api 重试到下一渠道时
multipart 请求体已被消费，报 `read multipart body: multipart: NextPart: EOF`，
最终失败。上游 Firefly 的图生图能力已存在：`/v1/chat/completions` 里
`_load_input_images → client.upload_image → client.generate(source_image_ids=...)`。

## 目标

新增 `POST /v1/images/edits`（multipart/form-data，OpenAI images/edits 兼容），
复用现有上传+生成链路，使渠道 185 直接服务图生图请求。

## 接口行为

- 鉴权：同现有端点（`require_service_api_key`）。
- 表单字段：
  - `prompt`：必填，缺失返回 400。
  - 图片：兼容 `image`、`image[]`、`image[N]` 字段名；至少 1 张、最多 6 张
    （与 chat 路径一致）；单张 ≤10MB；mime 按 `_normalize_image_mime` 规则
    归一（jpeg/png/webp，其余按 jpeg）。
  - `model`：可选，默认走 `resolve_model`/`resolve_ratio_and_resolution` 的
    默认模型逻辑；`gpt-image-2` 等动态模型由 `size`/`quality` 自适应
    比例与 1K/2K/4K 档。视频模型返回 400。
  - `mask`:接受但忽略（Firefly 无 mask 能力），日志记一行。
  - `response_format`：`url`（默认）或 `b64_json`；其他值 400。
  - `n`、`background` 等其余字段忽略，恒返回 1 张。
- 响应：`{created, model, data: [{url|b64_json}], usage}`；
  `usage = build_image_usage(prompt, resolution, ratio, 输入图张数)`
  （每张输入图计 300 token）。
- 错误映射与 generations 端点一致：429/401/503/HTTPException/500 →
  OpenAI error JSON。except 链抽成共享 helper，仅新端点使用，
  现有三个端点不动（避免回归）。

## 实现要点

- 路由在 `api/routes/generation.py`，`async def` + `await request.form()`
  手动解析（兼容多种图片字段名）；阻塞的生成流程经
  `starlette.concurrency.run_in_threadpool` 执行，不阻塞事件循环。
- `build_generation_router` 新增注入参数 `normalize_image_mime`
  （app.py 传 `_normalize_image_mime`）。
- 重试走 `run_with_token_retries(operation_name="images.edits")`。
- 新依赖：`python-multipart`（锁版本），FastAPI 解析 form-data 必需。
- 请求日志/积分集成（复查补漏）：`_resolve_request_operation` 增加
  `/v1/images/edits → images.edits` 映射，否则 `should_log=False`，
  请求既不进管理后台日志，credits_tracker 也不会结算。
  中间件对该路径跳过 body 缓冲与 JSON 字段提取（multipart 解析不出
  且最多白缓冲 60MB），model/prompt 由路由内
  `set_request_logging_fields` 主动上报（新增注入参数）。

## 测试

`tests/` 冒烟风格：edits 正常路径（200、url 可取）、`image[]` 字段名、
`b64_json`、缺 prompt→400、缺图→400、带 mask 不报错。

## 部署

build-and-push.sh 构建推 ACR → netcup 拉新镜像重启 adobe2api →
以 silence 的调用路径（new-api 渠道 185 → /v1/images/edits）回归验证。
new-api 的 multipart 重试缺陷仍在，但 185 成功后不再触发重试。
