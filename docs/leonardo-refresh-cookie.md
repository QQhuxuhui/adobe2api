# Leonardo cookie 刷新指南

Leonardo 出图 Bearer 是 AWS Cognito id_token（~1 小时过期），由 `leonardo-refresher`
sidecar 用会话 cookie 自动续期。会话 cookie 存活约 **6 周**，到期需**本地重新登录导出 cookie 上传**一次。

登录必须在**本地浏览器**做（住宅 IP + 真实浏览器，Turnstile 秒过）；服务器机房 IP 过不了人机校验。

## 步骤

1. 本地浏览器登录 **`app.leonardo.ai`**，保持登录态。
2. **F12 → Network（网络）** 标签。
3. 刷新页面，过滤框输入 **`get-session`**（或任选一条发往 `app.leonardo.ai` 的请求）。
4. 点中该请求 → **Headers（标头）→ Request Headers（请求标头）** → 复制整条 **`cookie:`** 的值。
   - 或：右键该请求 → **Copy → Copy as cURL**，其中 `-b '...'` 段即 cookie。
5. 上传（`<HOST>`=服务器 IP，`<KEY>`=`/opt/adobe2api/.env` 的 `LEONARDO_REFRESH_KEY`）：

```bash
curl -X POST http://<HOST>:6001/api/v1/tokens/leonardo/cookie \
  -H "X-Leonardo-Refresh-Key: <KEY>" \
  -H "Content-Type: application/json" \
  -d '{"cookie":"<粘贴整条 cookie>"}'
```

上传后约 60 秒内，refresher 自动拉取 → headless 加载 → get-session 取新鲜 id_token → 推入
token 池。`GET :8080/healthz` 变 `state=healthy` 即成功。

## 关键点

- 复制的 cookie 串里必须含以下三条（服务端只抽取它们，其余自动丢弃）：
  - `__Secure-better-auth.session_token`
  - `__Secure-better-auth.session_data.0`
  - `__Secure-better-auth.session_data.1`
- 这三条是 **HttpOnly**，只能从 **Network 的请求头**复制；Console 里 `document.cookie` 读不到。
- 已上传的 cookie 存 `config/leonardo_cookie.json`（sha256 指纹去重）；容器重启免重传。

## 何时需要重传

`GET :8080/healthz`：
- `state=login_required` + `last_error_kind=cookie_required`：从未上传 → 首次上传。
- `state=login_required` + `last_error_kind=login_required`：cookie 过期（约 6 周）→ 本地重新登录、重导、重传。

部署与架构见 [leonardo_deploy.md](leonardo_deploy.md)。
