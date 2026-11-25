"""管理员登录：账号密码放在 admin.json（服务器上直接改），网页里登录拿签名 cookie。

admin.json 结构（600 权限，不进 git）：
{
  "username": "admin",
  "password": "明文密码",              # 二选一：明文
  "password_sha256": "十六进制摘要",    # 二选一：摘要（更安全，推荐）
  "secret": "随机字符串",               # 签 cookie 用，缺失会自动生成并写回
  "session_hours": 24,
  "max_attempts": 5,                    # 同一 IP 连续失败多少次后暂时锁定
  "lock_minutes": 10
}

改了 admin.json 保存即生效（按 mtime 热加载），不用重启。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time

logger = logging.getLogger("admin_auth")

CONFIG_FILE = os.getenv("ADMIN_CONFIG_FILE", "/app/admin.json")
COOKIE_NAME = "hw_admin"

DEFAULTS = {
    "username": "admin",
    "password": "",
    "password_sha256": "",
    "secret": "",
    "session_hours": 24,
    "max_attempts": 5,
    "lock_minutes": 10,
}

_cfg: dict = dict(DEFAULTS)
_mtime: float = -1.0
_load_error: str = ""
_fails: dict[str, list[float]] = {}


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def reload(force: bool = False) -> None:
    """按 mtime 热加载配置；secret 缺失时自动生成并写回文件。"""
    global _cfg, _mtime, _load_error

    try:
        mtime = os.path.getmtime(CONFIG_FILE)
    except OSError:
        if _mtime != 0.0:
            logger.warning("找不到 %s，管理员登录不可用", CONFIG_FILE)
        _mtime = 0.0
        _cfg = dict(DEFAULTS)
        _load_error = f"缺少配置文件 {CONFIG_FILE}"
        return

    if not force and mtime == _mtime:
        return

    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except Exception as exc:  # noqa: BLE001
        _load_error = f"{type(exc).__name__}: {exc}"
        logger.error("admin.json 解析失败，沿用上一份配置 | %s", _load_error)
        _mtime = mtime
        return

    cfg = dict(DEFAULTS)
    cfg.update({k: v for k, v in (raw or {}).items() if k in DEFAULTS})
    cfg["username"] = str(cfg.get("username") or "admin").strip()
    cfg["session_hours"] = max(1, int(cfg.get("session_hours") or 24))
    cfg["max_attempts"] = max(1, int(cfg.get("max_attempts") or 5))
    cfg["lock_minutes"] = max(1, int(cfg.get("lock_minutes") or 10))

    if not cfg.get("password") and not cfg.get("password_sha256"):
        _load_error = "admin.json 里没有 password 或 password_sha256，登录会被拒绝"
        logger.error(_load_error)

    if not cfg.get("secret"):
        cfg["secret"] = secrets.token_hex(32)
        try:
            merged = dict(raw or {})
            merged["secret"] = cfg["secret"]
            with open(CONFIG_FILE, "w", encoding="utf-8") as fh:
                json.dump(merged, fh, ensure_ascii=False, indent=2)
            logger.info("已为 admin.json 自动生成签名密钥 secret")
        except OSError as exc:
            logger.warning("secret 写回失败（本次运行仍可用）: %s", exc)

    _cfg = cfg
    _mtime = mtime
    _load_error = ""
    logger.info("管理员配置已加载 | 用户 %s | 会话 %dh", cfg["username"], cfg["session_hours"])


def cfg() -> dict:
    reload()
    return _cfg


def username() -> str:
    return cfg()["username"]


def error() -> str:
    reload()
    return _load_error


# ---------------- 凭据校验 ----------------

def verify(user: str, password: str) -> bool:
    reload()
    c = _cfg
    if not (c.get("password") or c.get("password_sha256")):
        return False
    if not hmac.compare_digest(user.strip(), c["username"]):
        return False
    if c.get("password_sha256"):
        digest = hashlib.sha256((password or "").encode("utf-8")).hexdigest()
        return hmac.compare_digest(digest, str(c["password_sha256"]).lower())
    return hmac.compare_digest(password or "", str(c["password"]))


# ---------------- 会话 cookie ----------------

def issue(user: str) -> str:
    """生成签名令牌：base64(user:exp) + '.' + hmac。"""
    hours = cfg()["session_hours"]
    payload = f"{user}:{int(time.time()) + hours * 3600}"
    sig = hmac.new(cfg()["secret"].encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{_b64(payload.encode())}.{sig}"


def identify(token: str | None) -> str | None:
    """校验令牌，返回用户名；无效/过期返回 None。"""
    if not token or "." not in token:
        return None
    body, _, sig = token.partition(".")
    try:
        payload = _unb64(body).decode()
    except Exception:  # noqa: BLE001
        return None
    expect = hmac.new(cfg()["secret"].encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expect):
        return None
    user, _, exp = payload.rpartition(":")
    try:
        if int(exp) < time.time():
            return None
    except ValueError:
        return None
    if user != cfg()["username"]:
        return None
    return user


# ---------------- 失败限速 ----------------

def _window() -> float:
    return cfg()["lock_minutes"] * 60


def locked(ip: str) -> int:
    """返回剩余锁定秒数，0 表示未锁定。"""
    stamps = [t for t in _fails.get(ip, []) if time.time() - t < _window()]
    _fails[ip] = stamps
    if len(stamps) >= cfg()["max_attempts"]:
        return max(1, int(_window() - (time.time() - stamps[0])))
    return 0


def record_failure(ip: str) -> None:
    _fails.setdefault(ip, []).append(time.time())


def clear_failures(ip: str) -> None:
    _fails.pop(ip, None)


def client_ip(forwarded_for: str | None, fallback: str) -> str:
    """Caddy 会把真实客户端写在 X-Forwarded-For 的第一段。"""
    if forwarded_for:
        first = forwarded_for.split(",")[0].strip()
        if first:
            return first
    return fallback or "unknown"
