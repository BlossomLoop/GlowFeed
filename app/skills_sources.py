"""热门 Skill 榜采集层：全部免 API key、纯标准库、失败容忍。

三类源（详见 docs/specs/2026-06-16-hot-skills-board-design.md，以「M0 实测结论」为准）：
- GitHub Search：`topic:claude-code-skills` 按 stars 召回，**召回后过滤**剔除
  awesome/best-practice/list 等非真 skill 仓库（未认证 search 限流 10 次/分钟，严控请求数）。
- GitHub repo tree：一次 recursive 拿全树，筛 `SKILL.md` 结尾的 path → 子 skill 名（取父目录名）；
  含 ≥5 个判 `is_collection`（集合型仓库，如 anthropics/skills 实测 18 个）。
- 博客口碑：复用 sources.fetch_bing/fetch_hackernews 多 query 搜文章 → LLM 抽取候选（可注入便于测试）。

解析逻辑与网络分离：`_parse_*` 纯函数可喂录制夹具离线测；fetcher 失败一律返 []。
"""
import re

from . import http_util, sources

# 召回后过滤：name / full_name / topics 命中这些词（大小写不敏感）的判为非真 skill 仓库
_NOISE_RE = re.compile(
    r"awesome|best-practice|best_practice|bestpractice|"
    r"\blist\b|collection|cheatsheet|tutorial|guide",
    re.IGNORECASE,
)

# 集合型仓库阈值：repo 内 SKILL.md 数量 ≥ 此值判 is_collection
_COLLECTION_MIN = 5


def _is_noise(full_name: str, name: str, topics: list) -> bool:
    """召回后过滤：名字/全名/任一 topic 命中噪声词即剔除。"""
    haystack = " ".join([full_name or "", name or "", *(topics or [])])
    return bool(_NOISE_RE.search(haystack))


def _parse_skill_search(data: dict) -> list[dict]:
    """解析 GitHub search 响应，做召回后过滤，按 stars 降序。纯函数（喂夹具可测）。"""
    out = []
    for r in (data or {}).get("items", []):
        full_name = r.get("full_name") or ""
        name = r.get("name") or ""
        topics = r.get("topics") or []
        if not full_name or _is_noise(full_name, name, topics):
            continue
        out.append({
            "id": full_name,
            "name": name,
            "url": r.get("html_url") or f"https://github.com/{full_name}",
            "description": (r.get("description") or "").strip(),
            "github_stars": int(r.get("stargazers_count") or 0),
            "pushed_at": r.get("pushed_at"),
            "topics": list(topics),
        })
    out.sort(key=lambda x: -x["github_stars"])
    return out


def github_skill_search(period: str = "recent") -> list[dict]:
    """检索热门 Skill 仓库（topic:claude-code-skills，按 stars）。

    period 目前仅一档「recent」（GitHub search 本就拿近期高星库，见 M0 调整）；
    保留形参以兼容调用方/未来扩展。失败/限流返 []。"""
    q = http_util.quote("topic:claude-code-skills")
    url = (f"https://api.github.com/search/repositories?q={q}"
           f"&sort=stars&order=desc&per_page=30")
    data = http_util.get_json(url, headers={"Accept": "application/vnd.github+json"})
    if not data:
        return []
    return _parse_skill_search(data)


def _parse_repo_tree(repo: str, data: dict) -> dict:
    """从 git tree 响应筛出子 skill 名（SKILL.md 父目录名），判 is_collection。纯函数。"""
    children = []
    for node in (data or {}).get("tree", []):
        path = node.get("path") or ""
        if not path.endswith("SKILL.md"):
            continue
        parts = path.split("/")
        # path 形如 skills/<name>/SKILL.md → 取 SKILL.md 的父目录名；根级 SKILL.md 用 repo 名兜底
        child = parts[-2] if len(parts) >= 2 else (repo.split("/")[-1])
        if child and child not in children:
            children.append(child)
    return {
        "repo": repo,
        "is_collection": len(children) >= _COLLECTION_MIN,
        "children": children,
    }


def repo_tree_skills(repo: str, branch: str = "main") -> dict:
    """拉 repo 的 recursive git tree，展开其中的子 skill（SKILL.md）。

    免 key 可用（core 限流 60/h）。失败返回空结构（children 空、非集合）。
    main 拉空时回退 master 再试一次。"""
    empty = {"repo": repo, "is_collection": False, "children": []}
    for br in (branch, "master") if branch == "main" else (branch,):
        url = (f"https://api.github.com/repos/{repo}/git/trees/"
               f"{http_util.quote(br)}?recursive=1")
        data = http_util.get_json(url, headers={"Accept": "application/vnd.github+json"})
        if data and data.get("tree"):
            parsed = _parse_repo_tree(repo, data)
            if parsed["children"]:
                return parsed
    return empty


