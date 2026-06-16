# 热门 Skill 仓库榜（Hot Skill Repos Board）设计 · v2

- 日期：2026-06-16
- 状态：待评审（已纳入 Codex 对抗式审查意见，见第 10 节）
- 定位：**准公开榜单**（对外可见、需建立排名可信度）
- 适用项目：拾光 · GlowFeed（零依赖、零 API key 的多源资讯定时聚合系统）

## 0. M0 Spike 实测结论与方案调整（2026-06-16，覆盖正文相关条目）

实机验证三个外部源后据实调整（"先验证再投入"）：

| 源 | 本机直连 | 实测 | 首版决策 |
|---|---|---|---|
| GitHub repo tree（集合展开） | ✅ | `anthropics/skills` 一次 recursive 拿 18 个 SKILL.md，`truncated:false`，core 限流 60/h | **采用**，集合展开核心来源 |
| GitHub search | ✅ | **限流 10 次/分钟**（未认证 search API，非 core 的 60/h）；`topic:claude-code-skills` 召回混入大量 `awesome/best-practice/list` 类非真 skill | **采用**，但严控请求数 + **召回后过滤**剔除名字/topic 含 `awesome|best-practice|list|collection|cheatsheet|tutorial` 的；优先集合白名单 + tree 展开作真 skill 来源 |
| skills.sh /trending | ✅(200) | 纯 HTML 正则**解析不出榜单**：无 `__NEXT_DATA__`/`/api`，数据藏 App Router RSC 流(`__next_f`) | **首版放弃**：抓取脆弱、维护成本高、违背零依赖/失败容忍。**installs 维度整体留 v2** |

**对正文的影响（以本节为准）**：
- 删除所有 `skillsh_trending` 采集与 `skillsh_installs` 信号；热门榜排序**仅按 GitHub stars**（独立 repo）+ 集合展开。
- 实体 `type` 取值改为 `standalone | collection`（去掉 `skillsh`）。
- 热门榜 period 维持「近期」一档（GitHub search 本就拿近期高星库）。
- 预算表：GitHub search 严格 ≤ 数次/轮（受 10/min 约束）；删 skills.sh 行。
- 飙升榜 Δ、口碑榜（不依赖 skills.sh）不受影响。

## 1. 背景与目标

追踪 **Claude Code / Codex 生态里热门、上升快的 Agent Skill**，并补充「博客/媒体反复推荐」的口碑视角。复用 GlowFeed 已有的趋势榜机制（`sources.github_trending_list()`：OSSInsight、免 key、快照缓存 + 每日定时刷新 + 独立只读专页）。

### 目标
1. 一个独立专页，内部三榜：热门榜、飙升榜、口碑榜。
2. 坚持硬约束：**仅 Python 标准库、零 API key、失败容忍**。
3. **准公开定位 → 排名口径必须可信、可解释、可区分新旧/失败状态**。

### 非目标（YAGNI）
- 不做 skill 的安装/管理/执行（只做发现与排行）。
- 不做用户订阅/告警/历史趋势图。
- **首版不做 skill 级实体抽取**（不解析每个 repo 的 SKILL.md 目录树）——见第 3 节，留 v2。
- 不接需付费/强制 key 的源（SkillsMP 带 key 档暂不接）。

## 2. 调研结论（数据源）

| 源 | 提供 | 接入 | 档位 |
|---|---|---|---|
| GitHub Search API（项目已用 `fetch_github`） | `topic:claude-code-skills`、`q="SKILL.md"`，按 stars + `pushed:>近期` | 复用现成代码 | ✅ 免 key 直连 |
| OSSInsight 趋势 API（项目已用 `github_trending_list`） | 仓库级 trending score | 复用现成代码 | ✅ 免 key（**仅作可选「仓库趋势候选」，不混入飙升榜**，见 F2 处理） |
| skills.sh `/trending`（Vercel-labs） | rank + installs（SSR HTML） | HTML 正则抓取 | ✅ 免 key（脆弱，需容错） |
| ClaudeMarketplaces / SkillsMP / awesome-* | 更多 stars/install/精选 | 抓 HTML / 带 key / 静态清单 | 🟡 首版不接 |

