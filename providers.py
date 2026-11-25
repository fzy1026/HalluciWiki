"""多模型轮询 + 健康状态记录（应用内轮询，不需要额外网关）。

模型清单在 models.json，挂载进容器，改完自动热加载，不用重建镜像。
每一项按 OpenAI Chat Completions 的字段写：

{
  "version": 1,
  "defaults": { "timeout": 90, "temperature": 0.7 },
  "providers": [
    {
      "id": "deepseek",
      "name": "DeepSeek",
      "base_url": "https://api.deepseek.com/v1",
      "api_key": "${DEEPSEEK_API_KEY}",
      "model": "deepseek-chat",
      "temperature": 0.7,
      "max_tokens": 8192
    },
    {
      "id": "siliconflow",
      "base_url": "https://api.siliconflow.cn/v1",
      "api_key": "${SILICONFLOW_API_KEY}",
      "models": ["Qwen/Qwen2.5-7B-Instruct", "THUDM/glm-4-9b-chat"]
    }
  ]
}

约定：
- 轮询顺序 = providers 数组顺序；同一项写了 models 数组时，每个模型各占一个轮询位。
- api_key 支持 ${ENV_VAR} 引用（值来自容器环境，即 compose 的 env_file），也可以直接写明文。
- 除 id/name/base_url/api_key/model/models/enabled/timeout/headers/params 以外的字段，
  原样并入请求体（如 top_p / stop / presence_penalty / response_format ...）。
- enabled=false 的项不参与轮询，但监控页仍然显示。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field

import httpx

from config import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL

logger = logging.getLogger("providers")

MODELS_FILE = os.getenv("MODELS_FILE", "/app/models.json")
COOLDOWN_SECONDS = int(os.getenv("LLM_COOLDOWN_SECONDS", "60"))
BAD_KEY_COOLDOWN_SECONDS = int(os.getenv("LLM_BAD_KEY_COOLDOWN_SECONDS", "1800"))
PROBE_TIMEOUT = float(os.getenv("LLM_PROBE_TIMEOUT", "20"))

OK = "ok"
COOLING = "cooling"
DEAD = "dead"
UNCONFIGURED = "unconfigured"
DISABLED = "disabled"
UNKNOWN = "unknown"

# 供应商普遍用这几个响应头报额度，抓到就直接显示在监控页
QUOTA_HEADERS = {
    "x-ratelimit-remaining-requests": "剩余请求",
    "x-ratelimit-remaining-tokens": "剩余 token",
    "x-ratelimit-limit-requests": "请求上限",
    "retry-after": "等待秒数",
}

_RESERVED = {
    "id", "name", "base_url", "api_key", "model", "models",
    "enabled", "timeout", "headers", "params", "comment", "_comment",
}


def _expand_env(value: str) -> str:
    """把 ${VAR} / $VAR 换成环境变量值。"""
    if not value:
        return ""
    out = value
    if "${" in out:
        for name, val in os.environ.items():
            out = out.replace("${%s}" % name, val)
    return out


@dataclass
class Model:
    id: str
    name: str
    base_url: str
    api_key: str
    model: str
    enabled: bool = True
    timeout: float = 90.0
    params: dict = field(default_factory=dict)
    headers: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.id}:{self.model}"

    @property
    def chat_url(self) -> str:
        return self.base_url.rstrip("/") + "/chat/completions"

    @property
    def configured(self) -> bool:
        return bool(self.api_key.strip())

    @property
    def masked_key(self) -> str:
        k = self.api_key.strip()
        if not k:
            return "（未配置）"
        if len(k) <= 10:
            return "****"
        return f"{k[:4]}…{k[-4:]}"


@dataclass
class Stats:
    state: str = UNKNOWN
    cooldown_until: float = 0.0
    last_error: str = ""
    last_http: int | None = None
    last_latency_ms: int | None = None
    last_checked: float | None = None
    last_used: float | None = None
    success: int = 0
    failure: int = 0
    consecutive: int = 0
    last_chars: int = 0
    last_finish: str = ""
    echo_model: str = ""
    quota: dict = field(default_factory=dict)


_MODELS: list[Model] = []
_STATS: dict[str, Stats] = {}
_mtime: float = -1.0
_load_error: str = ""
_probe_lock = asyncio.Lock()


def stats_of(m: Model) -> Stats:
    st = _STATS.get(m.key)
    if st is None:
        st = Stats()
        _STATS[m.key] = st
    return st


def _parse(payload: dict) -> list[Model]:
    defaults = payload.get("defaults") or {}
    out: list[Model] = []
    for raw in payload.get("providers") or []:
        if not isinstance(raw, dict):
            continue
        pid = str(raw.get("id") or raw.get("name") or "").strip()
        base_url = _expand_env(str(raw.get("base_url") or "")).strip()
        if not pid or not base_url:
            continue
        name = str(raw.get("name") or pid).strip()
        api_key = _expand_env(str(raw.get("api_key") or "")).strip()
        enabled = bool(raw.get("enabled", True))
        timeout = float(raw.get("timeout") or defaults.get("timeout") or 90)

        params = {k: v for k, v in raw.items() if k not in _RESERVED}
        params.update(raw.get("params") or {})
        if "temperature" not in params and "temperature" in defaults:
            params["temperature"] = defaults["temperature"]
        if "max_tokens" not in params and "max_tokens" in defaults:
            params["max_tokens"] = defaults["max_tokens"]

        headers = {str(k): _expand_env(str(v)) for k, v in (raw.get("headers") or {}).items()}

        models = raw.get("models")
        if not models:
            single = str(raw.get("model") or "").strip()
            models = [single] if single else []
        for m in models:
            out.append(Model(
                id=pid, name=name, base_url=base_url, api_key=api_key,
                model=str(m).strip(), enabled=enabled, timeout=timeout,
                params=dict(params), headers=dict(headers),
            ))
    return out


def reload(force: bool = False) -> None:
    """models.json 变化时重新加载；解析失败则沿用上一份配置并在监控页报错。"""
    global _MODELS, _mtime, _load_error
    try:
        mtime = os.path.getmtime(MODELS_FILE)
    except OSError:
        mtime = 0.0
    if not force and mtime == _mtime and _MODELS:
        return

    payload = None
    if mtime:
        try:
            with open(MODELS_FILE, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except Exception as exc:  # noqa: BLE001
            _load_error = f"{type(exc).__name__}: {exc}"
            logger.error("models.json 读取失败，沿用上一份配置 | %s", _load_error)
            _mtime = mtime
            return

    if payload is None:
        # 没有 models.json 时退回旧变量，保证老部署不会直接挂掉
        base = (DEEPSEEK_BASE_URL or "https://api.deepseek.com").strip().rstrip("/")
        if not base.endswith("/v1"):
            base = base + "/v1"
        _MODELS = [Model(id="deepseek", name="DeepSeek（旧变量）", base_url=base,
                         api_key=(DEEPSEEK_API_KEY or "").strip(),
                         model=(DEEPSEEK_MODEL or "deepseek-chat").strip())]
        _load_error = ""
        _mtime = mtime
        logger.warning("未找到 %s，已用 DEEPSEEK_* 环境变量兜底", MODELS_FILE)
        return

    models = _parse(payload)
    if not models:
        _load_error = "models.json 里没有任何有效 provider（缺 id / base_url）"
        logger.error(_load_error)
        if _MODELS:
            return
    _MODELS = models
    _load_error = ""
    _mtime = mtime
    logger.info("模型清单已加载 | 共 %d 个 | %s", len(models),
                ", ".join(f"{m.id}/{m.model}" for m in models))


def load_error() -> str:
    return _load_error


def models() -> list[Model]:
    return _MODELS


def candidates(now: float | None = None) -> list[Model]:
    """可用优先、冷却中的按解冻时间排在后面（全冷却时也能兜底硬试一次）。"""
    now = now or time.time()
    usable = [m for m in _MODELS if m.enabled]
    ready = [m for m in usable if stats_of(m).cooldown_until <= now]
    cooling = sorted([m for m in usable if stats_of(m).cooldown_until > now],
                     key=lambda m: stats_of(m).cooldown_until)
    return ready + cooling


class AllModelsFailed(Exception):
    pass


def _capture_quota(st: Stats, headers) -> None:
    for header, _label in QUOTA_HEADERS.items():
        value = headers.get(header)
        if value:
            st.quota[header] = value


def _fail(m: Model, st: Stats, *, state: str, error: str, cooldown: float,
          http: int | None = None, latency_ms: int | None = None) -> None:
    st.state = state
    st.last_error = error[:240]
    st.last_http = http
    st.last_latency_ms = latency_ms
    st.last_checked = time.time()
    st.failure += 1
    st.consecutive += 1
    if cooldown:
        st.cooldown_until = time.time() + cooldown


def _ok(m: Model, st: Stats, *, latency_ms: int, http: int, chars: int, finish: str,
        echo: str = "") -> None:
    st.state = OK
    st.last_error = ""
    st.last_http = http
    st.last_latency_ms = latency_ms
    st.last_checked = time.time()
    st.last_used = time.time()
    st.last_chars = chars
    st.last_finish = finish
    if echo:
        st.echo_model = echo
    st.success += 1
    st.consecutive = 0
    st.cooldown_until = 0.0


async def chat(
    client: httpx.AsyncClient,
    prompt: str,
    *,
    purpose: str = "wiki",
    min_chars: int = 0,
    max_tokens: int | None = None,
) -> tuple[str, Model]:
    """按顺序尝试各模型，返回 (正文, 命中的模型)。全部失败抛 AllModelsFailed。"""
    reload()
    order = candidates()
    if not order:
        raise AllModelsFailed("没有任何可用模型，请检查 models.json")

    errors: list[str] = []
    for m in order:
        if not m.configured:
            continue
        st = stats_of(m)

        payload = dict(m.params)
        payload["model"] = m.model
        payload["messages"] = [{"role": "user", "content": prompt}]
        if max_tokens:
            payload["max_tokens"] = max_tokens

        headers = {"Content-Type": "application/json", **m.headers}
        headers["Authorization"] = f"Bearer {m.api_key}"

        started = time.monotonic()
        try:
            resp = await client.post(m.chat_url, json=payload, headers=headers, timeout=m.timeout)
        except Exception as exc:  # noqa: BLE001
            latency = int((time.monotonic() - started) * 1000)
            msg = f"{type(exc).__name__}: {exc}"[:200]
            _fail(m, st, state=COOLING, error=msg, cooldown=COOLDOWN_SECONDS, latency_ms=latency)
            logger.warning("模型失败，切换下一个 | %s/%s | %s", m.id, m.model, msg)
            errors.append(f"{m.name}({msg[:60]})")
            continue

        latency = int((time.monotonic() - started) * 1000)
        _capture_quota(st, resp.headers)

        if resp.status_code != 200:
            body = " ".join(resp.text.split())[:180]
            if resp.status_code in (401, 403):
                _fail(m, st, state=DEAD, error=f"HTTP {resp.status_code} 鉴权失败 {body}",
                      cooldown=BAD_KEY_COOLDOWN_SECONDS, http=resp.status_code, latency_ms=latency)
            elif resp.status_code == 404:
                _fail(m, st, state=DEAD, error=f"HTTP 404 base_url 或模型名不对 {body}",
                      cooldown=BAD_KEY_COOLDOWN_SECONDS, http=resp.status_code, latency_ms=latency)
            elif resp.status_code == 429:
                retry = (resp.headers.get("retry-after") or "").strip()
                wait = float(retry) if retry.replace(".", "", 1).isdigit() else COOLDOWN_SECONDS
                _fail(m, st, state=COOLING, error=f"HTTP 429 限流 {body}",
                      cooldown=min(wait, 900.0), http=resp.status_code, latency_ms=latency)
            else:
                _fail(m, st, state=COOLING, error=f"HTTP {resp.status_code} {body}",
                      cooldown=COOLDOWN_SECONDS, http=resp.status_code, latency_ms=latency)
            logger.warning("模型失败，切换下一个 | %s/%s | HTTP %s", m.id, m.model, resp.status_code)
            errors.append(f"{m.name}(HTTP {resp.status_code})")
            continue

        try:
            data = resp.json()
            choice = data["choices"][0]
            content = (choice.get("message") or {}).get("content") or ""
            finish = choice.get("finish_reason") or ""
            echo = str(data.get("model") or "")
        except Exception as exc:  # noqa: BLE001
            _fail(m, st, state=COOLING, error=f"响应解析失败 {type(exc).__name__}: {exc}",
                  cooldown=COOLDOWN_SECONDS, http=resp.status_code, latency_ms=latency)
            errors.append(f"{m.name}(响应格式异常)")
            continue

        if finish == "length":
            _fail(m, st, state=COOLING,
                  error=f"输出被截断（finish_reason=length，{len(content)} 字）",
                  cooldown=COOLDOWN_SECONDS, http=resp.status_code, latency_ms=latency)
            errors.append(f"{m.name}(输出截断)")
            continue

        if min_chars and len(content) < min_chars:
            _fail(m, st, state=COOLING,
                  error=f"输出过短（{len(content)} 字 < 要求 {min_chars} 字）",
                  cooldown=COOLDOWN_SECONDS, http=resp.status_code, latency_ms=latency)
            errors.append(f"{m.name}(输出过短 {len(content)} 字)")
            continue

        _ok(m, st, latency_ms=latency, http=resp.status_code, chars=len(content),
            finish=finish, echo=echo)
        logger.info("模型出稿成功 | %s/%s | %d 字 | %dms | %s",
                    m.id, m.model, len(content), latency, purpose)
        return content, m

    detail = "；".join(errors[:4]) if errors else "没有已配置密钥的模型"
    raise AllModelsFailed(detail)


async def _probe_one(client: httpx.AsyncClient, m: Model) -> None:
    st = stats_of(m)
    if not m.configured:
        st.state = UNCONFIGURED
        st.last_checked = time.time()
        return

    payload = {"model": m.model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 4}
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {m.api_key}", **m.headers}
    started = time.monotonic()
    try:
        resp = await client.post(m.chat_url, json=payload, headers=headers, timeout=PROBE_TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        latency = int((time.monotonic() - started) * 1000)
        _fail(m, st, state=COOLING, error=f"{type(exc).__name__}: {exc}"[:200],
              cooldown=COOLDOWN_SECONDS, latency_ms=latency)
        return

    latency = int((time.monotonic() - started) * 1000)
    _capture_quota(st, resp.headers)

    if resp.status_code == 200:
        try:
            data = resp.json()
            content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            echo = str(data.get("model") or "")
        except Exception:  # noqa: BLE001
            content, echo = "", ""
        _ok(m, st, latency_ms=latency, http=200, chars=len(content), finish="probe", echo=echo)
        return

    body = " ".join(resp.text.split())[:160]
    if resp.status_code in (401, 403):
        _fail(m, st, state=DEAD, error=f"HTTP {resp.status_code} 鉴权失败 {body}",
              cooldown=BAD_KEY_COOLDOWN_SECONDS, http=resp.status_code, latency_ms=latency)
    elif resp.status_code == 404:
        _fail(m, st, state=DEAD, error=f"HTTP 404 base_url 或模型名不对 {body}",
              cooldown=BAD_KEY_COOLDOWN_SECONDS, http=resp.status_code, latency_ms=latency)
    else:
        _fail(m, st, state=COOLING, error=f"HTTP {resp.status_code} {body}",
              cooldown=COOLDOWN_SECONDS, http=resp.status_code, latency_ms=latency)


async def probe_all() -> None:
    """并发给所有已配置模型发一个 4 token 的 ping，刷新监控页状态。"""
    reload()
    async with _probe_lock:
        targets = [m for m in _MODELS if m.enabled]
        for m in targets:
            if not m.configured:
                stats_of(m).state = UNCONFIGURED
        live = [m for m in targets if m.configured]
        if not live:
            return
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT + 5) as client:
            await asyncio.gather(*(_probe_one(client, m) for m in live))


async def probe_one(model_key: str) -> bool:
    """只检测指定的一个模型，key 形如 "siliconflow:Qwen/Qwen2.5-7B-Instruct"。"""
    reload()
    target = next((m for m in _MODELS if m.key == model_key), None)
    if target is None:
        return False
    if not target.configured:
        stats_of(target).state = UNCONFIGURED
        stats_of(target).last_checked = time.time()
        return True
    async with _probe_lock:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT + 5) as client:
            await _probe_one(client, target)
    return True


def _state_of(m: Model, now: float) -> str:
    st = stats_of(m)
    if not m.enabled:
        return DISABLED
    if not m.configured:
        return UNCONFIGURED
    if st.state == DEAD:
        return DEAD
    if st.cooldown_until > now:
        return COOLING
    return st.state


STATE_TEXT = {
    OK: "正常",
    COOLING: "冷却中",
    DEAD: "不可用",
    UNCONFIGURED: "未配置密钥",
    DISABLED: "已停用",
    UNKNOWN: "未检测",
}


def snapshot() -> dict:
    reload()
    now = time.time()
    rows = []
    for order, m in enumerate(_MODELS, start=1):
        st = stats_of(m)
        state = _state_of(m, now)
        rows.append({
            "priority": order,
            "kind": m.id,
            "key_id": m.key,
            "id": m.id,
            "name": m.name,
            "model": m.model,
            "base_url": m.base_url,
            "key": m.masked_key,
            "state": state,
            "state_text": STATE_TEXT.get(state, state),
            "cooldown_left": max(0, int(st.cooldown_until - now)),
            "last_http": st.last_http,
            "latency_ms": st.last_latency_ms,
            "last_error": st.last_error,
            "last_checked": st.last_checked,
            "last_used": st.last_used,
            "success": st.success,
            "failure": st.failure,
            "consecutive": st.consecutive,
            "chars": st.last_chars,
            "finish": st.last_finish,
            "echo_model": st.echo_model,
            "quota": st.quota,
        })
    usable = sum(1 for r in rows if r["state"] == OK)
    cooling = sum(1 for r in rows if r["state"] == COOLING)
    dead = sum(1 for r in rows if r["state"] == DEAD)
    unconfigured = sum(1 for r in rows if r["state"] == UNCONFIGURED)
    calls = sum(r["success"] + r["failure"] for r in rows)
    return {
        "rows": rows,
        "overview": {
            "total": len(rows),
            "usable": usable,
            "cooling": cooling,
            "dead": dead,
            "unconfigured": unconfigured,
            "calls": calls,
            "success": sum(r["success"] for r in rows),
            "failure": sum(r["failure"] for r in rows),
        },
        "load_error": _load_error,
        "models_file": MODELS_FILE,
        "generated_at": now,
    }


reload(force=True)
