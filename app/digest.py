"""简报：把零散资讯归纳成「综述 + 主题聚类 + 热点榜 + 关键词」。

简报是**快照式**的：由定时任务在每轮抓取后生成一次并存盘（store.write_digest），
页面只读取快照、不实时重算。综述按「模型设置」的配置生成（云端 / 本地 Ollama / 算法）。

注：自带 _bigrams（不复用 pipeline 的）以避免 pipeline ↔ digest 循环依赖。
"""
import re
from collections import Counter

from . import llm, store
from .sources import SOURCES

CLUSTER_THRESHOLD = 0.34   # 标题 bigram Jaccard ≥ 此值归为同一主题
SUMMARY_POOL = 250         # 参与聚类/综述的资讯上限（按热度取，控制 LLM 输入与耗时）

# 关键词提取停用词（中英），避免无意义高频片段
_STOP_EN = {"the", "a", "an", "to", "for", "of", "on", "in", "and", "with", "from",
            "by", "at", "is", "are", "this", "that", "how", "what", "new", "you",
            "your", "it", "we", "as", "be", "or", "vs"}
_STOP_ZH = {"回应", "发布", "表示", "如何", "怎么", "为何", "可以", "这个", "什么",
            "我们", "你们", "他们", "今天", "目前", "已经", "曾经", "记者"}


def _bigrams(text: str) -> set:
    t = re.sub(r"\s+", "", text.lower())
    return {t[i:i + 2] for i in range(len(t) - 1)} if len(t) > 1 else {t}


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _cluster(articles: list[dict]) -> list[dict]:
    """贪心聚类：与各簇代表（最高分首条）比相似度，超阈值则归入。
    入参需已按 score 降序，使每簇代表为簇内最热条目。"""
    clusters: list[dict] = []
    for a in articles:
        grams = _bigrams(a["title"])
        best, best_sim = None, 0.0
        for c in clusters:
            sim = _jaccard(grams, c["grams"])
            if sim > best_sim:
                best, best_sim = c, sim
        if best and best_sim >= CLUSTER_THRESHOLD:
            best["items"].append(a)
        else:
            clusters.append({"items": [a], "grams": grams})
    return clusters


def _topics(clusters: list[dict], min_size: int = 2, limit: int = 8) -> list[dict]:
    multi = [c for c in clusters if len(c["items"]) >= min_size]
    multi.sort(key=lambda c: (-len(c["items"]),
                              -sum(i.get("engagement", 0) for i in c["items"])))
    topics = []
    for c in multi[:limit]:
        items = c["items"]
        rep = items[0]
        topics.append({
            "title": rep["title"],
            "url": rep["url"],
            "count": len(items),
            "sources": sorted({SOURCES.get(i["source"], {}).get("name", i["source"]) for i in items}),
            "heat": sum(i.get("engagement", 0) for i in items),
            "items": [{"title": i["title"], "url": i["url"],
                       "source": SOURCES.get(i["source"], {}).get("name", i["source"])}
                      for i in items[:6]],
        })
    return topics


def _keywords(articles: list[dict], limit: int = 18) -> list[list]:
    en = Counter()
    grams = Counter()  # 中文 2~4-gram 频率，用于轻量新词发现
    for a in articles:
        title = a["title"]
        for w in re.findall(r"[a-zA-Z][a-zA-Z0-9+.#-]{1,}", title.lower()):
            if w not in _STOP_EN and len(w) > 1:
                en[w] += 1
        for run in re.findall(r"[一-鿿]{2,}", title):
            for n in (2, 3, 4):
                for i in range(len(run) - n + 1):
                    grams[run[i:i + n]] += 1

    # 去子串：长词优先；当更长词频率不低于短词的 60% 时，丢弃短碎片
    cand = {w: c for w, c in grams.items() if c >= 3 and w not in _STOP_ZH}
    zh_kept: dict[str, int] = {}
    for w in sorted(cand, key=len, reverse=True):
        if any(w in longer and cand[longer] >= cand[w] * 0.6 for longer in zh_kept):
            continue
        zh_kept[w] = cand[w]

    merged = [(w, n) for w, n in en.items() if n >= 2]
    merged += list(zh_kept.items())
    merged.sort(key=lambda x: -x[1])
    return [[w, n] for w, n in merged[:limit]]


def _llm_summary(task_name: str, titles: list[str]) -> tuple[str | None, str]:
    """委托 llm 层按用户配置生成综述（云端 / 本地 Ollama / 无）。"""
    system = "你是中文资讯编辑，擅长把零散资讯归纳成简洁、客观的综述。"
    user = (
        f"下面是「{task_name}」最近采集的资讯标题。"
        f"请用中文写一段不超过 200 字的热点综述，按话题归类点出主要动态和趋势，"
        f"客观陈述、不要分点、不要标题、不要寒暄：\n\n"
        + "\n".join(f"- {t}" for t in titles)
    )
    return llm.summarize(system, user)


def _extractive_summary(task_name: str, total: int, source_names: list[str],
                        topics: list[dict], hot: list[dict]) -> str:
    if not total:
        return f"「{task_name}」暂无资讯。"
    head = f"「{task_name}」共收录 {total} 条资讯，来自 {len(source_names)} 个来源。"
    if topics:
        body = f"最集中的话题是「{topics[0]['title']}」，有 {topics[0]['count']} 条相关报道。"
        if len(topics) > 1:
            others = "、".join(f"「{t['title']}」" for t in topics[1:3])
            body += f"其它受关注的还有 {others}。"
    elif hot:
        body = f"资讯较为分散，热度最高的是「{hot[0]['title']}」。"
    else:
        body = ""
    return head + body


def generate(task_id: int) -> dict | None:
    """对任务当前库（按热度取前 SUMMARY_POOL 条）生成一份简报快照。
    会实际调用 LLM，耗时较长——只应在定时任务后或用户手动刷新时调用。"""
    task = store.get_task(task_id)
    if not task:
        return None

    articles = store.list_articles(task_id=task_id, limit=10000)
    articles.sort(key=lambda a: -a.get("score", 0))
    pool = articles[:SUMMARY_POOL]

    source_names = sorted({SOURCES.get(a["source"], {}).get("name", a["source"]) for a in pool})
    topics = _topics(_cluster(pool))
    hot = [{"title": a["title"], "url": a["url"],
            "source": SOURCES.get(a["source"], {}).get("name", a["source"]),
            "engagement": a.get("engagement", 0), "score": a.get("score", 0)}
           for a in pool[:12]]
    keywords = _keywords(pool)

    summary, generated_by = None, "extractive"
    if pool:
        summary, generated_by = _llm_summary(task["name"], [a["title"] for a in pool])
    if not summary:
        generated_by = "extractive"
        summary = _extractive_summary(task["name"], len(pool), source_names, topics, hot)

    return {
        "task_id": task_id,
        "task_name": task["name"],
        "total": len(articles),
        "summarized": len(pool),
        "source_count": len(source_names),
        "generated_by": generated_by,
        "generated_at": store.now(),
        "summary": summary,
        "topics": topics,
        "hot": hot,
        "keywords": keywords,
    }


def generate_and_save(task_id: int) -> dict | None:
    """生成简报并写入快照文件。返回快照。"""
    snap = generate(task_id)
    if snap:
        store.write_digest(task_id, snap)
    return snap
