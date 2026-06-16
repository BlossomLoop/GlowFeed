"""HTTP 服务：静态页面 + REST API，纯标准库。

API:
  GET    /api/sources                  信息源元数据
  GET    /api/tasks                    任务列表
  POST   /api/tasks                    创建任务
  PUT    /api/tasks/{id}               更新任务
  DELETE /api/tasks/{id}               删除任务
  POST   /api/tasks/{id}/run           立即执行（异步）
  GET    /api/articles?task_id=&source=&q=&limit=&offset=
  GET    /api/runs?task_id=&limit=
"""
import hmac
import json
import re
import secrets
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import time

from . import digest, llm, pipeline, scheduler, skills_board, store
from .sources import SOURCES, github_trending_list

WEB_DIR = Path(__file__).parent.parent / "web"

MIME = {".html": "text/html", ".js": "text/javascript", ".css": "text/css",
        ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon"}

# 管理 token（由 serve() 注入；空串表示未配置，一律拒绝）
TOKEN = ""

# 热门 Skill 榜：管理员 force 刷新冷却（同 type 5 分钟内不重复真拉），以任务完成时间为准
SKILLS_REFRESH_COOLDOWN = 300
_skills_last_refresh: dict = {}
_skills_refresh_lock = threading.Lock()
_SKILLS_BOARD_TYPES = ("hot", "rising", "praise")

# 需鉴权的 GET 路由（写操作端点统一在 do_POST/PUT/DELETE 顶部校验）
PROTECTED_GET = {"/api/tasks", "/api/runs", "/api/settings", "/api/feedback"}


def check_token(auth_header: str, token: str) -> bool:
    """校验 Authorization 头里的 bearer token。token 为空（未配置）一律拒绝。"""
    if not token:
        return False
    prefix = "Bearer "
    presented = auth_header[len(prefix):] if auth_header.startswith(prefix) else ""
    return hmac.compare_digest(presented, token)


def _skills_cooldown_remaining(board_type: str | None) -> int:
    """返回该 type 距离冷却结束的剩余秒数（0 表示可刷新）。type=None 取全部 type 最大剩余。"""
    types = [board_type] if board_type else list(_SKILLS_BOARD_TYPES)
    now = time.time()
    remaining = 0
    with _skills_refresh_lock:
        for t in types:
            last = _skills_last_refresh.get(t)
            if last is not None:
                remaining = max(remaining, int(SKILLS_REFRESH_COOLDOWN - (now - last)))
    return max(0, remaining)


def _mark_skills_refresh(board_type: str | None) -> None:
    """记录刷新完成时间（冷却以完成时间为准）。type=None 标记全部三榜。"""
    types = [board_type] if board_type else list(_SKILLS_BOARD_TYPES)
    now = time.time()
    with _skills_refresh_lock:
        for t in types:
            _skills_last_refresh[t] = now


def _public_tasks(tasks: list) -> list:
    """任务列表的公开投影：只暴露 id 与 name，不泄露关键词/信息源等配置。"""
    return [{"id": t["id"], "name": t["name"]} for t in tasks]


def _validate_task(body: dict) -> str | None:
    """返回错误信息，合法返回 None。"""
    if not (body.get("name") or "").strip():
        return "任务名称不能为空"
    st = body.get("schedule_type", "interval")
    sv = body.get("schedule_value")
    if st == "interval":
        try:
            if int(sv) < 5:
                return "间隔不能小于 5 分钟"
        except (TypeError, ValueError):
            return "间隔必须是数字（分钟）"
    elif st == "daily":
        if not isinstance(sv, list) or not sv:
            return "daily 模式需要至少一个时刻"
        for t in sv:
            if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(t)):
                return f"时刻格式错误: {t}（应为 HH:MM）"
    else:
        return f"未知调度类型: {st}"
    bad = [s for s in body.get("sources", []) if s not in SOURCES]
    if bad:
        return f"未知信息源: {', '.join(bad)}"
    kws = body.get("keywords", [])
    search_only = body.get("sources") and all(
        SOURCES[s]["kind"] == "search" for s in body["sources"])
    if not any(str(k).strip() for k in kws) and search_only:
        return "纯检索型信息源必须配置关键词"
    for field in ("preferred_keywords", "excluded_keywords"):
        v = body.get(field)
        if v is not None and not (isinstance(v, list) and all(isinstance(x, str) for x in v)):
            return f"{field} 必须是字符串数组"
    return None


