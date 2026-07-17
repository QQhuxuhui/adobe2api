# 请求日志积分消耗展示 — 设计文档

日期:2026-07-16
状态:已确认(余额差值 + 自学习价格表)

## 背景与目标

管理后台请求日志页目前展示时间/状态/耗时/账号/模型/提示词/预览,没有单次请求的积分消耗。Adobe 生成接口不返回单次消耗,积分只能通过独立余额接口 `GET https://firefly.adobe.io/v1/credits/balance`(按账号)查询,现有封装为 `refresh_mgr.refresh_credits_for_token_id(token_id)`,会调用 `token_manager.set_credits` 更新账号缓存(`credits_total/used/available/updated_at`)。

目标:

1. 每条成功的生成请求在日志中展示消耗积分;实测值优先,测不准时用自学习价格表估算(带标记)。
2. 日志页缩小提示词与预览列的占用,为积分列腾出空间。
3. 副作用收益:每次成功生成后账号积分缓存自动保持新鲜。

## 总体数据流

```
生成请求 ──> _set_request_token_context()        # token 绑定点:in-flight 计数 +1
        ──> 请求结束(middleware finally)        # in-flight 计数 -1
              └─ 成功且有 token_id → 投递测量任务(log_id + 最终日志 payload + 归因快照)
                    └─ credits_tracker 后台单线程 worker:
                         1. refresh_credits_for_token_id(token_id) 取最新余额
                         2. delta = 新 used − 该账号上次快照 used
                         3. 归因干净 → credits=delta, source=measured,更新学习表
                            否则     → credits=学习表[key], source=estimated
                         4. log_store.upsert(log_id, payload + credits 字段) 回填日志
```

## 新模块 `core/credits_tracker.py`

单一职责:测量/估算每次成功请求的积分消耗并回填日志。

### 在途计数与归因判定

- 内存字典 `token_id -> in_flight_count`,`begin(token_id)` 在 `_set_request_token_context` 调用,`finish(token_id)` 在 middleware finally 调用。多次 attempt 换 token 时,以最终绑定的 token 为准(begin 前先 finish 旧值)。
- 内存字典 `token_id -> completions_since_snapshot`:自上次余额快照以来该账号完成的生成请求数。
- 测量时判定 **干净**:`completions_since_snapshot == 1`(只有本请求)且该账号当前 `in_flight_count == 0` 且上次快照的 `used` 已知(非 None)。
- 干净 → delta 可信;否则(并发、快照缺失、余额查询失败、delta ≤ 0)回退学习表。
- 每次成功查询余额后,`completions_since_snapshot` 归零、快照更新(快照即 token 记录里的 `credits_used`,由 `set_credits` 维护;tracker 在刷新前先读取旧值)。

### 后台 worker

- 单守护线程 + `queue.Queue`,middleware(async)只做非阻塞投递,阻塞的 `requests` 调用都在 worker 内执行。单线程天然保证同账号测量串行。
- 队列上限(如 200),满则丢弃并计 estimated 回填,避免上游故障时积压。
- 每个任务失败不抛出,仅日志 warning;回填失败静默。

### 学习表

- key 规则(从 model_id + 请求分辨率推导):
  - 图片:`{家族}:{分辨率档}`,如 `nano-banana-pro:2K`、`gpt-image:4K`。家族取目录项的 `upstream_model_version` 区分 nano-banana-2/3,gpt-image 统一;比例不参与 key(同分辨率档同价)。
  - Gemini 原生入口模型(`gemini-3-pro-image` 等):家族取模型 id 主干,分辨率档取解析后的输出档。
  - 视频:`{家族}:{时长}s[:{分辨率}]`,家族取 VIDEO_MODEL_CATALOG 的 `engine`(缺省时按 id 前缀归类为 sora2/sora2-pro),如 `sora2:12s`、`sora2-pro:8s`、`veo31-standard:8s:1080p`、`kling-o3:15s:1080p`、`kling3:10s:720p`。
  - 推导失败 → key = 完整 model_id 兜底。
- value:最近一次干净实测的积分数(直接覆盖,不做平均)。
- 持久化:`data/credit_costs_learned.json`,每次更新即写盘(量小);启动时加载,文件损坏则从空表开始。
- 估算时 key 未命中 → credits 置 None(前端显示 `-`),不猜。

### 分辨率捕获

动态分辨率模型(基础模型/别名/Gemini 原生)的输出档由请求参数决定,日志记录里没有。生成路由在解析出 `output_resolution`(1K/2K/4K)或视频规格后写入 `request.state.log_output_resolution`,随测量任务传给 tracker,仅用于 key 推导,不新增展示列。带后缀的目录模型直接查目录,无需该字段。

## 日志记录字段

`RequestLogRecord` 新增:

- `credits_used: Optional[float]` — 消耗积分,None 表示未知(旧日志/失败/无法估算)。
- `credits_source: Optional[str]` — `"measured"` 或 `"estimated"`,与 credits_used 同生同灭。

回填方式:middleware 把最终写入的完整 payload 交给测量任务,worker 补上两个字段后 `log_store.upsert(log_id, payload)`(JSONL 追加,读取时同 id 取最新,现有机制)。多 attempt 请求回填到最终成功的 attempt 记录 id。失败请求(状态≥400 或任务失败)不投递测量,不计消耗。

## 管理后台 UI(static/admin.js / admin.html / admin.css)

- 日志表新增窄列"积分",位于"模型"列后:
  - measured → 数字(如 `12`);
  - estimated → `~数字`,title 提示"估算值(按历史实测)";
  - 无值 → `-`。
- 提示词列(`.log-prompt-cell`)设 `max-width` + 单行省略号(悬停 tooltip 已有);预览列压缩到按钮宽度。
- `/api/v1/logs` 返回原始 dict,新字段自动透出,无后端接口改动。

## 错误处理

- 余额接口 401/403(CreditsAuthError):沿用现有语义(refresh_mgr 已处理账号失效),本次记 estimated。
- 余额接口其他失败/超时:记 estimated;学习表不更新。
- worker 任何异常不影响请求主流程(测量完全异步、旁路)。

## 测试计划

Python(pytest,新增 `tests/test_credits_tracker.py`):

- 干净测量:单请求完成 → delta 写入、source=measured、学习表更新并落盘。
- 并发回退:同 token 两个请求交叠 → 两条都 estimated,学习表不被污染。
- 快照缺失/余额查询失败/delta≤0 → estimated;key 未命中 → credits=None。
- key 推导:目录后缀模型、动态模型+log_output_resolution、视频各家族、未知模型兜底。
- 学习表持久化:写盘/加载/损坏文件容错。

前端(node --test,扩展现有结构断言):

- 日志表头含"积分"列、colspan 更新。
- 积分单元格渲染三态(数字 / `~数字` / `-`)。

## 明确不做

- 不做积分统计汇总卡(后续需要再加)。
- 不做手工价格表编辑界面(学习表自动维护;确有需要可直接改 JSON 文件)。
- 不追溯旧日志。
