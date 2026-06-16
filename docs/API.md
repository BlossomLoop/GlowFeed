# 拾光 GlowFeed · 接口文档

日期：2026-06-16
状态：现行

供前端对接。所有接口前缀 `/api`，请求体与响应体均为 JSON（UTF-8）。带请求体时设 `Content-Type: application/json`。

## 通用约定

- **鉴权**：管理操作采用 Bearer Token，请求头携带 `Authorization: Bearer <token>`，校验失败统一返回 `401 {"error": "未授权"}`。token 由站主经 `http://host/?token=xxx` 进入获得，前端存 `sessionStorage` 并在统一的 `api()` 封装里注入请求头。后端是唯一真实边界，前端按 `GET /api/auth/verify` 结果给 `<body>` 打 `authed`/`guest` class，仅控制管理菜单显隐。
- **时间**：所有时间字段为 ISO 8601 UTC（如 `2026-06-16T08:30:00+00:00`），无值为 `null`。
- **错误**：统一结构 `{"error": "描述"}`。常见状态码：`200` 成功 / `201` 创建成功 / `400` 参数错误 / `401` 未授权 / `404` 资源不存在。
- **分页/排序**：列表类接口由各自 query 参数控制，详见对应接口。

## 端点总览

**公开（无需 token）：**

| 端点 | 说明 |
|---|---|
| `GET /api/sources` | 信息源元数据列表 |
| `GET /api/tasks/public` | 公众简报页任务切换，仅 `[{id, name}]` |
| `GET /api/articles` | 资讯（情报流）列表 |
| `GET /api/digest` | 每日简报快照 |
| `GET /api/trending` | GitHub 趋势榜（读缓存快照） |
| `GET /api/auth/verify` | 鉴权探测，恒 `200 {authed}` |

**受保护（需 token，否则 401）：**

| 端点 | 说明 |
|---|---|
| `GET /api/tasks` | 完整任务列表 |
| `POST /api/tasks` | 创建任务 |
| `PUT /api/tasks/{id}` | 更新任务 |
| `DELETE /api/tasks/{id}` | 删除任务 |
| `POST /api/tasks/{id}/run` | 立即执行一次侦听 |
| `GET /api/feedback` | 某任务的赞踩信号 |
| `POST /api/feedback` | 提交赞踩 |
| `GET /api/runs` | 执行日志 |
| `GET /api/settings` | 模型配置（脱敏回显） |
| `PUT /api/settings` | 保存模型配置 |
| `POST /api/settings/test` | 测试模型连接 |
| `POST /api/digest/{id}/refresh` | 手动重算简报，耗 LLM |
| `POST /api/trending/refresh` | 强制刷新趋势榜，回源 OSSInsight、耗配额 |

---

## 一、鉴权探测

### GET /api/auth/verify

公开特例，恒 `200`，不返回 401。前端据此判断当前 token 是否有效、是否进入管理态。

**返回** `200`

```json
{ "authed": true }
```

---

## 二、信息源

### GET /api/sources

公开。返回全部信息源元数据，供前端任务编辑器勾选来源。

**返回** `200`：数组，单条字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string | 来源标识，如 `hackernews` / `bilibili` |
| `name` | string | 显示名 |
| `region` | string | 地区分类，如 `国内` / `国外` / `国内外` |
| `kind` | string | `search`（关键词检索）/ `feed`（热榜或 RSS，本地过滤） |

```json
[ { "id": "hackernews", "name": "Hacker News", "region": "国外", "kind": "search" } ]
```

---

## 三、任务

任务对象（`Task`）字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | int | 任务 ID |
| `name` | string | 任务名 |
| `keywords` | string[] | 用户设定关键词（命中任一即收录），永不被偏好学习改动 |
| `sources` | string[] | 启用的来源 id 列表 |
| `schedule_type` | string | `interval`（固定间隔）/ `daily`（每日定点） |
| `schedule_value` | string/int | `interval` 为分钟数（下限 5）；`daily` 为时刻列表，如 `"08:00, 20:30"` |
| `enabled` | bool | 是否启用调度 |
| `preferred_keywords` | string[] | 偏好学习产生的正向词（系统维护） |
| `excluded_keywords` | string[] | 偏好学习产生的排除词（系统维护） |
| `last_run` | string\|null | 上次执行时间 |
| `next_run` | string\|null | 下次计划执行时间 |
| `created_at` | string | 创建时间 |

### GET /api/tasks  🔒

返回完整任务列表（按 id 倒序）。

**返回** `200`：`Task[]`

### GET /api/tasks/public  🔓

公众简报页任务切换用，仅投影 `id` 与 `name`，不暴露关键词/来源。

**返回** `200`

```json
[ { "id": 7, "name": "AI 大模型动态" } ]
```

### POST /api/tasks  🔒

创建任务。创建后服务端会立即异步执行一次。

**请求体**