class Handler(BaseHTTPRequestHandler):
    server_version = "GlowFeed/1.0"

    # ---------- 基础 ----------
    def log_message(self, fmt, *args):
        pass  # 安静模式

    def _json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except ValueError:
            return {}

    def _static(self, path: str):
        if path == "/":
            path = "/index.html"
        file = (WEB_DIR / path.lstrip("/")).resolve()
        if not str(file).startswith(str(WEB_DIR.resolve())) or not file.is_file():
            self._json({"error": "not found"}, 404)
            return
        data = file.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(file.suffix, "application/octet-stream") + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authed(self) -> bool:
        return check_token(self.headers.get("Authorization", ""), TOKEN)

    # ---------- 路由 ----------
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        route = parsed.path

        if route == "/api/auth/verify":          # 探测端点，恒 200，不做 401 拦截
            self._json({"authed": self._authed()})
            return
        if route in PROTECTED_GET and not self._authed():
            self._json({"error": "未授权"}, 401)
            return

        if route == "/api/sources":
            self._json([{"id": k, "name": v["name"], "region": v["region"],
                         "kind": v["kind"]} for k, v in SOURCES.items()])
        elif route == "/api/settings":
            self._json(llm.public_config())
        elif route == "/api/tasks/public":
            self._json(_public_tasks(store.list_tasks()))
        elif route == "/api/tasks":
            self._json(store.list_tasks())
        elif route == "/api/articles":
            self._articles(q)
        elif route == "/api/digest":
            if not q.get("task_id"):
                self._json({"error": "需要 task_id 参数"}, 400)
                return
            task_id = int(q["task_id"])
            if not store.get_task(task_id):
                self._json({"error": "任务不存在"}, 404)
                return
            snap = store.read_digest(task_id)
            self._json(snap if snap else {"empty": True})
        elif route == "/api/feedback":
            if not q.get("task_id"):
                self._json({"error": "需要 task_id 参数"}, 400)
                return
            fb = store.get_feedback(int(q["task_id"]))
            self._json({v["url"]: v["signal"] for v in fb.values()})
        elif route == "/api/runs":
            self._json(store.list_runs(
                task_id=q.get("task_id"),
                limit=min(int(q.get("limit", 20)), 100)))
        elif route == "/api/trending":          # GitHub 趋势榜（公开，独立专页用，不经任务关键词过滤）
            self._json(github_trending_list(q.get("period", "today"),
                                            q.get("language") or "All"))
        elif route == "/api/skills/board":       # 热门 Skill 榜（公开只读快照，不打外网）
            board_type = q.get("type", "hot")
            if board_type not in _SKILLS_BOARD_TYPES:
                self._json({"error": f"未知榜单类型: {board_type}"}, 400)
                return
            period = q.get("period") or "recent"
            snap = store.read_skills_board(board_type, period)
            self._json(snap if snap else {"rows": [], "snapshot_time": None,
                                          "sources": [], "status": "warming-up"})
        elif route.startswith("/api/"):
            self._json({"error": "not found"}, 404)
        else:
            self._static(route)

    def _articles(self, q: dict):
        self._json(store.list_articles(
            task_id=q.get("task_id"),
            source=q.get("source"),
            q=q.get("q"),
            sort=q.get("sort", "score"),
            limit=min(int(q.get("limit", 50)), 200),
            offset=int(q.get("offset", 0)),
        ))

    def do_POST(self):
        if not self._authed():
            self._json({"error": "未授权"}, 401)
            return
        route = self.path.split("?")[0]
        m = re.fullmatch(r"/api/tasks/(\d+)/run", route)
        if m:
            task_id = int(m.group(1))
            if not store.get_task(task_id):
                self._json({"error": "任务不存在"}, 404)
                return
            threading.Thread(target=pipeline.run_task, args=(task_id,), daemon=True).start()
            self._json({"ok": True, "message": "已开始执行"})
            return
        if route == "/api/feedback":
            b = self._body()
            task_id = b.get("task_id")
            url = (b.get("url") or "").strip()
            signal = b.get("signal", "none")
            if not task_id or not url:
                self._json({"error": "缺少 task_id 或 url"}, 400)
                return
            if signal not in ("like", "dislike", "none"):
                self._json({"error": "signal 非法"}, 400)
                return
            if not store.get_task(int(task_id)):
                self._json({"error": "任务不存在"}, 404)
                return
            store.set_feedback(int(task_id), pipeline.url_hash(url), url,
                               (b.get("title") or "").strip(), signal)
            self._json({"ok": True})
            return
        m = re.fullmatch(r"/api/digest/(\d+)/refresh", route)
        if m:
            task_id = int(m.group(1))
            if not store.get_task(task_id):
                self._json({"error": "任务不存在"}, 404)
                return
            snap = digest.generate_and_save(task_id)  # 同步生成（含 LLM 调用，可能较慢）
            self._json(snap if snap else {"empty": True})
            return
        if route == "/api/trending/refresh":
            # 趋势榜强制刷新：force=True 真正重拉 OSSInsight（耗其配额），仅管理员可调
            # （do_POST 开头已做 _authed() 校验）。公开 GET /api/trending 仍只读缓存快照。
            b = self._body()
            rows = github_trending_list(b.get("period", "today"),
                                        b.get("language") or "All", force=True)
            self._json(rows)
            return
        if route == "/api/skills/refresh":
            # 热门 Skill 榜强制刷新：仅管理员（do_POST 开头已 _authed），同 type 5 分钟冷却
            b = self._body()
            board_type = b.get("type")
            if board_type is not None and board_type not in _SKILLS_BOARD_TYPES:
                self._json({"error": f"未知榜单类型: {board_type}"}, 400)
                return
            period = b.get("period") or "recent"
            cool = _skills_cooldown_remaining(board_type)
            if cool > 0:
                self._json({"error": f"冷却中，请 {cool} 秒后再试", "cooldown": cool}, 429)
                return
            data = skills_board.warm_skills(board_type, period)
            _mark_skills_refresh(board_type)
            self._json(data)
            return
        if route == "/api/settings/test":
            b = self._body()
            self._json(llm.test_connection(
                b.get("provider", "auto"), b.get("base_url", ""),
                b.get("model", ""), b.get("api_key")))
            return
        if route == "/api/tasks":
            body = self._body()
            err = _validate_task(body)
            if err:
                self._json({"error": err}, 400)
                return
            body["name"] = body["name"].strip()
            task = store.create_task(body)
            scheduler.reschedule_task(task["id"])
            # 新任务立刻跑一次，页面马上有内容
            threading.Thread(target=pipeline.run_task, args=(task["id"],), daemon=True).start()
            self._json(store.get_task(task["id"]), 201)
            return
        self._json({"error": "not found"}, 404)

    def do_PUT(self):
        if not self._authed():
            self._json({"error": "未授权"}, 401)
            return
        route = self.path.split("?")[0]
        if route == "/api/settings":
            b = self._body()
            provider = b.get("provider", "auto")
            if provider not in ("auto", "ollama", "openai", "anthropic", "off"):
                self._json({"error": f"未知 provider: {provider}"}, 400)
                return
            self._json(llm.save_config(provider, b.get("base_url", ""),
                                       b.get("model", ""), b.get("api_key")))
            return
        m = re.fullmatch(r"/api/tasks/(\d+)", route)
        if not m:
            self._json({"error": "not found"}, 404)
            return
        task_id = int(m.group(1))
        if not store.get_task(task_id):
            self._json({"error": "任务不存在"}, 404)
            return
        body = self._body()
        err = _validate_task(body)
        if err:
            self._json({"error": err}, 400)
            return
        body["name"] = body["name"].strip()
        store.update_task(task_id, body)
        patch = {f: body[f] for f in ("preferred_keywords", "excluded_keywords") if f in body}
        if patch:
            store.patch_task(task_id, **patch)
        scheduler.reschedule_task(task_id)
        self._json(store.get_task(task_id))

    def do_DELETE(self):
        if not self._authed():
            self._json({"error": "未授权"}, 401)
            return
        m = re.fullmatch(r"/api/tasks/(\d+)", self.path.split("?")[0])
        if not m:
            self._json({"error": "not found"}, 404)
            return
        store.delete_task(int(m.group(1)))
        self._json({"ok": True})


