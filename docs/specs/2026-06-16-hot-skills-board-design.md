# 热门 Skill 榜（Hot Skills Board）设计

- 日期：2026-06-16
- 状态：待评审（设计已与用户对齐，尚未实现）
- 适用项目：拾光 · GlowFeed（零依赖、零 API key 的多源资讯定时聚合系统）

## 1. 背景与目标

用户希望新增一个「类似 GitHub 趋势榜」的功能：追踪 **Claude Code / Codex 生态里热门、以及上升特别快的 Agent Skill**，并补充「博客/媒体反复推荐」的口碑视角。

GlowFeed 已有一个 GitHub 趋势榜专页（`sources.github_trending_list()` 基于 OSSInsight，免 key，快照式缓存 + 每日 08:30 刷新 + 独立只读专页）。本功能复用这套成熟机制，新增针对 skill 的采集与榜单组织。

### 目标
1. 一个独立「热门 Skill」专页，内部三个榜：热门榜、飙升榜、口碑榜。
2. 全程坚持项目硬约束：**仅 Python 标准库、零 API key、失败容忍**。
3. 复用现有快照缓存、调度、降级、LLM 综述机制，新增代码职责单一、可单测。

### 非目标（YAGNI）
- 不做 skill 的安装/管理/执行（只做发现与排行）。
- 不做用户订阅/告警/历史趋势图（首版只出当前榜单）。
- 不做跨多语言编程语言维度切分（趋势榜已有 language 维度，skill 榜首版不切）。
- 不接入需要付费/强制 API key 的源（SkillsMP 带 key 档暂不接）。

## 2. 调研结论（数据源）

按「能否直接喂进 GlowFeed 免 key 架构」分档：

| 源 | 提供 | 「上升快」信号 | 接入方式 | 档位 |
|---|---|---|---|---|
| GitHub Search API（项目已用 `fetch_github`） | `topic:claude-code-skills`、`q="SKILL.md"`，按 stars 排序 + `pushed:>近期` | 中（stars 存量 + 近期活跃） | 复用现成代码改 query | ✅ 免 key 直连 |
| OSSInsight 趋势 API（项目已用 `github_trending_list`） | 仓库级 trending score（GH Archive 事件算的真趋势分） | 强（本身即增量） | 复用现成代码 + skill 过滤 | ✅ 免 key 直连 |
| skills.sh `/trending`（Vercel-labs，跨 Claude Code/Codex/Cursor） | rank + installs（SSR 纯 HTML） | 中（installs 存量，无时间序列） | HTML 正则抓取（同 baidu/weibo 抓法） | ✅ 免 key（抓 HTML） |
| ClaudeMarketplaces.com | 21,600+ skills，按 install/stars/votes 排序 | 中 | 抓 HTML，结构自解析 | 🟡 备选，首版不接 |
| SkillsMP REST API | 1.7M skills，`sortBy=stars` | 无 trending 端点 | 匿名 50/天，带 key 500/天 | 🔑 首版不接 |
| awesome-* 仓库（hesreallyhim / VoltAgent / Composio） | 静态精选清单 | 无 | 作为种子白名单（可选） | 🟡 可选 |

关键判断：**外部站给的几乎都是「存量榜」（总 stars / 总 installs），只反映「现在火」。「上升特别快」需要增量/时间序列（ΔStars、ΔInstalls）——这正是 GlowFeed 作为定时快照系统的天然优势：自己存两次快照做差即可得到任何外部 API 都给不了的「飙升榜」。**

## 3. 总体结构：一个专页，三种榜

仿现有趋势专页，新增「热门 Skill」专页，内部三个分区（Tab），各支持 `today / week / month`（飙升榜为 48h / 7d / 30d）切换：

| 分区 | 回答的问题 | 数据源 | 排序依据 | 实体粒度 |
|---|---|---|---|---|
| 🔥 热门榜 | 现在最火 | GitHub Search + skills.sh installs | stars / installs 存量 | repo |
| 🚀 飙升榜 | 最近涨最快 | 自建快照做差，OSSInsight 兜底 | 48h/7d ΔStars、ΔInstalls | repo |
| ⭐ 口碑榜 | 博客反复推荐 | Bing/HN 搜文章 → LLM 抽取 | 被多少篇近期文章点名 | skill 名 |

**取舍：三榜并列，不做综合评分。** 三种信号（存量 / 增量 / 口碑）量纲与语义不同，加权融合会把排名机制藏进黑箱、用户无法理解排序来源；分开则每榜口径一句话讲清，也更贴近 GitHub trending 的 today/week/month 直觉。

## 4. Skill 实体模型（核心难点）

skill 在现实中有三种形态，**不假装都能算独立 stars**：
1. 独立 repo 型（如 `scrapegraphai/just-scrape`）——有自身 stars。
2. 集合型（如 `anthropics/skills`、`vercel-labs/skills` 一个 repo 装几十个 skill）——单个 skill 无独立 stars，只有所在 repo 的。
3. 纯名字型（博客点名「用 xxx skill」，可能无链接）。

处理策略：**不同榜用不同粒度**（诚实而非偷懒）——
- 热门 / 飙升榜：**repo 粒度**（stars/installs 信号本就是 repo 级；集合型显示「`anthropics/skills` ⭐X · 含 N 个 skill」）。
- 口碑榜：**skill 名粒度**（博客点名的是具体 skill 名，按名聚合）。

