#!/usr/bin/env python3
"""共享 LLM 调用模块 —— Kimi → Qwen 双通道 fallback。

用法::

    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from llm_fallback import call_llm, is_quota_error

    text, provider = call_llm(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=4096,
        temperature=0.5,
    )

优先级：
1. Kimi (Anthropic Messages API) —— 如果 KIMI_API_KEY 可用且未限额
2. Qwen (OpenAI Chat Completions) —— 阿里云百炼 token-plan 端点
3. 抛出 LLMAllProvidersError

环境变量覆盖：
    LLM_KIMI_URL       Kimi API 端点
    LLM_KIMI_MODEL     Kimi 模型名 (默认 k3)
    LLM_QWEN_URL       Qwen API 端点
    LLM_QWEN_MODEL     Qwen 模型名 (默认 qwen3.7-max)
    LLM_QWEN_API_KEY   Qwen API key
    LLM_SKIP_KIMI=1    跳过 Kimi，直接走 Qwen（Kimi 额度未恢复时用）
"""

import json
import os
import sys
import threading
import urllib.request
import urllib.error

from env_loader import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

KIMI_API_URL = (
    os.environ.get("LLM_KIMI_URL", "").strip()
    or os.environ.get("BELLA_KIMI_API_URL", "").strip()
    or "https://api.kimi.com/coding/v1/messages"
)
KIMI_MODEL = (
    os.environ.get("LLM_KIMI_MODEL", "").strip()
    or os.environ.get("BELLA_KIMI_MODEL", "").strip()
    or "k3"
)

QWEN_API_URL = (
    os.environ.get("LLM_QWEN_URL", "").strip()
    or "https://opencode.ai/zen/go/v1/chat/completions"
)
# 默认使用 OpenCode Go 套餐里的 Qwen 模型（订阅有效）。
QWEN_MODEL = os.environ.get("LLM_QWEN_MODEL", "").strip() or "qwen3.6-plus"
def load_qwen_api_key() -> str:
    """加载 Qwen API key：优先环境变量，随后从 OpenClaw 配置读取 OpenCode Go key。"""
    key = os.environ.get("LLM_QWEN_API_KEY", "").strip()
    if key:
        return key

    # 从 OpenClaw 全局配置读取 OpenCode Go 的 qwen provider key
    config_path = os.path.expanduser("~/.openclaw/openclaw.json")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        qwen_provider = data.get("models", {}).get("providers", {}).get("qwen", {})
        key = (qwen_provider.get("apiKey") or "").strip()
        if key:
            return key
    except (FileNotFoundError, OSError, json.JSONDecodeError, AttributeError):
        pass

    return ""

SKIP_KIMI = os.environ.get("LLM_SKIP_KIMI", "").strip() in ("1", "true", "yes")


