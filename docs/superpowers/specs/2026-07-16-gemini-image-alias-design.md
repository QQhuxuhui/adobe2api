# Gemini 绘图模型别名 + Gemini 口径 usage 上报

日期：2026-07-16
状态：已确认（用户拍板：不加 gemini-2.5-flash-image）

## 背景

下游 sub2api 已通过 `gpt-image-1/2` 别名 + OpenAI gpt-image-1 token 表跑通了 GPT 生图的
转发与按 token 计费（commit 0e1f98e / 2db67b5 / f945ef2）。Gemini 绘图（上游
nano-banana 系列）尚无对应适配：没有 sub2api/LiteLLM 认识的 `gemini-*` 模型名，
usage 也套的是 gpt-image-1 的 token 表，按 Gemini 单价计费会算错钱。

已验证的事实（sub2api 源码 + Adobe/Google 公开资料）：

- sub2api `/v1/images/generations` 只放行 `gpt-image-*`（`openai_images.go:464`），
  Gemini 绘图必须走 `/v1/chat/completions`；chat 网关从
  `usage.output_tokens_details.image_tokens` 取图像 token，按模型名查 LiteLLM
  `output_cost_per_image_token` 计费——本项目现有 usage 结构已兼容。
- Firefly 内部 `modelVersion` 与 Google 模型对应关系：
  - `nano-banana-2` = Firefly「Nano Banana Pro」= **gemini-3-pro-image(-preview)**
  - `nano-banana-3` = Firefly「Nano Banana 2」= **gemini-3.1-flash-image(-preview)**
- 原版 Nano Banana（gemini-2.5-flash-image）本项目上游没接，**不加别名**（决策：
  上游实际是 3.1 模型，挂 2.5 的名字只会少收钱）。

## 方案

完全复刻 gpt-image 别名的打法，三处改动：

### 1. catalog.py：注册 4 个动态别名

`gemini-3-pro-image`、`gemini-3-pro-image-preview` → upstream
`google:firefly:colligo:nano-banana-pro` / `gemini-flash` / `nano-banana-2`；
`gemini-3.1-flash-image`、`gemini-3.1-flash-image-preview` → 同 upstream_model，
版本 `nano-banana-3`。

均为 dynamic 基础模型（quality/size/aspect_ratio 请求参数自适应，行为同
`gpt-image-1`）。每个条目加 `usage_family` 字段（`"gemini-3-pro"` /
`"gemini-3.1-flash"`），供 usage 计算选表；不加该字段的模型走现有 gpt-image 表。

### 2. resolver.py：build_image_usage 按模型族选 token 表

新增 Gemini 输出图像 token 表（按分辨率档，与朝向无关；数值 × sub2api 内置
LiteLLM 单价 = Google 官方每图价）：

| usage_family | 1K | 2K | 4K | 输入图/张 | 单价 |
|---|---|---|---|---|---|
| gemini-3-pro | 1120 | 1120 | 2000 | 560 | $120/M（$0.134 / $0.134 / $0.24） |
| gemini-3.1-flash | 1120 | 1680 | 2520 | 1120 | $60/M（$0.067 / $0.101 / $0.151） |

`build_image_usage()` 增加可选参数 `usage_family: Optional[str] = None`：
- `None`（默认）→ 现有 gpt-image-1 表，**现有模型行为完全不变**；
- gemini 族 → 上表选输出 token（按 output_resolution），输入图 token 按族取
  560/1120，提示词 token 沿用现有 CJK 感知估算。

usage 返回结构不变（prompt/completion + input/output 两套命名 +
output_tokens_details.image_tokens）。

### 3. generation.py：调用点透传 usage_family

两处 `build_image_usage(...)` 调用（`generation.py:320` images/generations、
`generation.py:821` chat/completions）从 `model_conf.get("usage_family")`
取值传入。

### 4. README：支持模型族清单补 `gemini-3-pro-image*` / `gemini-3.1-flash-image*`
说明走 chat/completions、计费口径与 sub2api 配置方式（模型映射恒等即可）。

## 错误处理

- 未知 usage_family 值：回退 gpt-image 表（防御性，不抛错）。
- 未知分辨率档：沿用现有逻辑（默认 2K 档语义）。

## 测试

现有 tests/（test_generate.py / test_service.py）是需要真实服务与 token 的手工
集成脚本，不动。新增 `tests/test_models_unit.py`（纯离线，直接 `python -m pytest`
或 `python tests/test_models_unit.py` 可跑），断言：
- 4 个别名可被 resolve_model 解析、dynamic 生效（quality=high → 4K 等）；
- build_image_usage 各族 × 各分辨率的输出 token 断言（1120/1680/2520/2000 等）；
- 无 usage_family 时输出与现状一致（回归保护）；
- 输入图 token：3-pro 2 张 → 1120，3.1-flash 1 张 → 1120。

## 不做的事

- 不加 gemini-2.5-flash-image 别名（用户已拍板）。
- 不加 Gemini 原生 `/v1beta/...:generateContent` 端点（sub2api 走 OpenAI chat
  格式即可，链路已验证）。
- 不改 firefly-* 全名模型与 gpt-image-* 别名的 usage 口径。
