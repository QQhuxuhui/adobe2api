# Leonardo 集成使用指南

## 概述

Leonardo AI 已完整集成到 adobe2api，通过 `/v1/images/generations` 提供 OpenAI 兼容的图像生成 API。

## 快速开始

### 1. 配置代理（必需）

Leonardo API 需要通过代理访问（绕过 Cloudflare）：

```bash
export HTTP_PROXY=http://127.0.0.1:10809
export HTTPS_PROXY=http://127.0.0.1:10809
python3 app.py
```

### 2. 获取 Leonardo Bearer Token

**手动方式**（推荐，稳定可靠）：

1. 浏览器访问 https://app.leonardo.ai（确保代理开启）
2. 打开 DevTools → Network 标签
3. 刷新页面，找任意 GraphQL 请求
4. 复制 `Authorization: Bearer eyJ...` 的完整值

**添加到配置**：

```bash
python3 scripts/leonardo_token_manager.py --add "eyJraWQi..."
# 或更新现有 token
python3 scripts/leonardo_token_manager.py --update <token_id> "eyJraWQi..."
```

### 3. 调用 API

```bash
curl -X POST http://127.0.0.1:6001/v1/images/generations \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer projectx_webapp" \
  -d '{
    "model": "leonardo-nano-banana-2",
    "prompt": "a majestic dragon flying over mountains",
    "aspect_ratio": "16:9",
    "n": 1,
    "response_format": "url"
  }'
```

**响应示例**：

```json
{
  "created": 1785556940,
  "model": "leonardo-nano-banana-2",
  "data": [
    {
      "url": "http://127.0.0.1:6001/generated/1f18d5db-5954-6690-a576-834a42d2fe3c-0.png",
      "revised_prompt": "a majestic dragon flying over mountains"
    }
  ],
  "usage": {
    "total_tokens": 1582
  }
}
```

## 支持的参数

| 参数 | 类型 | 说明 | 默认值 |
|------|------|------|--------|
| `model` | string | 模型名称（`leonardo-nano-banana-2`） | 必需 |
| `prompt` | string | 图像描述 | 必需 |
| `aspect_ratio` | string | 宽高比（`1:1`, `16:9`, `9:16` 等） | `1:1` |
| `n` | integer | 生成图片数量（1-8） | 1 |
| `response_format` | string | 返回格式（`url` 或 `b64_json`） | `url` |

## Token 管理

### 查看当前 tokens

```bash
python3 scripts/leonardo_token_manager.py --list
```

### 更新 Bearer（每小时需刷新）

```bash
# JWT 有效期约 1 小时，过期后需重新获取
python3 scripts/leonardo_token_manager.py --update <token_id> "新Bearer"
```

### 重置 token 状态

```bash
# 如果 token 被标记为 invalid 但实际有效
python3 scripts/leonardo_token_manager.py --reset <token_id>
```

## 技术细节

### 完整请求链路

```
POST /v1/images/generations (model=leonardo-nano-banana-2)
  ↓
token_selector: get_available(token_type="leonardo")
  ↓
leonardo_generation.generate_images
  ↓
LeonardoClient.create_generation (GraphQL Generate)
  ↓
轮询 GetAIGenerationFeedStatuses 直到 COMPLETE
  ↓
CDN 图片下载 → 本地持久化
  ↓
返回 OpenAI 风格响应
```

### 异常处理

- **提交前失败**（认证/网络）：可自动换号重试
- **已提交/单发失败**：抛 `LeonardoGenerationError`，返回 500，**不可重试**（防重复扣费）
- **轮询超时**：默认 300 秒，返回 timeout 错误

### Credits 查询

服务启动时自动查询 Leonardo credits 余额：

```python
# refresh_mgr 自动识别 leonardo type
{
  "subscriptionTokens": 7660,
  "gptTokens": 100000  # apiCredit
}
```

## 已知限制

1. **JWT 有效期约 1 小时**：需定期手动刷新 Bearer（自动化被 Cloudflare Turnstile 挡住）
2. **需要代理**：直连会被 Cloudflare 检测
3. **账号池未持久化**：服务启动时从 `config/tokens.json` 加载

## 自动化尝试（备用方案）

项目包含以下自动获取 Bearer 的脚本（当前因 Cloudflare 检测未成功，保留供未来使用）：

- `scripts/leonardo_bootstrap_spike.py`（Playwright + stealth）
- `scripts/leonardo_bootstrap_nodriver.py`（nodriver）
- `scripts/leonardo_bootstrap_uc.py`（undetected-chromedriver）

如果未来 Cloudflare 策略放松或工具改进，可直接使用：

```bash
python3 scripts/leonardo_bootstrap_uc.py \
  --account tests/canva_member_emails_xxx.txt \
  --proxy http://127.0.0.1:10809 \
  --out leonardo_session.json
```

## 测试验证

```bash
# 运行 Leonardo 相关测试
python3 -m pytest tests/test_leonardo_*.py -v

# 完整测试套件（包含 Leonardo）
python3 -m pytest  # 702 passed
```

## 故障排查

### 503 Service Unavailable

**原因**：token pool 无可用 Leonardo token

**解决**：
1. 检查 token 状态：`python3 scripts/leonardo_token_manager.py --list`
2. 如果 status=invalid，重置：`--reset <token_id>`
3. 如果 JWT 过期，更新：`--update <token_id> "新Bearer"`

### Credits refresh failed

**原因**：Bearer 过期或代理未配置

**解决**：
1. 确保环境变量 `HTTP_PROXY` 和 `HTTPS_PROXY` 已设置
2. 更新 Bearer：`python3 scripts/leonardo_token_manager.py --update ...`
3. 重启服务

### 图片生成超时

**原因**：轮询超时（默认 300 秒）

**解决**：
- Leonardo 生成较慢，正常情况 15-30 秒
- 如果持续超时，检查代理连接和 Leonardo 服务状态

## 相关文件

- `core/leonardo_client.py` - Leonardo GraphQL 客户端
- `core/leonardo_generation.py` - 图像生成逻辑
- `core/leonardo_route.py` - 路由分叉与异常映射
- `scripts/leonardo_token_manager.py` - Token 管理工具
- `config/tokens.json` - Token 持久化配置
