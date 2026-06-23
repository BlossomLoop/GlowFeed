"""热门 Skill 榜采集层：全部免 API key、纯标准库、失败容忍。

三类源（详见 docs/specs/2026-06-16-hot-skills-board-design.md，以「M0 实测结论」为准）：
- GitHub Search：`topic:claude-code-skills` 按 stars 召回，**召回后过滤**剔除
  awesome/best-practice/list 等非真 skill 仓库（未认证 search 限流 10 次/分钟，严控请求数）。
- GitHub repo tree：一次 recursive 拿全树，筛 `SKILL.md` 结尾的 path → 子 skill 名（取父目录名）；
  含 ≥5 个判 `is_collection`（集合型仓库，如 anthropics/skills 实测 18 个）。
- 博客口碑：复用 sources.fetch_bing/fetch_hackernews 多 query 搜文章 → LLM 抽取候选（可注入便于测试）。

解析逻辑与网络分离：`_parse_*` 纯函数可喂录制夹具离线测；fetcher 失败一律返 []。
"""
import html as _htmllib
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

# query 须避开以 best/top 等英文词典词开头——Bing CN SERP 会将其劫持为单词释义，
# 整页自然结果退化成词典条目（实测）。改用短语锚定 / 中文意图词召回真实推荐文章。
_BLOG_QUERIES = [
    "claude code skill 推荐",
    "claude code skills 精选",
    "codex skill 推荐",
    "claude code 好用的 skill",
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
    articles, seen = [], set()
    for kw in _BLOG_QUERIES:
        for fetch in (sources.fetch_bing, sources.fetch_hackernews):
            try:
                hits = fetch(kw) or []
            except Exception:
                continue
            for a in hits:  # 多 query 常召回同一文章，按 url 去重免重复抓正文/抽取
                url = (a.get("url") or "").strip()
                if url and url not in seen:
                    seen.add(url)
                    articles.append(a)
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


# 逐篇抓正文 + 抽取的文章数上限：抓正文 + LLM 各耗时，控刷新整体时延。
_MAX_ARTICLES = 12
# 正文短于此视为抓取失败 / JS 壳 / 反爬空页，退回用搜索摘要。
_BODY_MIN_CHARS = 800
# 抽取输出上限：清单体文章一篇能荐 10+ 个 skill，需放大避免 JSON 被截断。
_EXTRACT_MAX_TOKENS = 1500
# 瞬时失败重试次数（正文抓取 / LLM 抽取共用）：把逐次产量拉稳到接近满载。
_RETRY = 2
# 已知硬反爬域名：基于反爬 cookie/token 拦截（zhihu 恒 403，换请求头无效），
# 抓正文必失败，直接跳过、退回搜索摘要，省一次注定 403 的请求与文章配额。
_BODY_BLOCKED_HOSTS = ("zhihu.com",)


def _html_to_text(raw: str) -> str:
    """粗剥 HTML 为纯文本：去 script/style，去标签，反转义，折叠空白。纯函数。"""
    t = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", raw or "")
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", _htmllib.unescape(t)).strip()


def _fetch_article_text(url: str, max_chars: int = 5000) -> str:
    """抓文章正文文本（清单体推荐文里完整的 skill 列表只在正文，不在搜索摘要）。

    失败 / 正文过短（反爬空页或 JS 壳）返 ''，由调用方退回用摘要。
    已知硬反爬域名直接跳过；其余瞬时失败重试 _RETRY 次。"""
    host = _domain_of(url)
    if any(host == b or host.endswith("." + b) for b in _BODY_BLOCKED_HOSTS):
        return ""
    for _ in range(_RETRY):
        raw = http_util.get(url, timeout=8, via_proxy=True)
        if raw:
            text = _html_to_text(raw)
            if len(text) >= _BODY_MIN_CHARS:
                return text[:max_chars]
    return ""


def _llm_extract_candidates(articles: list[dict]) -> list[dict]:
    """默认抽取器：逐篇抓正文 → LLM 抽被推荐的 skill 名。

    逐篇而非批量：正文体量大，且 domain/author 由文章自身确定（代码归属，不靠 LLM 回显），
    跨源去重才准。单篇失败只跳过该篇，不拖累其余。LLM 全失败返空 → 调用方降级。"""
    from . import llm

    out = []
    for a in articles[:_MAX_ARTICLES]:
        out.extend(_extract_one_article(llm, a))
    return out


def _extract_one_article(llm, a: dict) -> list[dict]:
    """抓正文（失败退回摘要）→ LLM 抽 skill 名 → 用文章自身 domain/author 归属。"""
    import json

    body = _fetch_article_text(a.get("url", "")) or (a.get("summary") or "")
    if not body.strip():
        return []
    system = "你是技术情报抽取助手，只输出 JSON 数组，不要解释。"
    user = (
        "下文是一篇推荐 Claude Code / Codex Skill 的文章。抽取其中明确被推荐的 skill 名，"
        "输出 JSON 数组，每项 {name, source_repo(GitHub owner/repo 或空), reason(一句话)}。"
        "只要具体 skill 名，忽略泛泛的工具/平台/模型名。无明确推荐则输出 []。\n\n"
        f"标题：{a.get('title', '')}\n正文：{body}"
    )
    text = None
    for _ in range(_RETRY):  # LLM 偶发超时/空响应重试，稳住逐篇产量
        text, _label = llm.summarize(system, user, max_tokens=_EXTRACT_MAX_TOKENS)
        if text:
            break
    if not text:
        return []
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
    except ValueError:
        return []
    domain = _domain_of(a.get("url", ""))      # 来源归该文章，供口碑榜 ≥2 跨源去重
    author = (a.get("author") or "").strip()
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
            "domain": domain,
            "author": author,
            "title": (a.get("title") or "").strip(),  # 推荐来源文章，供前端可点击跳转
            "url": (a.get("url") or "").strip(),
        })
    return out
