# Leonardo 集成使用指南

> **安全提示**：refresher 的 `leo-profile` volume 含 Leonardo 登录会话，等同账号访问权。noVNC 仅绑定 `127.0.0.1`，远程访问必须通过 SSH 隧道，不要将 6080 端口直接暴露到公网。

## 概述

Leonardo AI 已完整集成到 adobe2api，通过 `/v1/images/generations` 提供 OpenAI 兼容的图像生成 API。

## 快速开始

### 1. 配置自动刷新 sidecar（推荐）

先生成机器间共享密钥：

```bash
openssl rand -hex 32
```

将输出和 noVNC 密码写入项目根目录 `.env`。代理运行在 Docker 宿主机的 10809 端口时，使用 Compose 已配置的 `host.docker.internal`：

```dotenv
LEONARDO_REFRESH_KEY=replace-with-openssl-output
LEONARDO_PROXY=http://host.docker.internal:10809
NOVNC_PASSWORD=replace-with-a-strong-password
LEONARDO_ACCOUNT_LABEL=Primary
LEONARDO_TOKEN_MIN_TTL_SECONDS=600
```

Compose 使用 `LEONARDO_TOKEN_MIN_TTL_SECONDS` 同时配置主服务的最低 TTL 和 sidecar 的安全余量，避免两侧漂移。宿主机代理还必须监听 Docker 网桥可达的地址，不能只监听宿主机 `127.0.0.1`。

启动主服务和可选的 Leonardo profile：

```bash
docker compose --profile leonardo up -d --build
```

首次登录在部署主机本地打开 `http://127.0.0.1:6080/vnc.html`。远程部署时先建立隧道：

```bash
ssh -L 6080:127.0.0.1:6080 user@server
```

在 noVNC 中使用 `NOVNC_PASSWORD` 连接，然后在容器内 Chromium 完成 Leonardo → Canva → OTP/Turnstile 登录。登录成功后，sidecar 会立即获取 Cognito ID token，此后根据 token `exp` 自动刷新并推送到 adobe2api。

### 2. 手动获取 Bearer（备用）

不启用 sidecar 时仍可手动维护 token：

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

### 自动刷新状态

从容器内部读取 sidecar 状态：

```bash
docker compose --profile leonardo exec leonardo-refresher \
  python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/healthz').read().decode())"
```

| `state` | 含义 | 操作 |
| --- | --- | --- |
| `healthy` | 最近一次刷新和推送成功 | 无需操作 |
| `refresh_retrying` | token 太旧、代理或上游暂时失败 | 检查 `last_error_kind` 和代理，sidecar 会每 60 秒重试 |
| `login_required` | better-auth 会话明确失效 | 通过 noVNC 重新登录 |
| `push_failed` | 浏览器已取得 token，但 adobe2api 拒绝或不可达 | 检查共享密钥、主服务和容器网络 |
| `browser_unavailable` | Chromium 控制通道持续不可用 | sidecar 会先尝试重建浏览器；连续 3 轮失败后进程退出，由 Compose restart policy 重启容器 |

`/healthz` 在正常运行和登录过期时返回 200，只有浏览器控制通道不可恢复时返回 503。健康状态本身不负责重启；sidecar 会在连续 3 轮控制失败后退出，让 `restart: unless-stopped` 生效。Docker 不会因为登录过期反复重启容器；业务告警应检查 `state`、`last_success_at` 和 `current_token_exp`。

### 查看当前 tokens

```bash
python3 scripts/leonardo_token_manager.py --list
```

### 手动更新 Bearer

```bash
# 仅在未启用 sidecar 或临时排障时使用
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

### 自动刷新链路

```text
持久化 Chromium profile（人工登录一次）
  ↓ 浏览器上下文 fetch('/api/auth/get-session')
校验 id_token 与剩余 TTL
  ↓ X-Leonardo-Refresh-Key
POST /api/v1/tokens/leonardo
  ↓ type=leonardo + Cognito sub
