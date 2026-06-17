"""热门 Skill 榜编排层：归一去重 → 三榜计算 → 快照读写 + 做差归档。

口径（详见设计稿，统一 owner/repo 粒度）：
- 热门榜 build_hot：独立 repo 按 stars；集合型作单条目（children 列名，不与单体争排名）。
- 飙升榜 build_rising：当前快照与 ≥2 天前 history 做 ΔStars；history 不足显「积累中」。
- 口碑榜 build_praise：多源独立证据（domain,author）准入主榜，证据不足进 pending。

纯计算为主（build_* 可构造内存数据测）；warm_skills 带 single-flight 构建锁，
同一 board 并发刷新合并为一次。失败容忍：拉取失败保留旧快照，不抛到主流程。
"""
import threading
from datetime import datetime, timedelta, timezone

from . import skills_sources, store

# 飙升榜需要的最小历史天数
_RISING_MIN_DAYS = 2


def normalize(raw_items: list[dict]) -> list[dict]:
    """按 id(owner/repo) 去重合并多源信号，输出统一 SkillEntry 列表。

    raw_items 每项可来自 github_skill_search（带 github_stars / pushed_at / topics）
    或 repo tree 展开（带 children / is_collection）或博客口碑（带 blog_mention）。
    合并规则：同 id 取并集；stars/pushed_at 取非空较新值；blog_mentions 累加去重；
    type 据 is_collection 定 collection / standalone。"""
    by_id: dict[str, dict] = {}
    for raw in raw_items:
        rid = (raw.get("id") or "").strip()
        if not rid:
            continue
        e = by_id.get(rid)
        if e is None:
            e = {
                "id": rid,
                "name": raw.get("name") or rid.split("/")[-1],
                "url": raw.get("url") or f"https://github.com/{rid}",
                "description": "",
                "type": "standalone",
                "signals": {"github_stars": 0, "pushed_at": None, "blog_mentions": []},
                "delta": {"stars": None},
                "children": [],
                "agents": [],
            }
            by_id[rid] = e

        if raw.get("name"):
            e["name"] = raw["name"]
        if raw.get("url"):
            e["url"] = raw["url"]
        if raw.get("description") and not e["description"]:
            e["description"] = raw["description"]
        stars = raw.get("github_stars")
        if stars is not None:
            e["signals"]["github_stars"] = max(e["signals"]["github_stars"], int(stars))
        if raw.get("pushed_at"):
            e["signals"]["pushed_at"] = raw["pushed_at"]
        if raw.get("is_collection"):
            e["type"] = "collection"
        for child in raw.get("children") or []:
            if child not in e["children"]:
                e["children"].append(child)
        if e["children"] and e["type"] != "collection" and len(e["children"]) >= 5:
            e["type"] = "collection"
        for m in raw.get("blog_mentions") or []:
            e["signals"]["blog_mentions"].append(m)
        for ag in raw.get("agents") or []:
            if ag not in e["agents"]:
                e["agents"].append(ag)
    return list(by_id.values())


def build_hot(entries: list[dict]) -> list[dict]:
    """热门榜：按 github_stars 降序排名。集合型作单条目（children 列名），
    子 skill 不各占排名行（避免集合霸榜，见设计稿 F1）。"""
    rows = sorted(entries, key=lambda e: -e["signals"]["github_stars"])
    out = []
    for i, e in enumerate(rows, 1):
        out.append({
            "rank": i,
            "id": e["id"],
            "name": e["name"],
            "url": e["url"],
            "description": e.get("description") or "",
            "type": e["type"],
            "stars": e["signals"]["github_stars"],
            "pushed_at": e["signals"]["pushed_at"],
            "children": list(e.get("children") or []),
            "is_collection": e["type"] == "collection",
        })
    return out