**关键判断**：外部站给的多是「存量榜」（总 stars/installs）。「上升快」需要增量/时间序列（ΔStars、ΔInstalls）——这正是 GlowFeed 作为定时快照系统的天然优势：自己存两次快照做差。

## 3. 核心决策：统一 repo 粒度（回应 F1）

**首版正名为「热门 Skill 仓库榜」，全程以 `owner/repo` 为唯一 canonical 实体。** 理由：skill 在现实中三种形态——独立 repo 型有自身 stars；集合型（`anthropics/skills` 一个 repo 装几十个）单个 skill 无独立 stars；纯名字型只有名字。若热门/飙升按 repo、口碑按 skill 名混排，会导致**排名不可比 + 集合型仓库系统性霸榜**（codex F1）。

统一口径后：
- 热门/飙升榜：repo 实体，指标 = repo stars/installs 及其 Δ。
- **集合型仓库单列**：标记 `is_collection=true`，放专页内独立「📦 精选集合」区，**不混入主榜与单体竞争**。收录标准：repo 内含 ≥ N 个 SKILL.md（首版可用启发式：repo 名/topic 命中 + skills.sh 标注），N 待定（建议 ≥ 5）。
- 口碑榜：见第 5 节，证据优先对齐到 repo；纯名字型降级到「待证实」次级区，不进主榜。
- **v2 演进**：再做 skill 级抽取（拆集合 repo 的子 skill，repo 指标降为来源信号）。

## 4. 三榜结构

仿现有趋势专页，三分区（Tab），各支持 `today/week/month`（飙升榜 48h/7d/30d）：

| 分区 | 问题 | 数据源 | 排序 | 主榜门槛 |
|---|---|---|---|---|
| 🔥 热门榜 | 现在最火 | GitHub Search + skills.sh installs | stars/installs 存量 | repo 可解析 |
| 🚀 飙升榜 | 最近涨最快 | 自建快照做差（**无 OSSInsight 伪装**） | repo ΔStars/ΔInstalls | history ≥ 2 天，否则显「积累中」 |
| ⭐ 口碑榜 | 博客反复推荐 | Bing/HN → LLM 候选 + 确定性校验 | 去重后 mention_count | 命中 repo/可信目录 |

**取舍：三榜并列不做综合评分**——三种信号量纲/语义不同，加权融合会把排名机制变黑箱；分开则每榜口径一句话讲清。

统一实体：
```
SkillRepo {
  id(owner/repo), name, url, is_collection,
  signals: { github_stars, pushed_at, skillsh_installs, blog_mentions: [{domain, author, title, url}] },
  delta:   { stars, installs },   # 可空
  agents:  [claude-code, codex, cursor]
}
```

## 5. 数据流与关键子流程

```
采集  github_skill_search() · skillsh_trending(HTML) · blog_mention_search(Bing/HN+LLM)  [OSSInsight 可选候选]
  ↓
归一  normalize → 按 owner/repo 去重合并；标记 is_collection
  ↓
快照  data/skills/{board}_{period}.json (当前) + history/{date}.json (做差)
  ↓
计算  飙升榜 = 当前 − 最近 history；不足 2 天 → 状态 "warming-up"
```

### 飙升榜冷启动（回应 F2）
**不再用 OSSInsight 伪装成飙升榜**（口径不一致、首屏失真、后期跳变）。改为：history < 2 天时**诚实显示「📈 历史积累中，N 天后开放」**，符合 GlowFeed「失败容忍、诚实降级」精神。OSSInsight 若保留，**单独标为「仓库趋势候选」区**（默认可关），与飙升榜物理隔离、文案明示口径不同。

### 口碑榜反作弊（回应 F4）
1. `fetch_bing`/`fetch_hackernews` 多 query 搜文章（中英）。
2. **LLM 只做候选抽取** → `{候选 skill 名/可能 repo 链接, 推荐理由, 来源 domain+author}`。
3. **确定性校验 + canonicalization**：候选必须对齐到 GitHub repo 或可信目录命中，才能进**主榜**；对不上的进「待证实」次级区。
4. **去重**：按 (独立 domain, author) 去重，**每域对同一 skill 的 mention 计 1**（防同站/同作者软文刷高）。
5. 排序：去重后 mention_count。

## 6. 代码落点

