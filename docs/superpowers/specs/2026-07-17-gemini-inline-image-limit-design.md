# Gemini 原生入口 inline 图片限额调整设计

日期：2026-07-17

## 背景

Gemini 原生兼容入口当前同时设置了三层限制：

- HTTP 请求体最大 64 MiB；
- 单张 inline 图片解码后最大 20 MiB；
- 单请求图片解码后合计最大 40 MiB。

这会让一张 20 至 40 MiB 的图片在尚未触及请求体和图片总量上限时，被单图限制提前
拒绝并返回 `Inline image exceeds 20 MiB`。

Google Gemini 官方文档中的 20 MB 是包含文本、系统指令和 inline 数据在内的整个
请求上限，不是单图上限；更大的官方请求应使用 Files API。本项目的 Gemini 兼容入口
会把 inline 图片上传到 Adobe，并未实现 Gemini `fileData`，因此不能直接采用 Files API
的 2 GB 单文件上限。

官方来源：

- [Gemini API 图片理解](https://ai.google.dev/gemini-api/docs/image-understanding)
- [Gemini Files API](https://ai.google.dev/gemini-api/docs/files)

## 目标

- 不再因独立的 20 MiB 单图限制拒绝图片；
- 单张图片可使用完整的 40 MiB 图片总预算；
- 不扩大现有 64 MiB 请求体和 40 MiB 解码图片总量的资源边界；
- 在解码前拒绝明显超出剩余预算的 Base64，避免无意义的大块内存分配；
- 统一限额常量、错误文案、README 和测试。

## 非目标

- 不取消请求体或图片总量限制；
- 不实现 Gemini Files API 或 `fileData`；
- 不自动压缩、缩放或转码输入图片；
- 不修改 OpenAI 兼容入口、视频入口和实体图片入口的 10 MiB 限制；
- 不承诺 Adobe 上游接受任意尺寸的图片。超过本服务校验后，上游仍可按自身规则拒绝。

## 方案

### 限额模型

保留：

```text
GEMINI_NATIVE_MAX_BODY_BYTES = 64 MiB
GEMINI_MAX_TOTAL_IMAGE_BYTES = 40 MiB
GEMINI_MAX_IMAGES = 6
```

删除独立的 `GEMINI_MAX_IMAGE_BYTES = 20 MiB` 语义。每张图片可用大小由解析到该图片时
剩余的总预算决定：

```text
remaining = 40 MiB - 已接受图片的解码字节数
```

因此第一张图片最大可为 40 MiB；多图请求按出现顺序共享 40 MiB，仍只处理前 6 张。

### 解码流程

`decode_inline_image` 接收本次可用的 `max_bytes`，处理顺序如下：

1. 校验 MIME 类型仍为 JPEG、PNG 或 WebP；
2. 根据 `max_bytes` 计算 Base64 最大编码字符数
   `4 * ceil(max_bytes / 3)`；编码文本超界时，在解码前拒绝；
3. 使用 `base64.b64decode(..., validate=True)` 严格解码；
4. 解码结果超过 `max_bytes` 时拒绝；
5. 返回图片字节和归一化后的 MIME 类型。

请求解析器在处理每张图片前计算剩余预算并传入解码函数。这样既取消了 20 MiB 单图
门槛，又不会先解码一张超过 40 MiB 或超过剩余总预算的图片。

### 错误语义

所有图片预算超限统一返回 Google 风格的 400 `INVALID_ARGUMENT`：

```text
Inline images exceed 40 MiB total
```

这同时修复当前常量为 40 MiB、错误文案仍写 30 MiB 的不一致。非法 MIME 和非法
Base64 的错误保持不变。错误和请求日志不得包含 Base64 原文。

### 内存边界

64 MiB 请求体能容纳最多约 53.34 MiB 的 Base64 图片文本，对应 40 MiB 解码字节，
并留出约 10 MiB 给 JSON 和文本字段。解析期间仍可能同时存在原始请求、Base64 字符串、
解码字节和上传缓冲，因此必须保留请求体和解码总量两层限制，不能采用无限制方案。

## 文档更新

README 中 Gemini 原生入口的限制说明更新为：

- 请求体最大 64 MiB；
- 最多使用前 6 张 inline 图片；
- 不设独立单图限额；
- 图片解码后合计最大 40 MiB，故单张图片实际最大也是 40 MiB。

原 Gemini 原生入口设计文档中 20 MiB 单图限制的描述同步修正，避免后续维护时恢复旧
行为。

## 测试

解析与路由测试覆盖：

- 单图解码后大于 20 MiB、但不超过 40 MiB 时被接受；
- 单图恰好 40 MiB 时被接受；
- 单图超过 40 MiB 时在 Base64 解码前被拒绝；
- 多图累计恰好 40 MiB 时被接受；
- 多图累计超过 40 MiB 时，超预算图片在解码前或解码后被拒绝；
- 超限错误统一显示 40 MiB，不再出现 20 MiB 或 30 MiB；
- 非法 Base64、非白名单 MIME、最多 6 张图片和 64 MiB 请求体限制保持回归通过；
- OpenAI、视频和实体图片路径的既有限制不受影响。

测试使用桩或小体积替代数据模拟边界，避免在单元测试中实际分配多份 40 MiB 缓冲。

## 验收标准

- 原先触发 `Inline image exceeds 20 MiB` 的 20 至 40 MiB 单图不再被本地单图校验拒绝；
- 任一请求的前 6 张图片解码总量不能超过 40 MiB；
- 请求体不能超过 64 MiB；
- 超限在调用 Adobe 上传接口前返回 400；
- 完整测试套件通过，README 和设计文档与实现一致。