# ---------------- 博客口碑候选 ----------------

# 多语种 query：中英各几条，覆盖 claude code / codex skill 推荐类文章
_BLOG_QUERIES = [
    "best claude code skills",
    "top codex skills",
    "claude skill 推荐",
    "claude code skill 精选",
]

# 标题里 owner/repo 形态的 GitHub 引用，用于无 LLM 时的兜底提名
_REPO_RE = re.compile(r"github\.com/([\w.-]+/[\w.-]+)", re.IGNORECASE)
_NAME_HINT_RE = re.compile(r"`([\w.-]{2,40})`")  # 反引号包裹的疑似 skill 名


def _strip_www(host: str) -> str:
    host = (host or "").lower()
    return host[4:] if host.startswith("www.") else host


def _domain_of(url: str) -> str:
    """从 url 取主域名（去掉 scheme / path / 前导 www.），失败返空。"""
    m = re.match(r"https?://([^/]+)", url or "")
    return _strip_www(m.group(1) if m else "")


def _fallback_extract(articles: list[dict]) -> list[dict]:
    """LLM 不可用时的降级：用正则从标题/摘要提名（GitHub 链接优先，其次反引号名）。"""
    out = []
    for a in articles:
        text = f"{a.get('title', '')} {a.get('summary', '')}"
        repo = None
        m = _REPO_RE.search(a.get("url", "") + " " + text)
        if m:
            repo = m.group(1)
            name = repo.split("/")[-1]
        else:
            hint = _NAME_HINT_RE.search(text)
            if not hint:
                continue
            name = hint.group(1)
        out.append({
            "name": name,
            "source_repo": repo,
            "reason": (a.get("title") or "").strip()[:120],
            "domain": _domain_of(a.get("url", "")),
            "author": (a.get("author") or "").strip(),
            "title": (a.get("title") or "").strip(),
            "url": a.get("url") or "",
        })
    return out


def blog_mention_search(llm_extract=None) -> list[dict]:
    """搜博客/HN 文章 → 抽取「被推荐的 skill」候选（口碑榜原料）。

    llm_extract: 可注入的抽取函数 articles(list[dict]) -> candidates(list[dict])，
    便于离线测试。为 None 时走 app.llm（候选抽取）；LLM 不可用则降级到正则提名。
    每个候选形如 {name, source_repo?, reason, domain, author, title?, url?}。

    任何源/抽取失败：跳过该步，最终返 []（绝不抛出）。"""
    articles = []
    for kw in _BLOG_QUERIES:
        for fetch in (sources.fetch_bing, sources.fetch_hackernews):
            try:
                articles.extend(fetch(kw) or [])
            except Exception:
                continue
    if not articles:
        return []

    if llm_extract is None:
        llm_extract = _llm_extract_candidates
    try:
        cands = llm_extract(articles)
    except Exception:
        cands = None
    if cands:
        return cands
    # LLM 不可用 / 抽取为空 → 正则降级
    return _fallback_extract(articles)


def _llm_extract_candidates(articles: list[dict]) -> list[dict]:
    """默认抽取器：让 LLM 从文章里识别被推荐的 skill 候选。

    LLM 未配置/失败返回空 → 调用方降级。每篇文章带上来源 domain/author 供去重。"""
    import json

    from . import llm

    payload = [{
        "title": a.get("title") or "",
        "summary": (a.get("summary") or "")[:200],
        "url": a.get("url") or "",
        "domain": _domain_of(a.get("url", "")),
        "author": a.get("author") or "",
    } for a in articles[:40]]
    system = "你是技术情报抽取助手，只输出 JSON 数组，不要解释。"
    user = (
        "下面是若干推荐 Claude Code / Codex Skill 的文章。请抽取每篇明确推荐的 skill，"
        "输出 JSON 数组，每项 {name, source_repo(GitHub owner/repo 或空), reason(一句话), "
        "domain, author}。domain/author 直接用该文章给的值。无法确定推荐对象的文章跳过。\n\n"
        + json.dumps(payload, ensure_ascii=False)
    )
    text, _label = llm.summarize(system, user)
    if not text:
        return []
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
    except ValueError:
        return []
    out = []
    for it in arr:
        if not isinstance(it, dict):
            continue
        name = (it.get("name") or "").strip()
        if not name:
            continue
        out.append({
            "name": name,
            "source_repo": (it.get("source_repo") or "").strip() or None,
            "reason": (it.get("reason") or "").strip(),
            "domain": _strip_www((it.get("domain") or "").strip()),
            "author": (it.get("author") or "").strip(),
        })
    return out
