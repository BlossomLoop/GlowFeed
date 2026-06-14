"""检索管线：fetch → 关键词过滤 → 去重 → 评分 → 入库。

去重/评分策略参考 last30days skill 的 dedupe.py / signals.py：
- 标题相似度用字符 bigram Jaccard（中英文通用，中文无空格分词）
- 评分 = 新鲜度 + log 归一化热度 + 来源质量先验
"""
import hashlib
import math
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from . import digest, llm, store
from .sources import SOURCES

SIM_THRESHOLD = 0.72  # bigram Jaccard 高于此值视为重复


# ---------------- 过滤 ----------------

def keyword_match(text: str, keywords: list[str]) -> bool:
    """任一关键词命中即保留；关键词为空表示不过滤。"""
    if not keywords:
        return True
    low = text.lower()
    return any(k.lower() in low for k in keywords if k.strip())


def excluded_match(title: str, summary: str, excluded: list[str]) -> bool:
    """标题或摘要命中任一排除词即返回 True（应被丢弃）。"""
    if not excluded:
        return False
    text = f"{title} {summary}".lower()
    return any(e.lower() in text for e in excluded if e.strip())


# ---------------- 去重 ----------------

_TRACKING_PARAMS = re.compile(r"(utm_[a-z]+|spm|from|ref|share_token|src)=[^&]*&?")


def normalize_url(url: str) -> str:
    url = re.sub(r"^https?://", "", url.lower())
    url = re.sub(r"^www\.", "", url)
    url, _, query = url.partition("?")
    query = _TRACKING_PARAMS.sub("", query).strip("&")
    return (url.rstrip("/") + ("?" + query if query else ""))


def url_hash(url: str) -> str:
    return hashlib.sha1(normalize_url(url).encode()).hexdigest()[:16]


def _bigrams(text: str) -> set:
    t = re.sub(r"\s+", "", text.lower())
    return {t[i:i + 2] for i in range(len(t) - 1)} if len(t) > 1 else {t}


def title_similarity(a: str, b: str) -> float:
    ga, gb = _bigrams(a), _bigrams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def dedupe(items: list[dict]) -> list[dict]:
    """先按归一化 URL 去重，再按标题 bigram 相似度去重（保留热度高者优先，
    入参需已按 score 降序）。"""
    kept, seen_urls, kept_grams = [], set(), []
    for it in items:
        h = url_hash(it["url"]) if it["url"] else None
        if h and h in seen_urls:
            continue
        grams = _bigrams(it["title"])
        if any(len(grams & g) / len(grams | g) >= SIM_THRESHOLD for g in kept_grams if grams | g):
            continue
        if h:
            seen_urls.add(h)
        kept_grams.append(grams)
        kept.append(it)
    return kept


# ---------------- 评分 ----------------

def _recency_score(published_at: str | None) -> float:
    if not published_at:
        return 0.5  # 无时间戳（热榜类）默认当下，给中高分
    try:
        dt = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age_h = max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 3600)
        return max(0.0, 1.0 - age_h / 168)  # 7 天线性衰减
    except ValueError:
        return 0.5


def score_item(it: dict, preferred: list[str] | None = None) -> float:
    quality = SOURCES.get(it["source"], {}).get("quality", 0.6)
    engagement = min(1.0, math.log1p(max(0, it["engagement"])) / 14)  # ~1.2M → 1.0
    recency = _recency_score(it["published_at"])
    base = 0.45 * recency + 0.30 * engagement + 0.25 * quality
    if preferred:
        title = it["title"].lower()
        if any(p.lower() in title for p in preferred if p.strip()):
            base += 0.15  # 命中偏好词加分，让合口味的浮上来
    return round(min(1.0, base), 4)


# ---------------- 执行 ----------------

_running_tasks: set[int] = set()
_running_lock = threading.Lock()