def build_rising(current: list[dict], history: list[dict]) -> dict:
    """飙升榜：当前快照与最近 ≥2 天前的 history 做 ΔStars 排序。

    current / history 均为 normalize 后的 entries（或带 signals.github_stars 的等价 dict）。
    history 不足（None / 条目空）→ 返回 {"status":"warming-up","rows":[]}（诚实降级，见 F2）。"""
    base = _history_stars_map(history)
    if not base:
        return {"status": "warming-up", "rows": []}

    rows = []
    for e in current:
        rid = e["id"]
        now_stars = e["signals"]["github_stars"]
        old = base.get(rid)
        if old is None:
            continue  # 上次快照里没有，无法算 Δ（新出现的不计飙升，避免凭空跳变）
        delta = now_stars - old
        if delta <= 0:
            continue
        rows.append({
            "id": rid,
            "name": e["name"],
            "url": e["url"],
            "description": e.get("description") or "",
            "type": e["type"],
            "stars": now_stars,
            "delta_stars": delta,
        })
    rows.sort(key=lambda r: -r["delta_stars"])
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return {"status": "ok", "rows": rows}


def _history_stars_map(history) -> dict:
    """从 history 快照里取 id -> github_stars 映射；空/无效返回 {}。"""
    if not history:
        return {}
    out = {}
    for e in history:
        rid = (e.get("id") or "").strip()
        if not rid:
            continue
        stars = (e.get("signals") or {}).get("github_stars")
        if stars is None:
            stars = e.get("stars")
        if stars is not None:
            out[rid] = int(stars)
    return out


def build_praise(candidates: list[dict]) -> dict:
    """口碑榜：多源独立证据准入。

    candidates 每项 {name, source_repo?, reason, domain, author, ...}。
    - 按 skill 名归并；同一 (domain, author) 对同一 skill 只计 1 次证据（防同站/同作者软文刷高）。
    - 命中 ≥2 个独立 (domain,author) 的进主榜 rows，排序按去重后 mention_count。
    - 不足 2 个的进 pending（待证实），保留发现冷门好 skill 的初衷。
    返回 {"rows": [...], "pending": [...]}。"""
    groups: dict[str, dict] = {}
    for c in candidates or []:
        name = (c.get("name") or "").strip()
        if not name:
            continue
        key = name.lower()
        g = groups.setdefault(key, {
            "name": name,
            "source_repo": c.get("source_repo"),
            "evidence": {},   # (domain, author) -> mention dict（去重，每对计 1）
        })
        if not g["source_repo"] and c.get("source_repo"):
            g["source_repo"] = c.get("source_repo")
        ev_key = ((c.get("domain") or "").strip().lower(),
                  (c.get("author") or "").strip().lower())
        if ev_key not in g["evidence"]:
            g["evidence"][ev_key] = {
                "domain": c.get("domain") or "",
                "author": c.get("author") or "",
                "title": c.get("title") or "",
                "url": c.get("url") or "",
                "reason": c.get("reason") or "",
            }

    rows, pending = [], []
    for g in groups.values():
        mentions = list(g["evidence"].values())
        item = {
            "name": g["name"],
            "source_repo": g["source_repo"],
            "mention_count": len(mentions),
            "mentions": mentions,
        }
        (rows if len(mentions) >= 2 else pending).append(item)
    rows.sort(key=lambda r: -r["mention_count"])
    pending.sort(key=lambda r: -r["mention_count"])
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return {"rows": rows, "pending": pending}


# ---------------- 编排 / 快照 / 调度 ----------------

_BOARD_TYPES = ("hot", "rising", "praise")
_DEFAULT_PERIOD = "recent"

# single-flight：同一 board(type) 并发刷新合并为一次
_inflight: dict = {}
_inflight_lock = threading.Lock()


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _collect_entries() -> list[dict]:
    """拉取 + 展开集合 + 归一，得到统一 entries。失败的源各自返 []，不影响整体。"""
    raw = skills_sources.github_skill_search()
    entries = normalize(raw)
    # 集合展开：仅对疑似集合（topic/名字含 skills 复数 或已知集合）拉 tree，省请求配额。
    # 启发式：name 以 's' 结尾且非单体型，或 topics 含 marketplace/plugin，作集合候选。
    for e in entries:
        rid = e["id"]
        name = (e.get("name") or "").lower()
        looks_collection = name.endswith("skills") or name in ("skills",)
        if not looks_collection:
            continue
        tree = skills_sources.repo_tree_skills(rid)
        if tree.get("children"):
            e["children"] = tree["children"]
            if tree.get("is_collection"):
                e["type"] = "collection"
    return entries