| 字段 | 必填 | 说明 |
|---|:--:|---|
| `name` | ✓ | 任务名，不能为空 |
| `schedule_value` | ✓ | 见 Task 说明 |
| `schedule_type` | 否 | `interval`（默认）/ `daily` |
| `keywords` | 否 | 关键词数组 |
| `sources` | 否 | 来源 id 数组 |
| `enabled` | 否 | 默认 `true` |

**返回** `201`：新建 `Task` 完整对象。参数非法返回 `400 {"error": ...}`（如「任务名不能为空」「需要 schedule_value」「schedule_type 必须是 interval 或 daily」）。

### PUT /api/tasks/{id}  🔒

更新任务可编辑字段（`last_run`/`next_run`/`created_at` 保留）。请求体同 `POST /api/tasks`；另可携带 `preferred_keywords` / `excluded_keywords` 增量更新偏好标签。

**返回** `200`：更新后的 `Task`；任务不存在 `404`；参数非法 `400`。

### DELETE /api/tasks/{id}  🔒

删除任务，并级联清除其文章、简报、反馈数据。

**返回** `200 {"ok": true}`

### POST /api/tasks/{id}/run  🔒

立即触发一次侦听（异步执行，不等待结果）。

**返回** `200 {"ok": true, "message": "已开始执行"}`；任务不存在 `404`。

---

## 四、资讯（情报流）

资讯对象（`Article`）字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `task_id` | int | 所属任务 |
| `title` | string | 标题 |
| `url` | string | 原文链接 |
| `summary` | string | 摘要（已去 HTML，截断 500 字） |
| `source` | string | 来源 id |
| `author` | string | 作者，可能为空串 |
| `published_at` | string\|null | 发布时间 |
| `engagement` | int | 热度（点赞/评论/播放等，依来源而定） |
| `score` | float | 综合评分（新鲜度 + 热度 + 来源质量） |
| `url_hash` | string | URL 归一化哈希（去重用） |
| `fetched_at` | string | 抓取入库时间 |

### GET /api/articles  🔓

列资讯，支持过滤、排序、分页。

**Query 参数**

| 参数 | 必填 | 默认 | 说明 |
|---|:--:|---|---|
| `task_id` | 否 | — | 指定任务；缺省则跨任务聚合 |
| `source` | 否 | — | 按来源 id 过滤 |
| `q` | 否 | — | 标题/摘要关键词过滤（不区分大小写） |
| `sort` | 否 | `score` | `score`（热度/综合分）/ `time`（最新） |
| `limit` | 否 | `50` | 上限 200 |
| `offset` | 否 | `0` | 分页偏移 |

**返回** `200`：`Article[]`

---

## 五、每日简报

### GET /api/digest  🔓

读取某任务的简报快照（快照式，每次定时任务跑完自动生成存盘）。

**Query 参数**

| 参数 | 必填 | 说明 |
|---|:--:|---|
| `task_id` | ✓ | 缺失返回 `400`；任务不存在返回 `404` |

**返回** `200`：简报对象；尚无快照时返回 `{"empty": true}`。

简报对象字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `task_id` / `task_name` | int / string | 任务标识 |
| `total` | int | 文章总数 |
| `summarized` | int | 参与聚类/综述的文章数 |
| `source_count` | int | 涉及来源数 |
| `generated_by` | string | 生成方式，如 `algo · 标题聚类 + 词频摘要` 或模型名 |
| `generated_at` | string | 生成时间 |
| `summary` | string | 综述文本 |
| `topics` | object[] | 主题聚类，见下 |
| `hot` | object[] | 热点榜，见下 |
| `keywords` | array[] | 关键词，每项为二元数组 `[词, 次数]`，如 `["AI", 12]` |
| `empty` | bool | 是否无数据 |

`topics[]` 项：`{ "title": 代表标题, "count": 条数, "engagement": 热度合计, "items": [{title, url, source}] }`（items 最多 6 条；其中 `source` 为来源**显示名**）

`hot[]` 项：`{ "title", "url", "source", "engagement", "score" }`（最多 12 条；其中 `source` 为来源 **id**）

### POST /api/digest/{id}/refresh  🔒

手动重算简报（同步，含 LLM 调用，可能 10–20 秒）。

**返回** `200`：新生成的简报对象；任务不存在 `404`；无内容返回 `{"empty": true}`。

---

## 六、偏好反馈

### GET /api/feedback  🔒

某任务的赞踩信号映射，供前端回显卡片的 👍👎 态。

**Query 参数**：`task_id`（必填，缺失 `400`）

**返回** `200`：`{ "<url>": "like" | "dislike" }` 映射（无信号的 url 不出现）。

### POST /api/feedback  🔒

提交/取消对某条资讯的赞踩。`signal=none` 表示取消。

**请求体**

| 字段 | 必填 | 说明 |
|---|:--:|---|
| `task_id` | ✓ | 任务 ID |
| `url` | ✓ | 资讯 URL |
| `signal` | ✓ | `like` / `dislike` / `none` |
| `title` | 否 | 资讯标题（留存用） |

