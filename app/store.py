"""本地文件存储：JSON 文件持久化，无数据库依赖。

目录布局（均为人类可读 JSON，可直接查看/手改）：
  data/tasks.json          {"seq": N, "items": [task, ...]}
  data/runs.json           {"seq": N, "items": [run, ...]}      仅保留最近 MAX_RUNS 条
  data/articles/{id}.json  [article, ...]                       每任务一份，按 url_hash 去重

并发：HTTP 线程与调度线程共用一把可重入锁；写入走「临时文件 + 原子 rename」，
进程崩溃也不会留下半截文件。
"""
import json
import math
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

MAX_RUNS = 200          # 执行日志保留上限
MAX_ARTICLES = 2000     # 单任务文章保留上限（按热度裁剪）
RETENTION_DAYS = 14     # 非👍内容保留期：超此天数且未被点赞(like)的文章会被清除

_lock = threading.RLock()
_DATA: Path | None = None
_CONFIG: Path | None = None   # 配置文件（仓库根 config.json）：admin_token + server + llm


def now() -> str:
    """统一时间戳：UTC，空格分隔。前端 fmtTime 据此换算本地时区显示。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def init(data_dir: str, config_path: str | None = None) -> None:
    """data_dir 存运行时数据；config_path 指向独立的配置文件（与运行时数据分离）。"""
    global _DATA, _CONFIG
    _DATA = Path(data_dir)
    _CONFIG = Path(config_path) if config_path else None
    (_DATA / "articles").mkdir(parents=True, exist_ok=True)
    (_DATA / "digests").mkdir(parents=True, exist_ok=True)
    (_DATA / "feedback").mkdir(parents=True, exist_ok=True)
    (_DATA / "skills" / "history").mkdir(parents=True, exist_ok=True)
    (_DATA / "trending").mkdir(parents=True, exist_ok=True)
    for name in ("tasks.json", "runs.json"):
        if not (_DATA / name).exists():
            _write(_DATA / name, {"seq": 0, "items": []})


def _read(path: Path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return default


def _write(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)  # 原子替换


# ---------------- 配置（仓库根 config.json：admin_token + server + llm）----------------

def load_config_file(config_path: str | None) -> dict:
    """直接读取指定配置文件（不依赖 init，供引导阶段解析 data_dir / host / port 用）。
    文件缺失或损坏返回空字典。"""
    if not config_path:
        return {}
    cfg = _read(Path(config_path), {})
    return cfg if isinstance(cfg, dict) else {}


def read_config() -> dict:
    """读取整份配置；未初始化或文件缺失时返回空字典。"""
    with _lock:
        return _read(_CONFIG, {}) if _CONFIG else {}


def write_config(patch: dict) -> dict:
    """顶层键合并写回（如 {"llm": ...} 只覆盖 llm 节，保留 admin_token / server）。"""
    if not _CONFIG:
        return {}
    with _lock:
        cfg = _read(_CONFIG, {})
        if not isinstance(cfg, dict):
            cfg = {}
        cfg.update(patch)
        _write(_CONFIG, cfg)
        return cfg


# ---------------- 任务 ----------------

def _tasks_path() -> Path:
    return _DATA / "tasks.json"


def list_tasks() -> list[dict]:
    with _lock:
        store = _read(_tasks_path(), {"seq": 0, "items": []})
    return sorted(store["items"], key=lambda t: -t["id"])


def get_task(task_id: int) -> dict | None:
    with _lock:
        store = _read(_tasks_path(), {"seq": 0, "items": []})
    return next((t for t in store["items"] if t["id"] == task_id), None)


def create_task(data: dict) -> dict:
    with _lock:
        store = _read(_tasks_path(), {"seq": 0, "items": []})
        store["seq"] += 1
        task = {
            "id": store["seq"],
            "name": data["name"],
            "keywords": data.get("keywords", []),
            "sources": data.get("sources", []),
            "schedule_type": data.get("schedule_type", "interval"),
            "schedule_value": data["schedule_value"],
            "enabled": bool(data.get("enabled", True)),
            "preferred_keywords": [],
            "excluded_keywords": [],
            "last_run": None,
            "next_run": None,
            "created_at": now(),
        }
        store["items"].append(task)
        _write(_tasks_path(), store)
    return task


def update_task(task_id: int, data: dict) -> dict | None:
    """更新用户可编辑字段（保留 last_run / next_run / created_at）。"""
    with _lock:
        store = _read(_tasks_path(), {"seq": 0, "items": []})
        for t in store["items"]:
            if t["id"] == task_id:
                t.update({
                    "name": data["name"],
                    "keywords": data.get("keywords", []),
                    "sources": data.get("sources", []),
                    "schedule_type": data.get("schedule_type", "interval"),
                    "schedule_value": data["schedule_value"],
                    "enabled": bool(data.get("enabled", True)),
                })
                _write(_tasks_path(), store)
                return t
    return None


def patch_task(task_id: int, **fields) -> dict | None:
    """局部更新运行时字段，如 last_run / next_run。"""
    with _lock:
        store = _read(_tasks_path(), {"seq": 0, "items": []})
        for t in store["items"]:
            if t["id"] == task_id:
                t.update(fields)
                _write(_tasks_path(), store)
                return t
    return None


def delete_task(task_id: int) -> bool:
    with _lock:
        store = _read(_tasks_path(), {"seq": 0, "items": []})
        before = len(store["items"])
        store["items"] = [t for t in store["items"] if t["id"] != task_id]
        removed = len(store["items"]) < before
        if removed:
            _write(_tasks_path(), store)
        for sub in ("articles", "digests", "feedback"):
            p = _DATA / sub / f"{task_id}.json"
            if p.exists():
                p.unlink()
    return removed


# ---------------- 文章 ----------------

def _articles_path(task_id: int) -> Path:
    return _DATA / "articles" / f"{task_id}.json"


def add_articles(task_id: int, items: list[dict]) -> int:
    """按 url_hash + 标题幂等追加，返回新增条数；超量时按热度裁剪。

    热榜源的 URL 常带动态参数（同一条热搜每次抓取 URL 不同），单靠 url_hash
    无法去重，故同时按标题去重——同标题视为同一条，跨次/跨源都不重复入库。

    入库后做保留期裁剪：超过 RETENTION_DAYS 天且未被用户点赞(like)的内容清除，
    点赞内容长期保留。fetched_at 为零填充 UTC 字符串，可直接按字典序比较时间。
    """
    with _lock:
        pool = _read(_articles_path(task_id), [])
        seen_url = {a["url_hash"] for a in pool}
        seen_title = {a["title"].strip() for a in pool}
        added = 0
        for it in items:
            title = it["title"].strip()
            if it["url_hash"] in seen_url or title in seen_title:
                continue
            seen_url.add(it["url_hash"])
            seen_title.add(title)
            pool.append({**it, "task_id": task_id, "fetched_at": now()})
            added += 1

        # 保留期裁剪：点赞内容豁免，其余超期清除
        fb = _read(_feedback_path(task_id), {})
        liked = {h for h, v in fb.items() if v.get("signal") == "like"}
        cutoff = (datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
                  ).strftime("%Y-%m-%d %H:%M:%S")
        before = len(pool)
        pool = [a for a in pool
                if a.get("url_hash") in liked or (a.get("fetched_at") or "") >= cutoff]
        pruned = before - len(pool)

        if added or pruned:
            pool.sort(key=lambda a: (-a.get("score", 0), a.get("fetched_at", "")))
            del pool[MAX_ARTICLES:]
            _write(_articles_path(task_id), pool)
    return added


def count_articles(task_id: int) -> int:
    with _lock:
        return len(_read(_articles_path(task_id), []))


def _age_hours(ts: str) -> float:
    """距今小时数；无法解析（如无时间戳）按很久远处理。兼容 ISO 与 '空格' 两种格式。"""
    if not ts:
        return 1e6
    try:
        s = ts.replace("Z", "+00:00")
        dt = (datetime.fromisoformat(s) if ("T" in s or "+" in s)
              else datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 3600)
    except (ValueError, TypeError):
        return 1e6


def _trend_score(a: dict) -> float:
    """趋势分（新鲜热度）= (log 热度 + 1) / (距今小时数 + 3)。
    越新越热越靠前；+1 让零热度的新条目仍按新鲜度上榜，与绝对热度榜区分。"""
    eng = math.log1p(max(0, a.get("engagement", 0)))
    age = _age_hours(a.get("published_at") or a.get("fetched_at") or "")
    return (eng + 1.0) / (age + 3.0)


def list_articles(task_id=None, source=None, q=None, sort="score",
                  limit=50, offset=0) -> list[dict]:
    with _lock:
        if task_id:
            pool = _read(_articles_path(int(task_id)), [])
        else:
            pool = []
            for f in (_DATA / "articles").glob("*.json"):
                pool.extend(_read(f, []))
    if source:
        pool = [a for a in pool if a["source"] == source]
    if q:
        ql = q.lower()
        pool = [a for a in pool
                if ql in a["title"].lower() or ql in (a.get("summary") or "").lower()]
    if sort == "time":
        pool.sort(key=lambda a: (a.get("published_at") or a.get("fetched_at") or "",
                                 a.get("score", 0)), reverse=True)
    elif sort == "trend":
        pool.sort(key=_trend_score, reverse=True)
    else:
        # 热点：源内分桶 + 跨源轮转混合，防止单一源凭绝对热度量纲（github 的 star）
        # 或大量同分（bing 几百条并列 0.55）扎堆霸屏。各源桶内按绝对热度降序，桶间按
        # 各自 top 分排序决定同轮先后，再逐轮跨桶取，使各源头部交错上榜。
        buckets: dict = {}
        for a in pool:
            buckets.setdefault(a["source"], []).append(a)
        order = sorted(buckets.values(),
                       key=lambda items: max(i.get("score", 0) or 0 for i in items),
                       reverse=True)
        for items in order:
            items.sort(key=lambda a: a.get("score", 0) or 0, reverse=True)
        merged, depth = [], 0
        while any(depth < len(items) for items in order):
            for items in order:
                if depth < len(items):
                    merged.append(items[depth])
            depth += 1
        pool = merged
    return pool[offset:offset + limit]


# ---------------- 简报快照 ----------------

def _digest_path(task_id: int) -> Path:
    return _DATA / "digests" / f"{task_id}.json"


def read_digest(task_id: int) -> dict | None:
    with _lock:
        return _read(_digest_path(task_id), None)


def write_digest(task_id: int, data: dict) -> None:
    with _lock:
        _write(_digest_path(task_id), data)


# ---------------- 热门 Skill 榜快照 ----------------
# data/skills/{type}_{period}.json  当前榜单快照（公开 GET 直读，不打外网）
# data/skills/history/{date}.json   每日 entries 精简快照（飙升榜做差用）

def _skills_board_path(board_type: str, period: str) -> Path:
    return _DATA / "skills" / f"{board_type}_{period}.json"


def read_skills_board(board_type: str, period: str) -> dict | None:
    with _lock:
        return _read(_skills_board_path(board_type, period), None)


def save_skills_board(board_type: str, period: str, data: dict) -> None:
    with _lock:
        _write(_skills_board_path(board_type, period), data)


def _trending_path(source: str, period: str, language: str) -> Path:
    # 文件名安全化：语言可能含 + / 空格（如 C++、Jupyter Notebook）
    safe_lang = re.sub(r"[^\w.-]", "_", language or "All")
    return _DATA / "trending" / f"{source}_{period}_{safe_lang}.json"


def read_trending(source: str, period: str, language: str) -> dict | None:
    """读趋势榜磁盘快照；无则 None。趋势数据落盘后重启不丢、页面只读盘不穿透外部源。"""
    with _lock:
        return _read(_trending_path(source, period, language), None)


def save_trending(source: str, period: str, language: str, data: dict) -> None:
    with _lock:
        _write(_trending_path(source, period, language), data)


def archive_skills_history(date: str, data) -> None:
    """归档某日 entries 精简快照（覆盖写当日）。"""
    with _lock:
        _write(_DATA / "skills" / "history" / f"{date}.json", data)


def read_skills_history_before(date: str):
    """取日期 ≤ date 的最近一份 history 快照（按文件名字典序）；无则返回 None。
    飙升榜据此与当前快照做差。"""
    with _lock:
        hist_dir = _DATA / "skills" / "history"
        if not hist_dir.exists():
            return None
        candidates = sorted(
            p for p in hist_dir.glob("*.json") if p.stem <= date)
        if not candidates:
            return None
        return _read(candidates[-1], None)


# ---------------- 执行日志 ----------------

def _runs_path() -> Path:
    return _DATA / "runs.json"


def start_run(task_id: int) -> int:
    with _lock:
        store = _read(_runs_path(), {"seq": 0, "items": []})
        store["seq"] += 1
        store["items"].append({
            "id": store["seq"], "task_id": task_id,
            "started_at": now(), "finished_at": None,
            "status": "running", "stats": {},
        })
        if len(store["items"]) > MAX_RUNS:
            store["items"] = store["items"][-MAX_RUNS:]
        _write(_runs_path(), store)
        return store["seq"]


def finish_run(run_id: int, status: str, stats: dict) -> None:
    with _lock:
        store = _read(_runs_path(), {"seq": 0, "items": []})
        for r in store["items"]:
            if r["id"] == run_id:
                r.update(finished_at=now(), status=status, stats=stats)
                break
        _write(_runs_path(), store)


def list_runs(task_id=None, limit=20) -> list[dict]:
    with _lock:
        store = _read(_runs_path(), {"seq": 0, "items": []})
    items = store["items"]
    if task_id:
        items = [r for r in items if r["task_id"] == int(task_id)]
    return sorted(items, key=lambda r: -r["id"])[:limit]


# ---------------- 用户反馈 ----------------

def _feedback_path(task_id: int) -> Path:
    return _DATA / "feedback" / f"{task_id}.json"


def get_feedback(task_id: int) -> dict:
    with _lock:
        return _read(_feedback_path(task_id), {})


def set_feedback(task_id: int, url_hash: str, url: str, title: str, signal: str) -> None:
    """signal ∈ {like, dislike, none}；none 表示取消（删除该条）。"""
    with _lock:
        fb = _read(_feedback_path(task_id), {})
        if signal == "none":
            fb.pop(url_hash, None)
        else:
            fb[url_hash] = {"signal": signal, "url": url, "title": title, "at": now()}
        _write(_feedback_path(task_id), fb)