按账号原子 upsert、清理历史重复 token、拒绝 exp 倒退
```

Leonardo 浏览器和 GraphQL 请求只使用 `LEONARDO_PROXY`。程序不会读取全局 `HTTP_PROXY/HTTPS_PROXY`，避免 Adobe 或 CDN 请求被意外代理。

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

1. **首次登录仍需人工操作**：Turnstile 和 Canva OTP 不自动绕过。
2. **better-auth 会话约 6 周**：会话失效后需通过 noVNC 再登录一次。
3. **单 sidecar 单账号**：当前不提供多账号浏览器编排。
4. **需要稳定代理**：浏览器登录和 Leonardo GraphQL 应使用同一个可达代理。
5. **profile 是敏感资产**：删除 `leo-profile` volume 会丢失登录会话；复制该 volume 等同复制账号访问权。
6. **浏览器最小权限运行**：入口进程只在启动阶段迁移 `/profile` 所有权，随后通过基础镜像已有的 `pwuser` 运行 Xvfb、noVNC 和 Chromium，并使用 Playwright 官方 seccomp profile 启用 Chromium sandbox。已有的 root-owned `leo-profile` 会原地迁移，不需要删除登录会话。

## 历史 bootstrap 脚本

项目仍保留早期一次性 bootstrap 脚本用于调研和排障，它们不替代持久化 sidecar：

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
# 运行 Leonardo 与 refresher 相关测试
python3 -m pytest tests/test_leonardo_*.py -q

# 校验 Compose 和 shell
LEONARDO_REFRESH_KEY=test-key \
LEONARDO_PROXY=http://proxy:10809 \
NOVNC_PASSWORD=test-pass \
docker compose --profile leonardo config --quiet
bash -n leonardo_refresher/entrypoint.sh

# 完整测试套件
python3 -m pytest -q
```

## 故障排查

### 503 Service Unavailable

**原因**：token pool 无可用 Leonardo token

**解决**：
1. 检查 token 状态：`python3 scripts/leonardo_token_manager.py --list`
2. 检查 sidecar `/healthz` 和 `docker compose logs leonardo-refresher`
3. `state=login_required` 时通过 noVNC 重新登录
4. 未启用 sidecar时，手动执行 `--update <token_id> "新Bearer"`

### Credits refresh failed

**原因**：Bearer 过期或代理未配置

**解决**：
1. 确保两个容器的 `LEONARDO_PROXY` 相同且代理可达
2. 更新 Bearer：`python3 scripts/leonardo_token_manager.py --update ...`
3. 重启服务

### sidecar 显示 push_failed

**原因**：共享密钥不一致、adobe2api 未启动，或容器内地址不可达。

**解决**：
1. 确认 `.env` 中只有一个 `LEONARDO_REFRESH_KEY`
2. 运行 `docker compose --profile leonardo config` 检查两个 service 的渲染值
3. 重启两个服务：`docker compose --profile leonardo up -d`

### noVNC 可以打开但浏览器无法访问 Leonardo

**原因**：`LEONARDO_PROXY` 指向了容器自身的 `127.0.0.1`，或宿主代理没有监听 Docker 可达地址。

**解决**：代理在宿主机时使用 `http://host.docker.internal:<port>`；不要在容器配置中使用 `http://127.0.0.1:<port>`。

### 图片生成超时

**原因**：轮询超时（默认 300 秒）

**解决**：
- Leonardo 生成较慢，正常情况 15-30 秒
- 如果持续超时，检查代理连接和 Leonardo 服务状态

## 相关文件

- `core/leonardo_client.py` - Leonardo GraphQL 客户端
- `core/leonardo_generation.py` - 图像生成逻辑
- `api/routes/leonardo_tokens.py` - sidecar token 推送端点
- `leonardo_refresher/` - 持久化浏览器、刷新状态机和 noVNC 镜像
- `scripts/leonardo_token_manager.py` - Token 管理工具
- `config/tokens.json` - Token 持久化配置