def run_task(task_id: int) -> dict | None:
    """执行一个任务的完整检索管线。返回 stats；任务已在跑则返回 None。"""
    with _running_lock:
        if task_id in _running_tasks:
            return None
        _running_tasks.add(task_id)
    try:
        return _run_task_inner(task_id)
    finally:
        with _running_lock:
            _running_tasks.discard(task_id)


def _run_task_inner(task_id: int) -> dict:
    task = store.get_task(task_id)
    if not task:
        return {"error": "task not found"}
    keywords = [k for k in task["keywords"] if k.strip()]
    preferred = [k for k in task.get("preferred_keywords", []) if k.strip()]
    excluded = [k for k in task.get("excluded_keywords", []) if k.strip()]
    source_ids = task["sources"] or list(SOURCES.keys())

    run_id = store.start_run(task_id)
    stats: dict = {"sources": {}, "fetched": 0, "kept": 0, "new": 0}

    def fetch_one(sid: str) -> tuple[str, list[dict]]:
        meta = SOURCES[sid]
        search_terms = keywords + preferred
        try:
            if meta["kind"] == "search":
                if not search_terms:
                    return sid, []
                results = []
                for kw in dict.fromkeys(search_terms):
                    results.extend(meta["fn"](kw) or [])
            else:
                items = meta["fn"]() or []
                # keywords 为空表示"全量不过滤"；此时偏好词不参与 feed 过滤（仅用于排序加分），
                # 避免用户点赞反而收窄召回。仅当用户设了手动关键词时才把偏好词并入过滤集。
                match_terms = keywords + preferred if keywords else []
                results = [i for i in items
                           if keyword_match(i["title"] + " " + i["summary"], match_terms)]
            # 排除词过滤（所有源）
            results = [i for i in results
                       if not excluded_match(i["title"], i.get("summary", ""), excluded)]
            return sid, results
        except Exception:
            return sid, []

    try:
        all_items: list[dict] = []
        valid_ids = [s for s in source_ids if s in SOURCES]
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(fetch_one, sid) for sid in valid_ids]
            for fut in as_completed(futures):
                sid, items = fut.result()
                stats["sources"][sid] = len(items)
                all_items.extend(items)

        stats["fetched"] = len(all_items)
        for it in all_items:
            it["score"] = score_item(it, preferred)
            it["url_hash"] = url_hash(it["url"])
        all_items.sort(key=lambda x: -x["score"])
        kept = dedupe([i for i in all_items if i["title"] and i["url"]])
        stats["kept"] = len(kept)
        stats["new"] = store.add_articles(task_id, kept)

        # 偏好学习：有新反馈则用模型提炼，增量并入偏好/排除词（手设 keywords 不动）。
        # 独立 try：学习失败只记日志，绝不影响已成功的入库与 run 状态。
        try:
            fb = store.get_feedback(task_id)
            last = task.get("last_learned_at") or ""
            if any(v.get("at", "") > last for v in fb.values()):
                likes = [v.get("title", "") for v in fb.values() if v.get("signal") == "like"]
                dislikes = [v.get("title", "") for v in fb.values() if v.get("signal") == "dislike"]
                new_pref, new_excl = llm.extract_keywords(task["name"], likes, dislikes)
                fields = {"last_learned_at": store.now()}
                if new_pref or new_excl:
                    fields["preferred_keywords"] = llm.merge_keywords(preferred, new_pref, keywords)
                    fields["excluded_keywords"] = llm.merge_keywords(excluded, new_excl, keywords)
                store.patch_task(task_id, **fields)
        except Exception as e:
            print(f"[learn] task {task_id} 学习跳过: {e}")

        store.finish_run(run_id, "ok", stats)
        store.patch_task(task_id, last_run=store.now())
        # 有新增或尚无简报时，重新生成一次简报快照（在此调 LLM，而非页面打开时）
        if stats["new"] or store.read_digest(task_id) is None:
            digest.generate_and_save(task_id)
    except Exception as e:
        stats["error"] = str(e)
        store.finish_run(run_id, "error", stats)
    return stats