def _log(msg: str):
    print(f"[llm_fallback] {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Kimi API key 加载
# ---------------------------------------------------------------------------

def load_kimi_api_key() -> str:
    """从环境变量 / .env / auth-profiles 加载 Kimi API key。"""
    key = os.environ.get("KIMI_API_KEY", "").strip()
    if key:
        return key

    # 尝试 .env 文件
    for env_path in [
        os.environ.get("IB_RESEARCH_ENV", os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")),
        os.path.expanduser("~/.openclaw/.env"),
    ]:
        try:
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("KIMI_API_KEY="):
                        return line.split("=", 1)[1].strip().strip('"').strip("'")
        except (FileNotFoundError, OSError):
            continue

    # 尝试 openclaw auth-profiles
    auth_path = os.path.expanduser("~/.openclaw/agents/main/agent/auth-profiles.json")
    try:
        with open(auth_path) as f:
            data = json.load(f)
        for profile in data.values():
            if isinstance(profile, dict):
                if profile.get("provider") in ("kimi", "kimi-coding"):
                    return profile.get("key", "") or profile.get("apiKey", "")
        # 旧格式
        kimi = data.get("kimi") or data.get("Kimi") or {}
        if isinstance(kimi, dict):
            return kimi.get("apiKey") or kimi.get("api_key") or ""
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        pass

    return ""


# ---------------------------------------------------------------------------
# 错误检测
# ---------------------------------------------------------------------------

_QUOTA_MARKERS = (
    "429", "weekly limit", "week limit", "quota", "rate limit",
    "insufficient balance", "limit exceeded", "额度", "限额", "余额不足",
    "too many requests", "resource exhausted",
)


def is_quota_error(error) -> bool:
    """判断是否为额度/限流错误（Kimi 或通用）。"""
    text = str(error).lower()
    return any(m in text for m in _QUOTA_MARKERS)


class LLMAllProvidersError(RuntimeError):
    """所有 LLM 提供商均失败。"""
    def __init__(self, errors: list):
        self.errors = errors
        providers = ", ".join(f"{p}: {e}" for p, e in errors)
        super().__init__(f"所有 LLM 提供商均失败: {providers}")


# ---------------------------------------------------------------------------
# 代理辅助
# ---------------------------------------------------------------------------

def _get_proxy():
    """Only use a proxy when one is explicitly configured."""
    return (
        os.environ.get("HTTPS_PROXY", "").strip()
        or os.environ.get("https_proxy", "").strip()
        or os.environ.get("HTTP_PROXY", "").strip()
        or os.environ.get("http_proxy", "").strip()
    )


def _make_opener(use_proxy: bool):
    if use_proxy:
        proxy = _get_proxy()
        handler = urllib.request.ProxyHandler({
            "http": proxy, "https": proxy,
        })
        return urllib.request.build_opener(handler)
    return urllib.request.build_opener()


# ---------------------------------------------------------------------------
# 壁钟超时包装
# ---------------------------------------------------------------------------

def _request_with_timeout(url, data, headers, timeout, use_proxy):
    """在 daemon 线程中执行 urlopen，实现真正的壁钟超时。"""
    result = [None]
    error = [None]

    def _do():
        try:
            opener = _make_opener(use_proxy)
            req = urllib.request.Request(url, data=data, headers=headers)
            with opener.open(req, timeout=timeout) as resp:
                result[0] = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            error[0] = e

    t = threading.Thread(target=_do, daemon=True)
    t.start()
    t.join(timeout=timeout + 10)

    if t.is_alive():
        raise TimeoutError(f"LLM 请求超时 ({timeout}s wall-clock)")
    if error[0]:
        raise error[0]
    return result[0]


# ---------------------------------------------------------------------------
# Kimi 调用 (Anthropic Messages API)
# ---------------------------------------------------------------------------

def _extract_kimi_text(data: dict) -> str:
    """从 Kimi/Anthropic 格式响应中提取文本（跳过 thinking 块）。"""
    content = data.get("content", [])
    if isinstance(content, str):
        return content
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            return block.get("text", "")
    # fallback: 拼接所有 text 块
    texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
    if texts:
        return "\n".join(texts)
    raise ValueError(f"Kimi 响应中无 text 块: stop_reason={data.get('stop_reason')}")


def call_kimi(messages, max_tokens=4096, temperature=0.5, model=None,
              api_key=None, timeout=120):
    """调用 Kimi API，返回 (text, "kimi")。"""
    api_key = api_key or load_kimi_api_key()
    if not api_key:
        raise RuntimeError("KIMI_API_KEY 未配置")

    model = model or KIMI_MODEL
    body = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "claude-code/0.1.0",
    }

    data = _request_with_timeout(
        KIMI_API_URL,
        json.dumps(body).encode("utf-8"),
        headers,
        timeout=timeout,
        use_proxy=bool(_get_proxy()),
    )
    text = _extract_kimi_text(data)
    return text, "kimi"


# ---------------------------------------------------------------------------
# Qwen 调用 (OpenAI Chat Completions)
# ---------------------------------------------------------------------------

def call_qwen(messages, max_tokens=4096, temperature=0.5, model=None,
              api_key=None, timeout=120):
    """调用 Qwen API（OpenCode Go 套餐），返回 (text, "qwen"。"""
    # 惰性读取密钥：支持环境变量覆盖，或从 OpenClaw 配置读取 OpenCode Go key。
    api_key = api_key or load_qwen_api_key()
    if not api_key:
        raise RuntimeError("Qwen API key 未配置")

    model = model or QWEN_MODEL
    body = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        # OpenCode Go / opencode.ai 的 CDN 会拦截无 User-Agent 的请求（Cloudflare 1010）。
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
    }

    data = _request_with_timeout(
        QWEN_API_URL,
        json.dumps(body).encode("utf-8"),
        headers,
        timeout=timeout,
        use_proxy=bool(_get_proxy()),
    )

    choices = data.get("choices", [])
    if not choices:
        raise ValueError(f"Qwen 响应无 choices: {json.dumps(data, ensure_ascii=False)[:200]}")
    text = choices[0].get("message", {}).get("content", "")
    if not text:
        raise ValueError("Qwen 响应 content 为空")
    return text, "qwen"


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------

def call_llm(messages, max_tokens=4096, temperature=0.5,
             kimi_model=None, qwen_model=None,
             kimi_api_key=None, qwen_api_key=None,
             timeout=120, skip_kimi=None):
    """统一 LLM 调用：Kimi → Qwen fallback。

    Returns:
        (text: str, provider: str)  — provider 为 "kimi" 或 "qwen"

    Raises:
        LLMAllProvidersError: 所有提供商均失败
    """
    errors = []
    _skip = skip_kimi if skip_kimi is not None else SKIP_KIMI

    # 1) Kimi
    if not _skip:
        try:
            _log(f"尝试 Kimi (model={kimi_model or KIMI_MODEL})...")
            return call_kimi(
                messages, max_tokens=max_tokens, temperature=temperature,
                model=kimi_model, api_key=kimi_api_key, timeout=timeout,
            )
        except Exception as e:
            errors.append(("kimi", e))
            if is_quota_error(e):
                _log(f"Kimi 额度不足/限流: {e}")
            else:
                _log(f"Kimi 调用失败: {e}")

    # 2) Qwen
    try:
        _log(f"切换 Qwen (model={qwen_model or QWEN_MODEL})...")
        result = call_qwen(
            messages, max_tokens=max_tokens, temperature=temperature,
            model=qwen_model, api_key=qwen_api_key, timeout=timeout,
        )
        _log(f"Qwen 调用成功 ({len(result[0])} 字符)")
        return result
    except Exception as e:
        errors.append(("qwen", e))
        _log(f"Qwen 调用失败: {e}")

    raise LLMAllProvidersError(errors)


# ---------------------------------------------------------------------------
# 便捷函数：纯文本 prompt
# ---------------------------------------------------------------------------

def call_llm_text(prompt, max_tokens=4096, temperature=0.5, **kwargs):
    """传入纯文本 prompt，返回 (text, provider)。"""
    return call_llm(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        temperature=temperature,
        **kwargs,
    )


if __name__ == "__main__":
    # 简单测试
    text, provider = call_llm_text("你好，请用一句话介绍你自己。", max_tokens=100)
    print(f"[{provider}] {text}")
