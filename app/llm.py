"""LLM 接入层：统一的综述生成接口，按配置分派到不同 provider。

支持四种 provider（配置存仓库根 config.json 的 llm 节，也可在页面填写）：
- auto      : 默认。自动探测本机 Ollama（localhost:11434），有则用，无则降级算法摘要
- ollama    : 显式指定 Ollama 端点（可改 base_url 指向远程）
- openai    : OpenAI 风格 /chat/completions（兼容 DeepSeek / Moonshot / 通义 / OneAPI / vLLM 等）
- anthropic : Anthropic 风格 /v1/messages
- off       : 永不调用模型，始终算法摘要

云端 provider 走 via_proxy（被墙时自动尝试本地代理）；国内兼容服务直连优先。
"""
import json
import re

from . import http_util, store

DEFAULT = {"provider": "auto", "base_url": "", "api_key": "", "model": ""}
OLLAMA_DEFAULT_URL = "http://localhost:11434"


def get_config() -> dict:
    cfg = dict(DEFAULT)
    cfg.update(store.read_config().get("llm", {}))
    return cfg


def save_config(provider: str, base_url: str, model: str, api_key: str | None) -> dict:
    """保存配置；api_key 为 None/空表示沿用已存密钥（前端不回显明文）。"""
    cur = get_config()
    llm = {
        "provider": provider,
        "base_url": (base_url or "").strip(),
        "model": (model or "").strip(),
        "api_key": api_key if api_key else cur.get("api_key", ""),
    }
    store.write_config({"llm": llm})
    return public_config()


def public_config() -> dict:
    """供前端读取：不回显 api_key 明文，仅告知是否已设置。"""
    cfg = get_config()
    return {
        "provider": cfg["provider"],
        "base_url": cfg["base_url"],
        "model": cfg["model"],
        "api_key_set": bool(cfg["api_key"]),
    }


# ---------------- provider 实现 ----------------

def _ollama_first_model(base_url: str) -> str | None:
    data = http_util.get_json(f"{base_url.rstrip('/')}/api/tags", timeout=2)
    models = (data or {}).get("models", [])
    return models[0]["name"] if models else None


def _call_ollama(base_url: str, model: str, system: str, user: str) -> tuple[str | None, str]:
    payload = {"model": model, "prompt": f"{system}\n\n{user}",
               "stream": False, "options": {"temperature": 0.4}}
    status, body = http_util.open_url("POST", f"{base_url.rstrip('/')}/api/generate",
                                      json.dumps(payload), timeout=90)
    if status == 200:
        try:
            return (json.loads(body).get("response") or "").strip() or None, None
        except ValueError:
            return None, "响应解析失败"
    return None, f"HTTP {status}: {body[:160]}"


def _call_openai(cfg: dict, system: str, user: str,
                 max_tokens: int = 600) -> tuple[str | None, str]:
    base = cfg["base_url"].rstrip("/") or "https://api.openai.com/v1"
    payload = {
        "model": cfg["model"],
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": 0.4, "max_tokens": max_tokens, "stream": False,
    }
    status, body = http_util.open_url(
        "POST", f"{base}/chat/completions", json.dumps(payload),
        headers={"Authorization": f"Bearer {cfg['api_key']}"},
        timeout=60, via_proxy=True)
    if status == 200:
        try:
            return json.loads(body)["choices"][0]["message"]["content"].strip(), None
        except (ValueError, KeyError, IndexError):
            return None, "响应格式非 OpenAI 风格"
    return None, f"HTTP {status}: {body[:160]}"


def _call_anthropic(cfg: dict, system: str, user: str,
                    max_tokens: int = 600) -> tuple[str | None, str]:
    base = cfg["base_url"].rstrip("/") or "https://api.anthropic.com"
    payload = {
        "model": cfg["model"], "max_tokens": max_tokens, "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    status, body = http_util.open_url(
        "POST", f"{base}/v1/messages", json.dumps(payload),
        headers={"x-api-key": cfg["api_key"], "anthropic-version": "2023-06-01"},
        timeout=60, via_proxy=True)
    if status == 200:
        try:
            return json.loads(body)["content"][0]["text"].strip(), None
        except (ValueError, KeyError, IndexError):
            return None, "响应格式非 Anthropic 风格"
    return None, f"HTTP {status}: {body[:160]}"


