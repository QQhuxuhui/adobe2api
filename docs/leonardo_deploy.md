# Leonardo refresher — 搬瓦工容器部署

两容器：`adobe2api`（出图 API）+ `leonardo-refresher`（常驻 Chrome + noVNC，登录后自动刷新 Leonardo token 推给 adobe2api）。镜像本地已验证可构建（adobe2api 180MB / refresher 2.7GB）。

部署模型：**构建机推镜像到阿里云 → 搬瓦工 pull 部署**（与 adobe2api 现有流程一致）。

## A. 构建机（有 docker + 已 `docker login` 阿里云）

```bash
./build-and-push.sh            # 推 adobe2api:vN + latest
./build-and-push-leonardo.sh   # 推 leonardo-refresher:vN + latest（2.7GB，首次较慢）
```

## B. 搬瓦工（pull 部署）

```bash
# 1. 取仓库（拿 compose / seccomp / config 模板；镜像走 pull 不 build）
git clone <repo> adobe2api && cd adobe2api        # 或 git pull

# 2. 确认出口地区（决定要不要代理）
curl -s https://ipinfo.io/country                  # US/JP/NL 等非受限 → LEONARDO_PROXY 留空直连

# 3. 配置 adobe2api（首次）
cp config/config.example.json config/config.json
#   编辑：api_key、admin_username/password、admin_session_secret；出图代理 use_proxy/proxy 按需

# 4. 配置 sidecar 密钥
cp .env.example .env
#   LEONARDO_REFRESH_KEY = openssl rand -hex 32
#   NOVNC_PASSWORD       = openssl rand -base64 18（≥8）
#   LEONARDO_PROXY       = 非受限地区留空
#   （可选）ADOBE2API_IMAGE / LEONARDO_REFRESHER_IMAGE 固定到某 vN，默认 latest

# 5. pull + 起服务（--profile leonardo 才拉起 refresher）
docker compose -f docker-compose.deploy.yml --profile leonardo pull
docker compose -f docker-compose.deploy.yml --profile leonardo up -d

# 6. 健康检查
docker compose -f docker-compose.deploy.yml --profile leonardo ps
curl -s http://127.0.0.1:8080/healthz            # refresher: {"state":"login_required",...} 属正常（未登录）
curl -s http://127.0.0.1:6001/login -o /dev/null -w '%{http_code}\n'   # adobe2api 200
```

> 本地开发/自建构建仍可用 `docker-compose.yml`（`build:` 版）：`docker compose --profile leonardo up -d --build`。

## 首次上传 cookie（headless，无 noVNC）

登录**在本地**做（住宅 IP + 真实浏览器，Turnstile 秒过），再把 cookie 上传：

1. 本地浏览器登录 `app.leonardo.ai`。
2. DevTools → Network → 任意 `app.leonardo.ai` 请求 → 复制整条 `cookie:` 请求头
   （至少含 `__Secure-better-auth.session_token` + `session_data.0/.1`）。
3. 一条 curl 上传（`<KEY>` = `.env` 的 `LEONARDO_REFRESH_KEY`）：

```bash
curl -X POST http://<搬瓦工IP>:6001/api/v1/tokens/leonardo/cookie \
  -H "X-Leonardo-Refresh-Key: <KEY>" -H "Content-Type: application/json" \
  -d "{\"cookie\":\"<粘贴整条 cookie>\"}"
```

上传后 ~60 秒内 refresher 自动：拉 cookie → headless 加载 → get-session 取新鲜
id_token → 推 adobe2api 池。`/healthz` 变 `state=healthy`。会话约 6 周，到期本地重导一次即可。

## 验证出图

```bash
KEY=<config.json 的 api_key>
curl -s http://127.0.0.1:6001/v1/images/generations \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"model":"leonardo-nano-banana-2","prompt":"a red panda","aspect_ratio":"1:1","n":1,"response_format":"url"}'
```

## 运维
- 刷新器状态：`GET :8080/healthz`（headless 已发布到 `127.0.0.1:8080`）：`state`/`session_state`/`last_success_at`/`current_token_exp`/`consecutive_failures`/`last_error_kind`。
- `state=login_required` + `last_error_kind=cookie_required`（从未上传）或 `login_required`（cookie 过期）→ 本地重新登录导出 cookie，重跑上传 curl。
- `browser_unavailable` 是唯一 503 态（浏览器控制失联，连续 3 次进程退出→容器重启）。
- profile / 已上传 cookie 分别存 named volume `leo-profile` / `config/leonardo_cookie.json`，容器重启免重传。
- 镜像推私有 registry：adobe2api 用 `build-and-push.sh`，refresher 用 `build-and-push-leonardo.sh`；搬瓦工统一 `docker-compose.deploy.yml` pull 部署。