统一实体（以 `owner/repo` 为主键去重合并信号）：
```
Skill {
  id, name, repo, url,
  signals: { github_stars, pushed_at, skillsh_installs, blog_mentions: [{title, url}] },
  delta:   { stars, installs },          # 自建增量，可空
  agents:  [claude-code, codex, cursor]  # best-effort
}
```
博客抽取出的名字优先用 LLM 带回的 repo 链接对齐到已有实体；对不上的，**允许「只有名字」的条目单独留在口碑榜**，不强行编造 repo。

## 5. 数据流（四层）

```
采集  github_skill_search() · ossinsight(已有) · skillsh_trending(HTML抓) · blog_mention_search(Bing/HN + LLM)
  ↓
归一  normalize → 按 owner/repo 去重合并成 Skill 实体
  ↓
快照  data/skills/{board}_{period}.json (当前·页面秒读)  +  history/{date}.json (算 Δ 用)
  ↓
计算  飙升榜 = 当前快照 − 最近历史快照
```

### 口碑榜子流程（复用 `llm.py`）
1. `fetch_bing` 多 query 搜（中英）：`best claude code skills`、`top codex skills`、`claude skill 推荐` 等。
2. 拿到文章 title/url/summary。
3. LLM 抽取 → `{skill 名, 一句推荐理由, 来源文章}` 结构化 JSON。
4. 跨文章聚合：被 N 篇点名 → `mention_count=N` 排序。
- 降级：模型未配置时，口碑榜退化为「文章标题正则提名」或置空提示「配置模型后启用」。

### 飙升榜冷启动
首次运行无历史快照 → 飙升榜会空。解法：**用已有 OSSInsight 趋势分兜底**（它本身按事件增量算，不需自建历史）；待自建快照攒够 ≥ 2 天，再切到「真 Δ」或两者并用。

## 6. 代码落点（贴现有结构）

| 模块/改动 | 职责 | 依赖 |
|---|---|---|
| `app/skills_sources.py`（新） | 纯采集：`github_skill_search()` / `skillsh_trending()` / `blog_mention_search()`，失败返 `[]` | `http_util` |
| `app/skills_board.py`（新） | 编排：归一去重 → 三榜计算 → 快照读写。`build_board(type, period)` / `warm_skills()` | `skills_sources`、`store`、`llm`、`sources`(借 OSSInsight) |
| `store.py`（薄改） | `read_skills_board()` / `save_skills_board()` / `archive_skills_history()`，仿 `read_digest` | — |
| `server.py`（薄改） | 加 2 路由 | `skills_board` |
| `scheduler.py`（薄改） | 加 `SKILLS_REFRESH_TIMES` + 启动预热 | `skills_board` |

边界：sources 管「怎么拿」，board 管「怎么组织成榜」，server 管「怎么暴露」——各自可单测。

### 存储布局
```
data/skills/
  hot_today.json  rising_week.json  praise_today.json ...   # 当前快照
  history/2026-06-16.json                                   # 每日归档，飙升榜做差
```

### HTTP 接口（仿 `/api/trending`）
```
GET  /api/skills/board?type=hot|rising|praise&period=today|week|month   公开·读快照
POST /api/skills/refresh  {type?, period?}                              管理员·force 真拉
```

### 调度（仿趋势榜，多一步归档）
`SKILLS_REFRESH_TIMES=["08:40"]`（错开趋势榜 08:30，避免同时打外网）。刷新动作：拉新数据 → 存当前快照 → 与最近 history 做差算飙升 → 今日数据归档到 history/。启动预热一次。

### 降级矩阵（沿用「拉空保留旧快照」原则）
| 失败点 | 行为 |
|---|---|
| GitHub API 限流 | 热门榜该维度置空，其余源补；保留旧快照 |
| skills.sh 抓取失败 | installs 维度缺失，stars 维度照常 |
| LLM 未配置 | 口碑榜降级为标题正则提名 / 置空提示 |
| history < 2 天（冷启动） | 飙升榜走 OSSInsight 趋势分兜底 |

### 前端
`web/` 仿现有趋势专页加「热门 Skill」view + 导航入口；内部 `type` 三 Tab、`period` 下拉复用趋势页组件。公开只读，带 token 显示「重新生成」。

## 7. 测试策略
- `skills_sources`：各 fetcher 用录制的样本响应（HTML/JSON 夹具）单测解析与失败容忍（返 `[]`）。
- `skills_board`：归一去重（同 repo 多源合并）、三榜排序、飙升做差（构造两份历史快照）、冷启动兜底分支。
- `server`：路由鉴权（公开 GET / 管理员 refresh）、参数校验。
- 沿用项目现有 `tests/` 结构。

## 8. 已知风险与未决假设（供评审 challenge）
1. **skills.sh 是 SSR HTML，结构变更会破坏抓取**——无 API 契约保证，需容错 + 监控空榜。
2. **本机能否直连 skills.sh / GitHub API / OSSInsight 未在实现期验证**（OSSInsight 项目注释称本机直连可用；GitHub API 免 key 有 60 次/小时限流，可能不够三榜 × 三周期刷新）。
3. **集合型 skill 的 repo 粒度**会让 `anthropics/skills` 这类「巨型集合」长期霸榜，可能淹没真正的单体新星——是否需要对集合型降权或单列待定。
4. **口碑榜依赖 LLM 抽取的准确性**：LLM 可能虚构 skill 名或错误归类；mention_count 易被单一作者的多篇软文刷高。
5. **飙升榜冷启动用 OSSInsight 兜底**，但 OSSInsight 是仓库级趋势，未必能过滤出「skill 类」仓库，兜底质量存疑。
6. **GitHub API 限流**下三榜 × 三周期的刷新预算是否够，需要实测；可能需要合并请求或降低刷新频率。