def warm_skills(type=None, period: str = _DEFAULT_PERIOD) -> dict:
    """刷新一个或全部 board：拉取 → build_* → 存当前快照 → 与最近 history 做差 → 归档今日 history。

    type=None 刷新全部三榜；single-flight：同 (type, period) 在途请求合并为一次。
    返回 {board_type: {rows...}} 摘要。失败容忍：构建异常时保留旧快照。"""
    if type is None:
        result = {}
        for t in _BOARD_TYPES:
            result[t] = warm_skills(t, period)
        return result

    flight_key = (type, period)
    with _inflight_lock:
        ev = _inflight.get(flight_key)
        if ev is not None:
            leader = False
        else:
            ev = threading.Event()
            _inflight[flight_key] = ev
            leader = True

    if not leader:
        ev.wait(timeout=60)  # 合并到在途任务，等其完成后读快照
        return store.read_skills_board(type, period) or {"rows": []}

    try:
        return _do_warm(type, period)
    finally:
        with _inflight_lock:
            _inflight.pop(flight_key, None)
        ev.set()


def _do_warm(type: str, period: str) -> dict:
    """真正构建并落盘（single-flight 锁内调用）。异常 → 保留旧快照。"""
    try:
        entries = _collect_entries()
    except Exception as e:
        print(f"[skills] 采集失败 {type}/{period}: {e}", flush=True)
        return store.read_skills_board(type, period) or {"rows": []}

    snapshot_time = store.now()
    if type == "hot":
        rows = build_hot(entries)
        data = {"rows": rows, "snapshot_time": snapshot_time,
                "sources": _source_status(entries)}
    elif type == "rising":
        history = _recent_history(_RISING_MIN_DAYS)
        rising = build_rising(entries, history)
        data = {"rows": rising["rows"], "status": rising["status"],
                "snapshot_time": snapshot_time, "sources": _source_status(entries)}
    elif type == "praise":
        try:
            cands = skills_sources.blog_mention_search()
        except Exception:
            cands = []
        praise = build_praise(cands)
        data = {"rows": praise["rows"], "pending": praise["pending"],
                "snapshot_time": snapshot_time,
                "sources": _source_status(entries, blog=True)}
    else:
        return {"rows": []}

    store.save_skills_board(type, period, data)
    # 归档今日 entries 快照供飙升榜做差（仅需 stars，存精简版即可）
    store.archive_skills_history(_today(), _history_payload(entries))
    print(f"[skills] 刷新 {type}/{period}: {len(data.get('rows', []))} 条", flush=True)
    return data


def _source_status(entries: list[dict], blog: bool = False) -> list[dict]:
    """暴露各源状态供前端区分新/旧/空。"""
    fetched = store.now()
    gh_status = "ok" if entries else "empty"
    out = [{"id": "github", "status": gh_status, "fetched_at": fetched}]
    if blog:
        out.append({"id": "blog", "status": "ok", "fetched_at": fetched})
    return out


def _history_payload(entries: list[dict]) -> list[dict]:
    """归档用精简快照：只留 id + stars（飙升榜做差只需要这些）。"""
    return [{"id": e["id"], "stars": e["signals"]["github_stars"]} for e in entries]


def _recent_history(min_days: int):
    """取 min_days 天前（或更早）最近一份 history 快照，供做差。不足返回 None。"""
    target = (datetime.now(timezone.utc) - timedelta(days=min_days - 1)).strftime("%Y-%m-%d")
    return store.read_skills_history_before(target)