def _dispatch(cfg: dict, system: str, user: str,
              max_tokens: int = 600) -> tuple[str | None, str, str | None]:
    """返回 (文本, 标签, 错误)。标签如 'openai:gpt-4o-mini'，失败时文本为 None。

    max_tokens 控制云端 provider 的输出上限；抽取类长输出（如清单体文章抽多个 skill）
    需调大，否则 JSON 会被截断。Ollama 走自身默认，不受此参数约束。"""
    provider = cfg.get("provider", "auto")
    if provider == "off":
        return None, "extractive", None
    if provider == "auto":
        model = _ollama_first_model(OLLAMA_DEFAULT_URL)
        if not model:
            return None, "extractive", None
        text, err = _call_ollama(OLLAMA_DEFAULT_URL, model, system, user)
        return text, f"ollama:{model}", err
    if provider == "ollama":
        base = cfg["base_url"] or OLLAMA_DEFAULT_URL
        model = cfg["model"] or _ollama_first_model(base)
        if not model:
            return None, "extractive", "未找到 Ollama 模型"
        text, err = _call_ollama(base, model, system, user)
        return text, f"ollama:{model}", err
    if provider == "openai":
        text, err = _call_openai(cfg, system, user, max_tokens)
        return text, f"openai:{cfg['model']}", err
    if provider == "anthropic":
        text, err = _call_anthropic(cfg, system, user, max_tokens)
        return text, f"anthropic:{cfg['model']}", err
    return None, "extractive", f"未知 provider: {provider}"


def summarize(system: str, user: str, max_tokens: int = 600) -> tuple[str | None, str]:
    """生成综述。失败/未配置时返回 (None, 'extractive')，由调用方降级。

    max_tokens 默认 600（综述足够）；抽取类长输出可调大避免 JSON 截断。"""
    cfg = get_config()
    text, label, _ = _dispatch(cfg, system, user, max_tokens)
    return (text, label) if text else (None, "extractive")


def test_connection(provider: str, base_url: str, model: str,
                    api_key: str | None) -> dict:
    """测试一次最小调用，返回 {ok, message}。供设置页「测试连接」用。"""
    cur = get_config()
    cfg = {
        "provider": provider,
        "base_url": (base_url or "").strip(),
        "model": (model or "").strip(),
        "api_key": api_key if api_key else cur.get("api_key", ""),
    }
    if provider in ("openai", "anthropic"):
        if not cfg["model"]:
            return {"ok": False, "message": "请填写模型名称"}
        if not cfg["api_key"]:
            return {"ok": False, "message": "请填写 API Key"}
    text, label, err = _dispatch(cfg, "你是测试助手。", "请只回复两个字：正常")
    if text:
        return {"ok": True, "message": f"连接成功（{label}）：{text[:40]}"}
    if label == "extractive" and provider in ("auto", "off"):
        return {"ok": True, "message": "当前为本地/算法模式，无需云端连接"}
    return {"ok": False, "message": err or "调用失败"}


def merge_keywords(existing: list[str], new: list[str],
                   base: list[str], limit: int = 20) -> list[str]:
    """把新学到的词并入已有学习词：保序去重、剔除与 base（用户手设词）重复者、截断到 limit。"""
    base_low = {b.strip().lower() for b in base}
    out, seen = [], set()
    for w in [*existing, *new]:
        k = w.strip()
        if not k or k.lower() in base_low or k.lower() in seen:
            continue
        seen.add(k.lower())
        out.append(k)
    return out[:limit]


def _parse_keywords(text: str) -> tuple[list[str], list[str]]:
    """从模型输出里抽取 JSON {"preferred":[...],"excluded":[...]}，失败返回 ([], [])。"""
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not m:
        return [], []
    try:
        obj = json.loads(m.group(0))
    except ValueError:
        return [], []
    pref = [str(x).strip() for x in obj.get("preferred", []) if str(x).strip()]
    excl = [str(x).strip() for x in obj.get("excluded", []) if str(x).strip()]
    return pref, excl


def extract_keywords(task_name: str, likes: list[str],
                     dislikes: list[str]) -> tuple[list[str], list[str]]:
    """据喜欢/不喜欢的标题，让模型提炼应强化/排除的检索词。
    模型未配置或调用失败返回 ([], [])，调用方据此跳过本轮学习。"""
    if not likes and not dislikes:
        return [], []
    system = "你是中文资讯检索策略助手，只输出 JSON，不要解释。"
    user = (
        f"任务「{task_name}」的用户反馈如下。\n"
        f"喜欢的标题:\n" + "\n".join(f"- {t}" for t in likes[:30]) + "\n\n"
        f"不喜欢的标题:\n" + "\n".join(f"- {t}" for t in dislikes[:30]) + "\n\n"
        "请提炼用于资讯检索的关键词：preferred 是应强化召回的主题词，"
        "excluded 是应过滤掉的主题词。各最多 8 个，简短（2-6 字/词）。"
        '只输出 JSON：{"preferred":["..."],"excluded":["..."]}'
    )
    cfg = get_config()
    text, _label, _err = _dispatch(cfg, system, user)
    if not text:
        return [], []
    return _parse_keywords(text)