| 模块/改动 | 职责 | 依赖 |
|---|---|---|
| `app/skills_sources.py`（新） | 纯采集，失败返 `[]` | `http_util` |
| `app/skills_board.py`（新） | 归一去重 → 三榜计算 → 快照读写；`build_board()` / `warm_skills()` | `skills_sources`、`store`、`llm`、`sources`(可选 OSSInsight 候选) |
| `store.py`（薄改） | `read_skills_board()` / `save_skills_board()` / `archive_skills_history()` | — |
| `server.py`（薄改） | 加 2 路由 | `skills_board` |
| `scheduler.py`（薄改） | `SKILLS_REFRESH_TIMES` + 启动预热 | `skills_board` |

### 存储
```
data/skills/  hot_today.json  rising_week.json  praise_today.json ...
data/skills/history/2026-06-16.json
```

## 7. 请求预算与 staleness 语义（回应 F3）

**预算闭环（一轮预热的外部请求上限）：**

| 源 | 请求数 | 说明 |
|---|---|---|
| GitHub Search | 3–6 | 3 个 query；**period 不分别打**——一次拉足量按时间本地切 today/week/month |
| skills.sh | 1 | `/trending` 一页 |
| 博客（Bing+HN） | 6–8 | + LLM 抽取 1–2 次 |
| **合计** | **~15–20** | 其中 GitHub 仅占 ~6，**远低于免 key 60 次/小时** |

- **公开 GET 读快照不打外网**；只有每日 08:40 预热 + 管理员 force 才真拉。
- **管理员 force 冷却**：同 `type` 5 分钟内不重复真拉。
- **退避**：单源限流/失败 → 保留旧快照，标 `status`。

**响应暴露状态**（每个 board）：
```
GET /api/skills/board?type=hot|rising|praise&period=today|week|month
→ { rows, snapshot_time, sources: [{ id, status: ok|stale|ratelimited|empty, fetched_at }] }
POST /api/skills/refresh {type?, period?}   管理员·带冷却
```
前端据 `sources[].status` 区分「新数据 / 旧快照 / 限流失败 / 空」，不让用户误把旧/失败当最新。

## 8. 调度
`SKILLS_REFRESH_TIMES=["08:40"]`（错开趋势榜 08:30）。刷新：拉新 → 存当前 → 与最近 history 做差 → 今日归档 history/。启动预热一次。

## 9. 前端 / 测试
- 前端：仿趋势专页加「热门 Skill」view，`type` 三 Tab + 集合区 + period 下拉；顶部显示 `snapshot_time` 与各源 `status` 角标。公开只读，带 token 显示「重新生成」。
- 测试：`skills_sources` 用录制夹具测解析与失败容忍；`skills_board` 测去重合并、三榜排序、飙升做差（构造两份 history）、冷启动 warming-up 分支、口碑去重/证据门槛；`server` 测路由鉴权与 staleness 字段。

## 10. Codex 对抗式审查处理记录

| # | 严重度 | 意见 | 处理 |
|---|---|---|---|
| F1 | high | 不同榜不同粒度 → 不可比 + 集合霸榜 | **接受**：统一 repo 粒度、正名「仓库榜」、集合型单列；skill 级抽取留 v2（第 3 节） |
| F2 | high | OSSInsight 兜底口径不一致 → 首屏失真/跳变 | **接受**：删除伪装，冷启动诚实显「积累中」；OSSInsight 仅作隔离的「仓库趋势候选」（第 5 节） |
| F3 | medium | 免 key 配额无闭环 | **部分接受**：补预算表 + 冷却 + staleness 响应字段（第 7 节）；但反驳「很快空榜」——快照架构下 GitHub 仅 ~6 请求/轮 |
| F4 | medium | 口碑榜无反作弊/校验 | **接受**：证据优先入主榜、按域名+作者去重、LLM 只做候选（第 5 节）；纯名字型留「待证实」区（保留发现冷门好 skill 的初衷） |

## 11. 仍未决（首版需实测/定义）
1. skills.sh SSR HTML 结构变更会破坏抓取——需容错 + 空榜监控告警。
2. 本机能否直连 skills.sh / GitHub API 未验证（OSSInsight 注释称本机直连可用）。
3. 「精选集合」收录阈值 N 与启发式判定标准待定。
4. 口碑榜「可信目录」白名单具体包含哪些站（awesome-* / ClaudeMarketplaces 等）待定。
