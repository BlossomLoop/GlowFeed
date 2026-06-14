"""HTTP 工具：直连优先，被墙源自动探测本地代理，全程无需任何 API key。

参考 last30days skill 的 http.py：统一 UA、gzip、超时、快速失败。
"""
import gzip
import io
import json
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# 常见本地代理端口（Clash/FlClash/V2Ray/Surge 默认值），按序探测
PROXY_PORT_CANDIDATES = [7890, 7897, 1080, 6152, 8118]

_lock = threading.Lock()
_working_proxy: str | None = None
_proxy_probed = False


def _probe_local_proxy() -> str | None:
    """探测本机正在监听的代理端口，结果缓存。"""
    global _working_proxy, _proxy_probed
    with _lock:
        if _proxy_probed:
            return _working_proxy
        for port in PROXY_PORT_CANDIDATES:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                    _working_proxy = f"http://127.0.0.1:{port}"
                    break
            except OSError:
                continue
        _proxy_probed = True
        return _working_proxy


def _build_opener(proxy: str | None) -> urllib.request.OpenerDirector:
    # 始终显式指定代理配置，避免继承环境变量里的坏代理
    handler = urllib.request.ProxyHandler(
        {"http": proxy, "https": proxy} if proxy else {}
    )
    return urllib.request.build_opener(handler)


def _read_body(resp) -> bytes:
    raw = resp.read()
    if resp.headers.get("Content-Encoding") == "gzip":
        raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
    return raw


def get(
    url: str,
    headers: dict | None = None,
    timeout: int = 10,
    via_proxy: bool = False,
    cookie_jar=None,
) -> str | None:
    """GET 请求返回文本；失败返回 None（聚合管线对单源失败容忍）。

    via_proxy=True 时：先直连（短超时），失败后尝试本地代理。
    """
    hdrs = {"User-Agent": UA, "Accept-Encoding": "gzip"}
    if headers:
        hdrs.update(headers)

    attempts: list[str | None] = [None]
    if via_proxy:
        proxy = _probe_local_proxy()
        if proxy:
            attempts = [None, proxy]

    for proxy in attempts:
        try:
            opener = _build_opener(proxy)
            if cookie_jar is not None:
                opener.add_handler(urllib.request.HTTPCookieProcessor(cookie_jar))
            req = urllib.request.Request(url, headers=hdrs)
            # 直连失败要快，给代理留时间
            t = min(timeout, 6) if (proxy is None and via_proxy) else timeout
            with opener.open(req, timeout=t) as resp:
                return _read_body(resp).decode("utf-8", errors="replace")
        except Exception:
            continue
    return None


def open_url(method: str, url: str, data: str | bytes | None = None,
             headers: dict | None = None, timeout: int = 60,
             via_proxy: bool = False) -> tuple[int, str]:
    """底层请求，返回 (status_code, body_text)。

    - 应用层错误（4xx/5xx）也如实返回其 status + body，供调用方读取错误详情
    - 网络层失败（连不上/超时）返回 (0, 错误信息)
    - 始终显式绕过环境变量代理；via_proxy=True 时直连失败再尝试本地代理
    LLM 云端 API（可能被墙）用 via_proxy=True；国内兼容服务直连即可。
    """
    raw = data.encode() if isinstance(data, str) else data
    hdrs = {"User-Agent": UA, "Accept-Encoding": "gzip"}
    if raw is not None:
        hdrs["Content-Type"] = "application/json"  # 本项目 POST body 均为 JSON
    if headers:
        hdrs.update(headers)  # 调用方可覆盖

    attempts: list[str | None] = [None]
    if via_proxy:
        proxy = _probe_local_proxy()
        if proxy:
            attempts.append(proxy)

    last_err = "无法连接到服务"
    for proxy in attempts:
        try:
            opener = _build_opener(proxy)
            req = urllib.request.Request(url, data=raw, headers=hdrs, method=method)
            t = min(timeout, 8) if (proxy is None and via_proxy) else timeout
            with opener.open(req, timeout=t) as resp:
                return resp.status, _read_body(resp).decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            # 服务器已响应（网络通），是应用层错误：返回详情，不再换代理重试
            try:
                body = _read_body(e).decode("utf-8", errors="replace")
            except Exception:
                body = ""
            return e.code, body
        except Exception as ex:
            last_err = str(ex)
            continue
    return 0, last_err


def get_json(url: str, headers: dict | None = None, timeout: int = 10,
             via_proxy: bool = False, cookie_jar=None):
    text = get(url, headers=headers, timeout=timeout,
               via_proxy=via_proxy, cookie_jar=cookie_jar)
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def quote(s: str) -> str:
    return urllib.parse.quote_plus(s)