**返回** `200 {"ok": true}`；缺 `task_id`/`url` 返回 `400`；`signal` 非法 `400`；任务不存在 `404`。

---

## 七、执行日志

### GET /api/runs  🔒

列出执行记录（按 id 倒序，最多保留最近 200 条）。

**Query 参数**

| 参数 | 必填 | 默认 | 说明 |
|---|:--:|---|---|
| `task_id` | 否 | — | 按任务过滤 |
| `limit` | 否 | `20` | 上限 100 |

**返回** `200`：数组，单条字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | int | 记录 ID |
| `task_id` | int | 所属任务 |
| `status` | string | 执行状态 |
| `started_at` / `finished_at` | string | 起止时间 |
| `stats` | object | 本轮统计（各源抓取量/入库量/新增量等明细） |

---

## 八、模型设置

### GET /api/settings  🔒

模型配置脱敏回显（不返回 key 明文）。

**返回** `200`

```json
{ "provider": "auto", "base_url": "", "model": "", "has_key": false }
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `provider` | string | `auto` / `openai` / `anthropic` / `ollama` / `off` |
| `base_url` | string | 接口地址 |
| `model` | string | 模型名 |
| `has_key` | bool | 是否已配置 API key（不回显明文） |

### PUT /api/settings  🔒

保存模型配置（写回 `config.json` 的 `llm` 节，保留 `admin_token`/`server`）。

**请求体**

| 字段 | 必填 | 说明 |
|---|:--:|---|
| `provider` | 否 | 默认 `auto`；非 `auto/ollama/openai/anthropic/off` 返回 `400` |
| `base_url` | 否 | 接口地址 |
| `model` | 否 | 模型名 |
| `api_key` | 否 | API key（不传则保留原值） |

**返回** `200`：脱敏后的配置（同 `GET /api/settings`）。

### POST /api/settings/test  🔒

测试模型连接（存在 SSRF/费用风险，故受保护）。请求体同 `PUT /api/settings`。

**返回** `200`

```json
{ "ok": true,  "model": "glm-4", "message": "连接成功" }
{ "ok": false, "error": "未知 provider 或缺少配置" }
```

---

## 九、GitHub 趋势榜

数据来自 OSSInsight（免 key），按 `(period, language)` 维度缓存在服务端进程内存中（快照式，不落盘）。回源拉取只发生在两条受控路径：**①定时预热**（启动 + 每日 08:30，仅 `language=All`）、**②管理员刷新**。

两接口取值一致：

- `period`：`today` / `week` / `month`，默认 `today`
- `language`：`All` / `Python` / `JavaScript` / `TypeScript` / `Go` / `Rust` / `Java` / `C++` / `C` / `Swift` / `Kotlin`，默认 `All`

### GET /api/trending  🔓

读取当前 `(period, language)` 的缓存快照；未命中时服务端回源一次再返回。

**Query 参数**：`period`、`language`（均可选，见上）

**返回** `200`：榜单数组（按趋势分降序，无数据 `[]`）。单条字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `rank` | int | 排名，从 1 递增 |
| `name` | string | 仓库全名 `owner/repo` |
| `url` | string | 仓库地址 |
| `language` | string | 主语言，可能为空串 |
| `description` | string | 描述，可能为空串 |
| `stars` / `forks` | int | Star / Fork 数 |
| `score` | float | 趋势分（1 位小数） |
| `contributors` | string[] | 贡献者用户名，最多 3 个 |

> 返回 `[]` 时前端展示「暂无趋势数据」占位：可能该组合尚未预热或数据源瞬时无响应。

### POST /api/trending/refresh  🔒

强制回源 OSSInsight，重拉**当前 `(period, language)` 组合**并更新缓存。消耗数据源配额，仅管理态调用；同步阻塞，等待数据源返回（通常数秒）。

**请求体**：`{ "period": "today", "language": "All" }`（字段均可选，见上）

**返回**

| 状态码 | 响应体 | 说明 |
|---|---|---|
| `200` | 榜单数组 | 刷新后的榜单，结构同 `GET /api/trending` |
| `401` | `{"error": "未授权"}` | 未携带或令牌无效 |

> 前端注意：
> - 刷新入口仅管理态展示，公开态隐藏；按钮即便意外可见，后端仍以 `401` 兜底。
> - 回源拉空但有旧缓存时，本接口返回**旧快照**——前端无法仅凭返回值区分「已更新」与「拉取失败保留旧值」，成功提示建议中性化为「已刷新」。
> - 刷新期间禁用按钮 + 加载态，成功后用返回值或重新 `GET /api/trending` 渲染。

---

## 变更

- 2026-06-16：统一规划全量接口文档；新增 `POST /api/trending/refresh`（管理员强制刷新）并明确 `GET /api/trending` 公开读缓存语义。
