# 拾光 · GlowFeed

参考 [last30days skill](https://github.com/mvanhorn/last30days-skill) 的多源检索思路构建的
**全网中外资讯定时聚合系统**。零依赖（仅 Python 标准库）、**零 API key**、零 npm/pip 安装。

## 启动

需要 Python 3.8+，无需安装任何第三方依赖。

```bash
git clone <repo-url>
cd glowfee
cp config.example.json config.json   # 可选；不复制则首次启动自动生成
python3 run.py                        # 默认 http://127.0.0.1:8787
python3 run.py --port 9000            # 命令行参数覆盖 config.json
python3 run.py --token 你的固定token   # 线上建议固定 token，保持管理链接稳定
```

启动时控制台会打印**管理链接** `http://.../?token=xxx`。公众访问根地址只能只读简报与情报流；
带 token 访问才显示任务/日志/设置菜单并可操作。

## 配置（仓库根 `config.json`）

所有部署配置集中在仓库根 **`config.json`**，与运行时数据 `data/` 分离：

```json
{
  "server": { "host": "127.0.0.1", "port": 8787, "data_dir": "data" },
  "admin_token": "",
  "llm": { "provider": "auto", "base_url": "", "model": "", "api_key": "" }
}
```

- 仓库只提交脱敏模板 **`config.example.json`**；真实 `config.json`（含 `admin_token`、`api_key`）已在 `.gitignore` 中排除，**绝不入库**。
- 三处取值优先级：**命令行参数 > `config.json` > 内置默认值**（`host=127.0.0.1`、`port=8787`、`data_dir=./data`）。
- `server.data_dir`：运行时数据目录（相对路径相对启动目录解析）；命令行 `--data` 优先级最高。
- **零配置启动**：不存在 `config.json` 时首次启动自动生成（随机 `admin_token` + `provider:auto`）并持久化，重启后 token 固定不变。
- **`llm` 节**也可不手填——在网页「模型设置」页填写并保存，会写回 `config.json` 的 `llm` 节（保留 `admin_token`/`server` 不变）。
- `admin_token` 来源优先级：命令行 `--token` > `config.json` 的 `admin_token` > 自动生成。

## 功能

- **每日简报**（默认首页）：把零散资讯归纳成「综述 + 主题聚类 + 热点榜 + 关键词」。简报是**快照式**的——每次定时任务跑完自动生成一份存盘，页面打开秒读，不实时重算；也可手动「重新生成」
- **侦听任务**：自定义关键词（多个，命中任一即收录）+ 信息源 + 定时方式
  - 固定间隔：每 N 分钟（下限 5 分钟）
  - 每日定点：如 `08:00, 12:30, 20:00`
- **后端定时执行**：后台调度线程按任务配置自动检索 → 过滤 → 去重 → 评分 → 入库
- **情报流渲染**：按任务/来源/关键词筛选，按热度或时间排序，定时产出自动刷新
- **执行日志**：每轮抓取量、入库量、新增量、各源明细

## 资讯归纳总结

不想看一堆卡片？「每日简报」把同主题资讯聚成话题、排出热点、提炼关键词，并生成一段综述。

**生成时机**：简报在每次定时任务抓取后自动生成一份快照存到 `data/digests/{task_id}.json`，
页面打开直接读快照（约 40ms），不会每次重算、也不会重复调用模型。只有定时任务有新增内容、
或用户点「重新生成」时才会真正跑一次聚类与模型综述（约 10–20 秒）。

综述的生成方式在「模型设置」页配置，五选一：

| 接入方式 | 说明 | 是否需要 Key |
|---------|------|------------|
| 自动（默认） | 探测本机 Ollama，有则用、无则算法摘要 | 否 |
| OpenAI 风格 | `/chat/completions`，兼容 OpenAI / DeepSeek / Moonshot / 通义 / OneAPI / vLLM 等 | 是 |
| Anthropic 风格 | `/v1/messages`（Claude） | 是 |
| Ollama | 本地或远程 Ollama，可自定义地址 | 否 |
| 关闭 | 始终用算法摘要 | 否 |

- **算法摘要**（保底）：标题 bigram 聚类成主题、跨标题词频提炼关键词（含中文新词发现去碎片）、热度榜、模板综述。零依赖、开箱即用。
- **云端 / 本地大模型**：在设置页填 API 地址、模型名、Key，点「测试连接」验证后保存。Key 存本地 `config.json` 的 `llm` 节，页面只回显「已设置」不显示明文。云端请求被墙时自动尝试本地代理。
- 任意一级失败都会静默降级到算法摘要，简报始终可用。

## 信息源（全部免 API key，单源失败不影响整体）

| 来源 | 类型 | 说明 |
|------|------|------|
| Hacker News | 关键词检索 | Algolia 公开 API |
| GitHub | 关键词检索 | 公开搜索 API（未认证 10 次/分钟） |
| Bing 网页 | 关键词检索 | cn.bing.com 的 RSS 输出，中英文皆覆盖 |
| Reddit | 关键词检索 | search.rss；直连不通时自动探测本地代理（Clash 等常见端口） |
| 百度热搜 / 微博热搜 / 头条热榜 / B站热门 | 热榜 | 公开接口，按关键词本地过滤（关键词留空则全量收录） |
| 36氪 / IT之家 / 少数派 | RSS | 按关键词本地过滤 |

## 管线设计（参考 last30days）

```
并发抓取 → 关键词过滤 → 评分(0.45新鲜度 + 0.30 log热度 + 0.25源质量)
→ 去重(URL归一化 + 标题字符bigram Jaccard≥0.72，中英文通用)
→ SQLite 入库(跨轮按 url_hash 幂等)
```

## 结构

```
run.py              启动入口（解析配置，引导服务）
config.example.json 配置模板（入库；复制为 config.json 后填值）
config.json         真实配置（admin_token + server + llm；被 .gitignore 排除）
app/
  server.py         HTTP 服务 + REST API（纯标准库）
  scheduler.py      定时调度线程（interval / daily）
  pipeline.py       过滤、去重、评分、入库
  digest.py         每日简报：聚类 + 关键词 + 热点 + 综述
  llm.py            LLM 接入层（OpenAI / Anthropic / Ollama 多 provider）
  sources.py        11 个免 key 信息源
  http_util.py      直连优先 + 本地代理自动探测
  store.py          本地 JSON 文件存储（无数据库）+ 配置读写
web/                单页前端（vanilla JS，零外部依赖）
tests/              核心纯函数测试 + token 配置测试
data/               运行时数据（抓取内容/任务/日志/简报快照，自动生成）
```

## 数据持久化（本地 JSON 文件，无数据库）

```
data/
  tasks.json          {"seq": N, "items": [任务...]}        # 任务配置 + 调度状态
  runs.json           {"seq": N, "items": [执行记录...]}    # 保留最近 200 条
  articles/{id}.json  [资讯...]                             # 每任务一份，按 url 去重，保留 2000 条
```

写入走「临时文件 + 原子 rename」，进程崩溃不会留下半截文件；HTTP 线程与调度线程
共用一把可重入锁。`--data <目录>` 可自定义存储位置。

## 注意

- 微博热搜需先种 visitor cookie，已自动处理（缓存 30 分钟）
- Reddit / DuckDuckGo 类被墙源：若本机有代理（7890/7897/1080 等端口）会自动使用，没有则该源静默跳过
- 所有请求显式绕过环境变量代理，不受 shell 注入的坏代理影响
- 发布到公网前务必加 HTTPS（应用本身跑 HTTP，由反向代理如 Cloudflare Tunnel / Caddy 终结 TLS）。
  纯 HTTP 下 token 会明文传输，仅能挡住"不知道 token 的访客"，挡不住流量嗅探者。

## 测试

```bash
python3 tests/test_core.py          # 核心纯函数
python3 tests/test_token_config.py  # token 配置加载
```

## 许可

[MIT](LICENSE) © 2026 BlossomLoop

`data/` 目录（含 API Key 与抓取内容）已在 `.gitignore` 中排除，不会进入仓库。