def _resolve_token(cli_token: str | None, config_path: str) -> str:
    """管理 token 解析优先级：命令行 --token > config.json 的 admin_token > 自动生成。
    自动生成的 token 写回 config.json 持久化（保留 server / llm 等其它键），重启后固定不变；
    也可手动编辑 config.json 改成自定义 token。"""
    if cli_token:
        return cli_token
    cfg_path = Path(config_path)
    cfg = {}
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    tok = (cfg.get("admin_token") or "").strip()
    if tok:
        return tok
    # 未配置：生成随机 token 写回 config.json 持久化（合并写，不丢失其它配置），下次启动复用
    tok = secrets.token_urlsafe(32)
    cfg["admin_token"] = tok
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return tok


def serve(host: str, port: int, config_path: str, token: str | None = None):
    """启动服务。调用前需先 store.init(data_dir, config_path) 完成存储初始化。"""
    global TOKEN
    TOKEN = _resolve_token(token, config_path)
    scheduler.start()
    httpd = ThreadingHTTPServer((host, port), Handler)
    shown = "127.0.0.1" if host == "0.0.0.0" else host
    print(f"📡 拾光 GlowFeed 已启动: http://{shown}:{port}", flush=True)
    print(f"🔑 管理链接（妥善保管）: http://{shown}:{port}/?token={TOKEN}", flush=True)
    httpd.serve_forever()
