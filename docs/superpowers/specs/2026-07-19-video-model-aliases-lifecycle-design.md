# 视频通用模型映射与任务生命周期修复设计

日期：2026-07-19
状态：已确认

## 背景与目标

当前 Adobe 视频目录主要暴露带时长、比例和分辨率后缀的模型 ID，例如
`firefly-veo31-8s-16x9-1080p`。new-api 的任务协议使用通用模型名：Sora 使用
`sora-2` / `sora-2-pro`，Gemini Veo 使用 `veo-3.1-generate-preview` /
`veo-3.1-fast-generate-preview`。本次改造让直连 `/v1/chat/completions`、`/v1/models`
和两个异步协议共享同一套通用模型语义，并保留旧后缀模型兼容。

同时修复上一轮审查发现的任务持久化、重启、关机、参数静默忽略和下载路径问题。

## 方案

视频目录增加动态基础模型别名，由统一解析器根据请求参数生成实际的视频配置。旧
`firefly-*` ID 继续存在并可调用；通用别名和旧 ID 都可由 `/v1/models` 返回，避免
破坏已有直连客户端。

动态解析结果至少包含：`engine`、`upstream_model`、`duration`、`aspect_ratio`、
`resolution` 和用于积分的具体 Adobe 模型 ID。请求日志保留用户传入的通用模型名，
积分使用具体后缀 ID。

## 模型映射

### Sora

公共模型为 `sora-2` 和 `sora-2-pro`。

- `seconds` 或 `duration` 支持 `4`、`8`、`12`，默认 `4`；
- `size` 支持 `1280x720`、`720x1280`，默认 `720x1280`，对应 Adobe `720p`；
- Pro 额外支持 `1792x1024`、`1024x1792`，对应 Adobe `1080p`；
- 比例由 `size` 计算，积分 ID 形如 `firefly-sora2-8s-16x9` 或
  `firefly-sora2-pro-12s-9x16`；
- 非 Pro 使用高分辨率尺寸、非法时长或非法尺寸均在提交前返回 400。

### Veo

公共模型为 `veo-3.1-generate-preview` 和 `veo-3.1-fast-generate-preview`。

- `durationSeconds` / `duration` 支持 `4`、`6`、`8`，默认 `8`；
- `aspectRatio` / `aspect_ratio` 支持 `16:9`、`9:16`，默认 `16:9`；
- `resolution` 支持 `720p`、`1080p`，默认 `720p`；1080p 只允许 8 秒；
- 标准版映射为 `firefly-veo31-{duration}s-{ratio}-{resolution}`，Fast 版映射为
  `firefly-veo31-fast-{duration}s-{ratio}-{resolution}`；
- `negativePrompt` 仍然透传 Adobe，未支持的媒体、安全或生成参数非空时返回 400。

Gemini 原生 `predictLongRunning` 已经使用上述两个公共模型名，提交任务时直接复用
解析结果；轮询名称仍以 Gemini operation name 为准。

## 请求与错误处理

- Sora 和 Veo 的 JSON/multipart 入口在解析阶段拒绝非空的未知媒体、安全参数；不再
  把 `metadata.input.media`、`seed`、`safetySettings`、`generateAudio` 等未支持字段
  静默忽略；
- `NaN`、`Infinity` 等非有限数值按参数错误返回 400；
- Sora 省略尺寸时统一采用 `720x1280`，与 new-api 的默认计费语义一致；
- 下载只允许读取 `generated_dir` 下的已生成视频文件，并校验解析后的路径没有越界。

## 任务生命周期与持久化

- worker 的状态转换和最终状态写入都在可恢复的异常处理范围内；状态写入失败时执行
  最佳努力的失败收尾，不让 future 异常静默留下永久 queued/in_progress；
- 启动恢复会把遗留 active 任务写成 `service_restarted`，同时将对应请求日志更新为
  FAILED，并删除该任务的 `.video.tmp` 和未提交的最终视频文件；
- 关机停止接收新任务，取消未开始任务，并等待运行中 worker 完成后再关闭积分 tracker，
  保证最终日志和积分测量不会写入已停止的队列；
- 生成成功后，日志或积分写入异常不应把已经存在的生成文件伪装成生成失败；任务状态和
  账户积分异常分别记录，避免重复生成。

## 测试

新增或扩展测试覆盖：

- 通用 Sora/Veo 别名在 `/v1/models` 和 `/v1/chat/completions` 的参数映射；
- Sora 默认竖屏、Pro 高尺寸、Veo 默认值和 1080p 时长约束；
- 旧后缀模型回归；
- 未支持媒体/安全字段和非有限数值的 400 响应；
- 任务状态写入失败、重启日志/临时文件收尾、关机等待和积分完成顺序；
- 下载路径越界拒绝；
- 完整 pytest，以及尽可能覆盖真实 app 路由到 manager/worker 的集成测试。

## 不在范围内

- 不修改 new-api 源码或其模型列表；
- 不新增图生视频、视频编辑、webhook、Veo 4K 或 Sora 16/20 秒；
- 不删除现有后缀模型 ID。
