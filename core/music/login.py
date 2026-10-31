"""扫码登录与 QQ 音乐自动保活。

两条平台路线差别很大，这里刻意不抽象成同一个基类：

* 网易云：``weapi/login/qrcode/*`` 三个状态码，登录成功
  直接给 ``MUSIC_U``，它本身就是长期凭据，不需要续期；
* QQ 音乐：``ptqrlogin`` 只给一个跳转 URL，必须再走 ``check_sig`` →
  ``oauth2.0/authorize`` → ``QQConnectLogin.LoginServer`` 才能换到
  ``musickey`` + ``refresh_token``，之后靠 refresh 续命。

对外只暴露「任务」这一层：``task_id`` / ``state`` / ``message`` /
``qr_data_url`` / ``nickname``。Cookie 与 refresh_token 只落在内存和
``data_dir`` 下的私有 JSON 里，绝不进入返回值。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import inspect
import json
import os
import re
import secrets
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Final
from urllib.parse import parse_qs, urlsplit

import httpx

from .client import USER_AGENT, fetch_error, open_client, text_of
from .crypto import hash33, music_u_of, weapi_form
from .models import MusicError

PLATFORMS: Final[frozenset[str]] = frozenset({"netease", "qqmusic"})
# 扫码只开放 QQ 音乐。网易云已封禁第三方扫码（上游 MusicUID 同结论）：weapi
# 会被 IP 风控成 8821/-462，interface type=3 产出云端不认的二等会话（媒体 403、
# 安全中心无设备）。网易云统一走 Cookie 导入，见凭据页的获取指引。
QR_PLATFORMS: Final[frozenset[str]] = frozenset({"qqmusic"})

# -- 会话生命周期 -----------------------------------------------------------
TASK_TTL_SEC: Final[float] = 600.0
MAX_TASKS: Final[int] = 8
TASK_ID_LEN: Final[int] = 32

# -- 网易云 -----------------------------------------------------------------
_NETEASE_BASE: Final[str] = "https://music.163.com/"
# 扫码固定走 weapi + type=1（web 会话）：这套会话与浏览器登录同源，云端完全
# 认可（安全中心可见、媒体可下）。interface.music.163.com 的 /api + type=3
# 虽然：风控更宽松，但产出的是云账号体系不认的「二等会话」——媒体 CDN 一律
# 403、安全中心无设备（2026-11-01 真机实测），不能用作登录态。
# 上游 MusicUID 同样用 weapi；被风控（-462/8821）时以 Cookie 导入兜底。
_NETEASE_UNIKEY: Final[str] = "weapi/login/qrcode/unikey"
_NETEASE_CHECK: Final[str] = "weapi/login/qrcode/client/login"
_NETEASE_QR_PREFIX: Final[str] = "https://music.163.com/login?codekey="

# -- QQ 音乐 ----------------------------------------------------------------
_MUSICU_URL: Final[str] = "https://u.y.qq.com/cgi-bin/musicu.fcg"
_QQ_QR_URL: Final[str] = "https://ssl.ptlogin2.qq.com/ptqrshow"
_QQ_POLL_URL: Final[str] = "https://ssl.ptlogin2.qq.com/ptqrlogin"
_QQ_CHECK_SIG_URL: Final[str] = "https://ssl.ptlogin2.graph.qq.com/check_sig"
_QQ_AUTHORIZE_URL: Final[str] = "https://graph.qq.com/oauth2.0/authorize"
_QQ_HEADERS: Final[dict[str, str]] = {
    "User-Agent": USER_AGENT,
    "Referer": "https://y.qq.com/",
}
_QQ_LOGIN_REFERER: Final[str] = "https://xui.ptlogin2.qq.com/"
# 与参考实现保持相同的 QQ 互联授权回调地址。
_QQ_REDIRECT_URI: Final[str] = (
    "https://y.qq.com/portal/wx_redirect.html?login_type=1&surl=https://y.qq.com/"
)
_QQ_QR_PARAMS: Final[dict[str, str]] = {
    "appid": "716027609",
    "e": "2",
    "l": "M",
    "s": "3",
    "d": "72",
    "v": "4",
    "daid": "383",
    "pt_3rd_aid": "100497308",
}
DEFAULT_QQ_NICK: Final[str] = "QQ用户"
DEFAULT_KEY_EXPIRES: Final[int] = 259200
REFRESH_COOLDOWN_SEC: Final[float] = 300.0
REFRESH_FAIL_COOLDOWN_SEC: Final[float] = 60.0

_WAITING: Final[str] = "waiting"
_SCANNED: Final[str] = "scanned"
_SUCCESS: Final[str] = "success"
_EXPIRED: Final[str] = "expired"
_FAILED: Final[str] = "failed"
_PUBLIC_STATES: Final[tuple[str, ...]] = (_WAITING, _SCANNED, _SUCCESS, _EXPIRED, _FAILED)
_COOKIE_HINT: Final[str] = "仍可改用 Cookie 导入登录"


def _fingerprint(cookie: str) -> str:
    """Cookie 指纹：只用于判断「有没有人在此期间换过登录态」。"""
    value = str(cookie or "").strip()
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _int_of(value: Any, fallback: int = 0) -> int:
    if isinstance(value, bool):
        return fallback
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _safe_business_code(value: Any) -> int | None:
    """Return a bounded integer code suitable for logs and user diagnostics."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        code = value
    elif isinstance(value, str) and re.fullmatch(r"-?\d{1,10}", value.strip()):
        code = int(value.strip())
    else:
        return None
    return code if -(2**31) <= code <= 2**31 - 1 else None


class _LoginStageError(MusicError):
    """A safe login failure annotated with a non-secret diagnostic stage."""

    def __init__(self, stage: str, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        self.stage = stage
        self.code = code


class _QQStageError(_LoginStageError):
    """QQ 登录链路中的阶段错误。

    ``stage`` / ``code`` 只用于后台日志定位，用户文案仍只走 ``MusicError``
    的安全消息，不携带 URL、Cookie、token 或服务端原始 ``errMsg``。
    """

def _log_login_failure(platform: str, exc: BaseException) -> None:
    """只记录平台、阶段、异常类型和上游整数码。

    响应体、请求 URL、Cookie、token 与原始错误消息一律不落日志。
    """
    stage = text_of(getattr(exc, "stage", "")) or "unexpected"
    code = getattr(exc, "code", None)
    safe_code = _safe_business_code(code)
    try:
        from astrbot.api import logger

        logger.warning(
            "[denia_share] 音乐登录操作失败: platform=%s stage=%s error=%s code=%s",
            platform,
            stage,
            type(exc).__name__,
            safe_code if safe_code is not None else "-",
        )
    except Exception:  # noqa: BLE001 - 日志不可用不能影响登录状态机
        return


def _music_id_of(value: Any) -> str:
    """Normalize numeric QQ music IDs without losing JSON integer values."""
    if isinstance(value, bool):
        return ""
    if isinstance(value, int):
        return str(value) if value > 0 else ""
    if isinstance(value, str):
        text = value.strip()
        return text if text.isdigit() and any(char != "0" for char in text) else ""
    return ""


def parse_qq_cookie(cookie: str) -> dict[str, str]:
    """把 QQ 音乐的 Cookie 串拆成键值对（只按第一个 ``=`` 切）。"""
    result: dict[str, str] = {}
    for part in str(cookie or "").split(";"):
        key, sep, value = part.partition("=")
        key = key.strip()
        if not sep or not key:
            continue
        result[key] = value.strip()
    return result


def format_qq_cookie(values: dict[str, str]) -> str:
    """按固定顺序拼回 Cookie，避免每次续期都换一个字符串形态。

    保留顺序：``uin`` → ``qm_keyst`` → ``qqmusic_key`` → 其它已知字段。
    未知字段不落盘，防止把浏览器里无关的 Cookie 带进配置。
    """
    order = ("uin", "qm_keyst", "qqmusic_key", "pskey", "skey", "p_skey", "p_uin")
    parts = []
    for key in order:
        value = str(values.get(key) or "").strip()
        if value:
            parts.append(f"{key}={value}")
    return "; ".join(parts)


@dataclass
class QQMobileCredential:
    """QQ 音乐移动端凭据（服务端续期所需的全部字段）。"""

    openid: str = ""
    refresh_token: str = ""
    access_token: str = ""
    expired_at: int = 0
    musicid: str = ""
    musickey: str = ""
    unionid: str = ""
    str_musicid: str = ""
    refresh_key: str = ""
    musickey_create_time: int = 0
    key_expires_in: int = DEFAULT_KEY_EXPIRES
    login_type: int = 2
    nick: str = ""

    def to_cookie(self) -> str:
        return format_qq_cookie(
            {
                "uin": self.musicid or self.str_musicid,
                "qm_keyst": self.musickey,
                "qqmusic_key": self.musickey,
                "pskey": self.refresh_key,
            }
        )


def _parse_credential(
    data: Any,
    *,
    nick: str = "",
    fallback_uin: str = "",
    fallback_expires: int = DEFAULT_KEY_EXPIRES,
    fallback_login_type: int = 2,
) -> QQMobileCredential:
    """从 ``QQLogin`` / refresh 的响应体里取出凭据。"""
    node = data if isinstance(data, dict) else {}
    musickey = text_of(node.get("musickey"))
    refresh_token = text_of(node.get("refresh_token"))
    if not musickey or not refresh_token:
        raise MusicError("返回的 musickey 或 refresh_token 为空")
    musicid = _music_id_of(node.get("musicid")) or _music_id_of(node.get("str_musicid")) or _music_id_of(fallback_uin)
    expires = _int_of(node.get("keyExpiresIn"), fallback_expires) or fallback_expires
    raw_login_type = node.get("login_type")
    if raw_login_type is None:
        raw_login_type = node.get("loginType")
    if raw_login_type is None:
        raw_login_type = node.get("tmeLoginType")
    login_type = fallback_login_type if raw_login_type in (None, "") else _int_of(raw_login_type, 0)
    return QQMobileCredential(
        openid=text_of(node.get("openid")),
        refresh_token=refresh_token,
        access_token=text_of(node.get("access_token")),
        expired_at=_int_of(node.get("expired_at")),
        musicid=musicid,
        musickey=musickey,
        unionid=text_of(node.get("unionid")),
        str_musicid=_music_id_of(node.get("str_musicid")),
        refresh_key=text_of(node.get("refresh_key")),
        musickey_create_time=_int_of(node.get("musickeyCreateTime"), int(time.time())),
        key_expires_in=expires,
        login_type=login_type,
        nick=nick or text_of(node.get("nick")),
    )


def _merge_refreshed_credential(
    data: Any, previous: QQMobileCredential
) -> QQMobileCredential:
    """Merge a partial key rotation without dropping the current account tokens."""
    if not isinstance(data, dict):
        raise _QQStageError("refresh_response", "QQ 音乐续期返回数据格式不符合预期")
    musickey = text_of(data.get("musickey"))
    if not musickey:
        raise _QQStageError(
            "refresh_response", "QQ 音乐续期未返回新 musickey，请稍后重试", code=0
        )

    incoming_login_type = data.get("login_type")
    if incoming_login_type is None:
        incoming_login_type = data.get("loginType")
    if incoming_login_type is None:
        incoming_login_type = data.get("tmeLoginType")
    if incoming_login_type not in (None, ""):
        parsed_login_type = _int_of(incoming_login_type, 0)
        if parsed_login_type not in (1, 2) or parsed_login_type != previous.login_type:
            raise _QQStageError("refresh_account", "QQ 音乐续期账号信息不一致")

    identity_fields = (
        ("musicid", _music_id_of(data.get("musicid")), _music_id_of(previous.musicid)),
        ("str_musicid", _music_id_of(data.get("str_musicid")), _music_id_of(previous.str_musicid)),
        ("openid", text_of(data.get("openid")), text_of(previous.openid)),
        ("unionid", text_of(data.get("unionid")), text_of(previous.unionid)),
    )
    for _, incoming, existing in identity_fields:
        if incoming and existing and incoming != existing:
            raise _QQStageError("refresh_account", "QQ 音乐续期返回了其他账号的凭据")
    existing_uids = {
        value for value in (_music_id_of(previous.musicid), _music_id_of(previous.str_musicid)) if value
    }
    incoming_uids = {
        value for value in (_music_id_of(data.get("musicid")), _music_id_of(data.get("str_musicid"))) if value
    }
    if existing_uids and any(value not in existing_uids for value in incoming_uids):
        raise _QQStageError("refresh_account", "QQ 音乐续期返回了其他账号的凭据")

    create_time = _int_of(data.get("musickeyCreateTime"), 0)
    if create_time <= 0:
        # A newly rotated key without a timestamp starts its validity window now.
        create_time = int(time.time())
    expires = _int_of(data.get("keyExpiresIn"), 0) or previous.key_expires_in or DEFAULT_KEY_EXPIRES
    return QQMobileCredential(
        openid=text_of(data.get("openid"), previous.openid),
        refresh_token=text_of(data.get("refresh_token"), previous.refresh_token),
        access_token=text_of(data.get("access_token"), previous.access_token),
        expired_at=_int_of(data.get("expired_at"), previous.expired_at),
        musicid=_music_id_of(data.get("musicid")) or previous.musicid,
        musickey=musickey,
        unionid=text_of(data.get("unionid"), previous.unionid),
        str_musicid=_music_id_of(data.get("str_musicid")) or previous.str_musicid,
        refresh_key=text_of(data.get("refresh_key"), previous.refresh_key),
        musickey_create_time=create_time,
        key_expires_in=expires,
        login_type=previous.login_type,
        nick=text_of(data.get("nick"), previous.nick),
    )


def _qr_data_url(png: bytes) -> str:
    if not png:
        return ""
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def _render_qr(text: str) -> str:
    """把 URL 渲染成 PNG data URL；环境缺 qrcode 时返回空串。"""
    if not text:
        return ""
    try:
        import io

        import qrcode  # 延迟导入：登录模块在无二维码依赖时也要能加载

        buffer = io.BytesIO()
        qrcode.make(text).save(buffer, "PNG")
        return _qr_data_url(buffer.getvalue())
    except Exception:  # noqa: BLE001 - 渲染失败只影响展示
        return ""


@dataclass
class _LoginTask:
    task_id: str
    platform: str
    state: str = _WAITING
    message: str = ""
    created_at: float = field(default_factory=time.monotonic)
    updated_at: float = field(default_factory=time.monotonic)
    qr_url: str = ""
    qr_data_url: str = ""
    nickname: str = ""
    cookie_fingerprint: str = ""
    generation: int = 0
    secret: dict[str, str] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class LoginManager:
    """音乐扫码登录 + QQ 音乐续期。

    Args:
        data_dir: QQ 长期凭据的私有目录（构造时不创建、不读写）。
        get_cookie: ``get_cookie(platform) -> str``，可为协程。
        save_cookie: ``save_cookie(platform, cookie)``，可为协程。
        client_options: ``client_options(platform) -> {proxy, verify_ssl, ...}``。
    """

    def __init__(
        self,
        data_dir: Path,
        get_cookie: Callable[[str], Any],
        save_cookie: Callable[[str, str], Any],
        client_options: Callable[[str], dict[str, Any]] | None = None,
        *,
        timeout: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._data_dir = Path(data_dir)
        self._get_cookie = get_cookie
        self._save_cookie = save_cookie
        self._client_options = client_options
        self._timeout = timeout
        self._transport = transport
        self._tasks: dict[str, _LoginTask] = {}
        self._registry_lock = asyncio.Lock()
        self._commit_locks = {platform: asyncio.Lock() for platform in PLATFORMS}
        self._generations = {platform: 0 for platform in PLATFORMS}
        self._refresh_locks: dict[str, asyncio.Lock] = {}
        self._refresh_failures: dict[str, float] = {}
        self._last_refresh_at: dict[str, float] = {}
        self._closed = False

    # -- 基础设施 -----------------------------------------------------------
    def _ensure_open(self) -> None:
        if self._closed:
            raise MusicError("音乐登录已关闭，请重新进入插件后再试")

    def _options(self, platform: str) -> dict[str, Any]:
        options: dict[str, Any] = {}
        if callable(self._client_options):
            try:
                raw = self._client_options(platform)
            except Exception as exc:  # noqa: BLE001
                raise fetch_error(exc) from None
            if isinstance(raw, dict):
                options = dict(raw)
        proxy = text_of(options.get("proxy")) or None
        verify = options.get("verify_ssl", True)
        timeout = options.get("timeout", self._timeout)
        transport = options.get("transport", self._transport)
        return {
            "proxy": proxy,
            "verify_ssl": bool(verify),
            "timeout": timeout if isinstance(timeout, (int, float)) and timeout > 0 else self._timeout,
            "transport": transport,
        }

    def _client(self, platform: str, *, follow_redirects: bool = False):
        opts = self._options(platform)
        return open_client(
            proxy=opts["proxy"],
            verify_ssl=opts["verify_ssl"],
            timeout=opts["timeout"],
            transport=opts["transport"],
            follow_redirects=follow_redirects,
        )

    async def _current_cookie(self, platform: str) -> str:
        try:
            value = self._get_cookie(platform)
            if inspect.isawaitable(value):
                value = await value
        except MusicError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise fetch_error(exc) from None
        return str(value or "").strip()

    async def _persist_cookie(
        self,
        platform: str,
        cookie: str,
        expected_fp: str,
        generation: int,
        *,
        qq_credential: QQMobileCredential | None = None,
    ) -> None:
        """提交 Cookie 与其 QQ refresh 状态，和 clear/start 串行化。"""
        async with self._commit_locks[platform]:
            if self._closed or generation != self._generations[platform]:
                raise MusicError("登录会话已取消，较新的登录操作优先")
            current_fp = _fingerprint(await self._current_cookie(platform))
            # 空 Cookie 也是一个需要匹配的快照：从未登录状态开始的旧二维码
            # 不能覆盖期间导入的新登录态。
            if self._closed or generation != self._generations[platform]:
                raise MusicError("登录会话已取消，较新的登录操作优先")
            if current_fp != expected_fp:
                raise MusicError(
                    "登录期间该平台已更新过 Cookie，本次登录结果未覆盖较新的登录态；"
                    "如需改用它，请重新获取二维码"
                )
            try:
                result = self._save_cookie(platform, cookie)
                if inspect.isawaitable(result):
                    await result
                if self._closed or generation != self._generations[platform]:
                    raise MusicError("登录会话已取消，较新的登录操作优先")
                if qq_credential is not None:
                    await asyncio.to_thread(self._store_qq_state, cookie, qq_credential)
            except MusicError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise fetch_error(exc) from None

    async def _advance_generation(self, platform: str, message: str) -> int:
        """开始一代新操作；调用方不持锁做后续网络请求。"""
        self._ensure_open()
        # Increment before the first await so a concurrently completing network
        # request observes the cancellation even while registry cleanup is queued.
        self._generations[platform] += 1
        generation = self._generations[platform]
        async with self._registry_lock:
            if self._closed or generation != self._generations[platform]:
                raise MusicError("登录操作已被更新的请求取消")
            self._cancel_platform_locked(platform, message)
            self._prune_locked()
            self._cleanup_tasks_locked()
        return generation

    async def _generation_is_current(self, platform: str, generation: int) -> bool:
        async with self._registry_lock:
            return not self._closed and generation == self._generations[platform]

    # -- 任务登记 -----------------------------------------------------------
    def _prune_locked(self) -> None:
        now = time.monotonic()
        for task_id, task in list(self._tasks.items()):
            if now - task.created_at <= TASK_TTL_SEC:
                continue
            if task.state in (_WAITING, _SCANNED):
                task.state = _EXPIRED
                task.message = "登录会话已过期，请重新获取二维码"
            task.secret.clear()
            self._tasks.pop(task_id, None)

    def _cancel_platform_locked(self, platform: str, message: str = "登录会话已被新的请求替代") -> None:
        for task in self._tasks.values():
            if task.platform != platform or task.state not in (_WAITING, _SCANNED):
                continue
            task.state = _EXPIRED
            task.message = message
            task.secret.clear()
            task.qr_data_url = ""

    def _public(self, task: _LoginTask, *, with_qr: bool = True) -> dict[str, str]:
        payload = {
            "task_id": task.task_id,
            "state": task.state,
            "message": task.message,
        }
        if with_qr and task.qr_data_url and task.state in (_WAITING, _SCANNED):
            payload["qr_data_url"] = task.qr_data_url
        if task.nickname:
            payload["nickname"] = task.nickname
        return payload

    def _new_task(self, platform: str, fingerprint: str, generation: int = 0) -> _LoginTask:
        task = _LoginTask(
            task_id=secrets.token_hex(TASK_ID_LEN // 2),
            platform=platform,
            cookie_fingerprint=fingerprint,
            generation=generation,
        )
        self._tasks[task.task_id] = task
        return task

    def _cleanup_tasks_locked(self) -> None:
        finished = [
            (task.updated_at, task_id)
            for task_id, task in self._tasks.items()
            if task.state in (_SUCCESS, _EXPIRED, _FAILED)
        ]
        if len(self._tasks) < MAX_TASKS:
            return
        for _, task_id in sorted(finished)[: max(1, len(self._tasks) - MAX_TASKS + 1)]:
            self._tasks.pop(task_id, None)

    # -- 入口 ---------------------------------------------------------------
    async def start(self, platform: str) -> dict[str, str]:
        self._ensure_open()
        key = self._normalize_platform(platform)
        if key not in QR_PLATFORMS:
            raise MusicError("网易云音乐已不支持扫码登录，请改用 Cookie 导入")
        generation = await self._advance_generation(key, "登录会话已被新的请求替代")
        cookie = await self._current_cookie(key)
        fingerprint = _fingerprint(cookie)
        if not await self._generation_is_current(key, generation):
            raise MusicError("登录操作已被更新的请求取消")
        try:
            if key == "netease":
                task = await self._start_netease(fingerprint, generation)
            else:
                task = await self._start_qq(fingerprint, generation)
        except MusicError as exc:
            diagnostic = exc if isinstance(exc, _LoginStageError) else _LoginStageError("start", str(exc))
            _log_login_failure(key, diagnostic)
            raise
        except Exception as exc:  # noqa: BLE001 - unknown failures stay redacted
            safe_error = fetch_error(exc)
            _log_login_failure(key, _LoginStageError("start", str(safe_error)))
            raise safe_error from None
        return self._public(task)

    async def poll(self, task_id: str) -> dict[str, str]:
        self._ensure_open()
        value = str(task_id or "").strip()
        if not value or len(value) > 100:
            raise MusicError("扫码会话无效，请重新获取二维码")
        async with self._registry_lock:
            self._prune_locked()
            task = self._tasks.get(value)
            if task is None:
                raise MusicError("扫码会话不存在或已过期，请重新获取二维码")
        async with task.lock:
            if task.state in (_SUCCESS, _EXPIRED, _FAILED):
                return self._public(task)
            if not await self._generation_is_current(task.platform, task.generation):
                task.state = _EXPIRED
                task.message = "登录会话已取消"
                task.secret.clear()
                task.qr_data_url = ""
                return self._public(task)
            if task.state == _WAITING or task.state == _SCANNED:
                if time.monotonic() - task.created_at > TASK_TTL_SEC:
                    task.state = _EXPIRED
                    task.message = "登录会话已过期，请重新获取二维码"
                    task.secret.clear()
                    return self._public(task)
            try:
                if task.platform == "netease":
                    await self._poll_netease(task)
                else:
                    await self._poll_qq(task)
            except MusicError as exc:
                if self._closed or task.generation != self._generations[task.platform]:
                    task.state = _EXPIRED
                    task.message = "登录会话已取消"
                else:
                    task.state = _FAILED
                    task.message = self._with_hint(str(exc))
                    diagnostic = exc if isinstance(exc, _LoginStageError) else _LoginStageError("poll", str(exc))
                    _log_login_failure(task.platform, diagnostic)
                task.secret.clear()
            except Exception as exc:  # noqa: BLE001
                if self._closed or task.generation != self._generations[task.platform]:
                    task.state = _EXPIRED
                    task.message = "登录会话已取消"
                else:
                    task.state = _FAILED
                    task.message = self._with_hint(str(fetch_error(exc)))
                    _log_login_failure(task.platform, exc)
                task.secret.clear()
            if self._closed or task.generation != self._generations[task.platform]:
                task.state = _EXPIRED
                task.message = "登录会话已取消"
                task.secret.clear()
                task.qr_data_url = ""
            task.updated_at = time.monotonic()
            return self._public(task)

    async def refresh(self, platform: str) -> dict[str, str]:
        self._ensure_open()
        key = self._normalize_platform(platform)
        if key == "netease":
            return {
                "state": _SUCCESS,
                "message": "网易云 MUSIC_U 为长期凭据，无需自动续期",
            }
        return await self._refresh_qq(force=True)

    async def ensure_fresh(self, platform: str) -> dict[str, str]:
        """在搜索/取流前调用：只有接近过期才会真的发请求。"""
        self._ensure_open()
        key = self._normalize_platform(platform)
        if key == "netease":
            return {"state": _SUCCESS, "message": "网易云登录态无需自动续期"}
        return await self._refresh_qq(force=False)

    async def clear(self, platform: str) -> dict[str, str]:
        self._ensure_open()
        key = self._normalize_platform(platform)
        # Invalidate immediately, before waiting for an in-progress short commit.
        self._generations[key] += 1
        async with self._commit_locks[key]:
            if self._closed:
                raise MusicError("音乐登录已关闭，请重新进入插件后再试")
            async with self._registry_lock:
                self._cancel_platform_locked(key, "登录会话已取消")
                # These maps are keyed by cookie fingerprint, not platform.
                self._refresh_failures.clear()
                self._last_refresh_at.clear()
            if key == "qqmusic":
                await asyncio.to_thread(self._drop_qq_state)
                return {"state": _SUCCESS, "message": "已清理 QQ 音乐登录态与续期凭据"}
            return {"state": _SUCCESS, "message": "已清理网易云登录会话"}

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for platform in PLATFORMS:
            self._generations[platform] += 1
        acquired: list[asyncio.Lock] = []
        try:
            # Wait only for config/file commit callbacks; QR/refresh network calls
            # never hold these locks and will fail their generation check later.
            for platform in sorted(PLATFORMS):
                lock = self._commit_locks[platform]
                await lock.acquire()
                acquired.append(lock)
            async with self._registry_lock:
                for task in self._tasks.values():
                    task.state = _EXPIRED
                    task.message = "音乐登录已关闭"
                    task.secret.clear()
                    task.qr_data_url = ""
                self._tasks.clear()
                self._refresh_failures.clear()
                self._last_refresh_at.clear()
        finally:
            for lock in reversed(acquired):
                lock.release()

    # -- 网易云 -------------------------------------------------------------
    async def _start_netease(self, fingerprint: str, generation: int) -> _LoginTask:
        async with self._client("netease") as client:
            try:
                response = await client.post(
                    _NETEASE_BASE + _NETEASE_UNIKEY,
                    headers=self._netease_headers(),
                    data=weapi_form({"type": 1}),
                )
            except Exception as exc:  # noqa: BLE001
                raise fetch_error(exc) from None
        if response.status_code >= 400:
            raise _LoginStageError(
                "unikey_http", "网易云二维码服务暂时不可用，请稍后重试或改用 Cookie 导入",
                code=response.status_code,
            )
        try:
            body = response.json()
        except ValueError:
            raise _LoginStageError("unikey_response", "网易云二维码响应无法解析，请改用 Cookie 导入") from None
        node = body if isinstance(body, dict) else {}
        unikey = text_of(node.get("unikey"))
        if not unikey:
            code = _safe_business_code(node.get("code"))
            if code in (-462, -460, 8810, 8821):
                detail = f"网易云登录被风控拦截（错误码 {code}），请稍后重试或更换网络"
            elif code is not None:
                detail = f"获取网易云二维码密钥失败（错误码 {code}）"
            else:
                detail = "获取网易云二维码密钥失败"
            raise _LoginStageError("unikey_response", f"{detail}，请改用 Cookie 导入", code=code)
        qr_url = f"{_NETEASE_QR_PREFIX}{unikey}"
        data_url = _render_qr(qr_url)
        if not data_url:
            raise _LoginStageError("qrcode_render", "二维码图片生成失败，请改用 Cookie 导入")
        async with self._registry_lock:
            if self._closed or generation != self._generations["netease"]:
                raise MusicError("登录会话已取消，请重新获取二维码")
            self._prune_locked()
            self._cleanup_tasks_locked()
            task = self._new_task("netease", fingerprint, generation)
            task.qr_url = qr_url
            task.qr_data_url = data_url
            task.message = "请使用网易云音乐 App 扫码登录"
            task.secret["unikey"] = unikey
        return task

    async def _poll_netease(self, task: _LoginTask) -> None:
        unikey = task.secret.get("unikey", "")
        if not unikey:
            raise _LoginStageError("poll_request", "二维码已失效，请重新获取")
        async with self._client("netease") as client:
            try:
                response = await client.post(
                    _NETEASE_BASE + _NETEASE_CHECK,
                    headers=self._netease_headers(),
                    data=weapi_form({"key": unikey, "type": 1}),
                )
            except Exception as exc:  # noqa: BLE001
                raise fetch_error(exc) from None
        if response.status_code >= 400:
            raise _LoginStageError(
                "poll_http", "网易云扫码状态查询失败，请稍后重试", code=response.status_code
            )
        try:
            body = response.json()
        except ValueError:
            raise _LoginStageError("poll_response", "网易云扫码状态无法解析，请重新获取二维码") from None
        node = body if isinstance(body, dict) else {}
        code = _safe_business_code(node.get("code"))
        if code == 800:
            task.state = _EXPIRED
            task.message = "二维码已过期，请重新发起登录"
            task.secret.clear()
            return
        if code == 801:
            task.state = _WAITING
            task.message = "等待扫码中..."
            return
        if code == 802:
            task.state = _SCANNED
            task.nickname = text_of(node.get("nickname"), task.nickname)
            task.message = "已扫码，请在手机上确认登录"
            return
        if code == 803:
            music_u = self._extract_music_u(node, response)
            if not music_u:
                raise _LoginStageError(
                    "poll_credential", "网易云登录成功但未返回 MUSIC_U，请改用 Cookie 导入", code=code
                )
            cookie = f"MUSIC_U={music_u}"
            try:
                await self._persist_cookie(
                    "netease", cookie, task.cookie_fingerprint, task.generation
                )
            except MusicError as exc:
                raise _LoginStageError("persist", str(exc)) from None
            task.state = _SUCCESS
            task.nickname = text_of(node.get("nickname"), task.nickname)
            task.message = "网易云音乐登录成功"
            task.secret.clear()
            return
        detail = f"网易云扫码未完成（错误码 {code}）" if code is not None else "网易云扫码响应无法确认"
        raise _LoginStageError("poll_response", detail, code=code)

    @staticmethod
    def _netease_headers() -> dict[str, str]:
        return {"Referer": _NETEASE_BASE, "User-Agent": USER_AGENT}

    @staticmethod
    def _extract_music_u(body: dict[str, Any], response: httpx.Response) -> str:
        music_u = music_u_of(text_of(body.get("cookie")))
        if music_u:
            return music_u
        for header in response.headers.get_list("set-cookie"):
            for part in header.split(";"):
                key, _, value = part.partition("=")
                if key.strip() == "MUSIC_U" and value.strip():
                    return value.strip()
        return ""

    # -- QQ 音乐：扫码 ------------------------------------------------------
    async def _start_qq(self, fingerprint: str, generation: int) -> _LoginTask:
        params = dict(_QQ_QR_PARAMS)
        params["t"] = str(int(time.time() * 1000))
        headers = dict(_QQ_HEADERS)
        headers["Referer"] = "https://xui.ptlogin2.qq.com/"
        async with self._client("qqmusic") as client:
            try:
                response = await client.get(_QQ_QR_URL, params=params, headers=headers)
            except Exception as exc:  # noqa: BLE001
                raise fetch_error(exc) from None
            qrsig = client.cookies.get("qrsig") or self._cookie_from_response(response, "qrsig")
            content = response.content
        if response.status_code >= 400 or not qrsig or not content:
            raise MusicError(f"获取 QQ 登录二维码失败，{_COOKIE_HINT}")
        data_url = _qr_data_url(content)
        if not data_url:
            raise MusicError(f"QQ 登录二维码图片无效，{_COOKIE_HINT}")
        async with self._registry_lock:
            if self._closed or generation != self._generations["qqmusic"]:
                raise MusicError("登录会话已取消，请重新获取二维码")
            self._prune_locked()
            self._cleanup_tasks_locked()
            task = self._new_task("qqmusic", fingerprint, generation)
            task.qr_data_url = data_url
            task.qr_url = "https://ssl.ptlogin2.qq.com/ptqrshow"
            task.message = "请使用手机 QQ 扫码登录"
            task.secret["qrsig"] = qrsig
        return task

    async def _poll_qq(self, task: _LoginTask) -> None:
        qrsig = task.secret.get("qrsig", "")
        if not qrsig:
            raise _QQStageError("qrcode", "二维码已失效，请重新获取")
        params = {
            "u1": "https://graph.qq.com/oauth2.0/login_jump",
            "ptqrtoken": str(hash33(qrsig)),
            "ptredirect": "0",
            "h": "1",
            "t": "1",
            "g": "1",
            "from_ui": "1",
            "ptlang": "2052",
            "action": f"0-0-{int(time.time() * 1000)}",
            "js_ver": "20102616",
            "js_type": "1",
            "pt_uistyle": "40",
            "aid": "716027609",
            "daid": "383",
            "pt_3rd_aid": "100497308",
            "has_onekey": "1",
        }
        headers = dict(_QQ_HEADERS)
        headers["Referer"] = "https://xui.ptlogin2.qq.com/"
        async with self._client("qqmusic") as client:
            try:
                # ptqrlogin 认 qrsig 这个 Cookie，而不是查询参数；新 client 必须补上。
                client.cookies.set("qrsig", qrsig)
                response = await client.get(_QQ_POLL_URL, params=params, headers=headers)
            except Exception as exc:  # noqa: BLE001
                raise _QQStageError("poll", str(fetch_error(exc))) from None
            text = response.text or ""
            if response.status_code >= 400:
                raise _QQStageError(
                    "poll",
                    f"QQ 扫码状态查询失败（HTTP {response.status_code}），请稍后重试",
                    code=response.status_code,
                )
            parts = self._parse_ptui_cb(text)
            if not parts:
                raise _QQStageError("poll", f"QQ 扫码状态无法解析，{_COOKIE_HINT}")
            code = _int_of(parts[0], -1)
            if code == 66:
                task.state = _WAITING
                task.message = "等待手机 QQ 扫码"
                return
            if code == 67:
                task.state = _SCANNED
                task.nickname = text_of(parts[5], task.nickname)
                task.message = "已扫码，请在手机 QQ 点击确认登录"
                return
            if code == 65:
                task.state = _EXPIRED
                task.message = "二维码已过期，请重新发起登录"
                task.secret.clear()
                return
            if code != 0:
                task.state = _FAILED
                task.message = self._with_hint("QQ 扫码未完成")
                task.secret.clear()
                return
            sig_url = text_of(parts[2])
            nick = text_of(parts[5], DEFAULT_QQ_NICK)
            if not sig_url:
                raise _QQStageError("poll", f"QQ 登录未返回跳转地址，{_COOKIE_HINT}")
            credential = await self._exchange_mobile_credential(client, sig_url, nick)
        cookie = credential.to_cookie()
        if not cookie:
            raise _QQStageError("credential", f"QQ 音乐凭据不完整，{_COOKIE_HINT}")
        try:
            await self._persist_cookie(
                "qqmusic",
                cookie,
                task.cookie_fingerprint,
                task.generation,
                qq_credential=credential,
            )
        except MusicError as exc:
            # 本地保存失败与上游拒绝必须能在后台日志里区分开。
            raise _QQStageError("persist", str(exc)) from None
        task.state = _SUCCESS
        task.nickname = credential.nick or nick
        task.message = "登录成功，已绑定移动端凭据并启用自动保活"
        task.secret.clear()
        self._refresh_failures.pop(_fingerprint(cookie), None)

    async def _exchange_mobile_credential(
        self, client: httpx.AsyncClient, sig_url: str, nick: str
    ) -> QQMobileCredential:
        """逐层换取 QQ 音乐移动端凭据：check_sig → authorize → QQLogin。"""
        try:
            query = parse_qs(urlsplit(sig_url).query)
        except ValueError:
            raise _QQStageError("sig", f"QQ 登录跳转地址无效，{_COOKIE_HINT}") from None
        uin = text_of((query.get("uin") or [""])[0])
        ptsigx = text_of((query.get("ptsigx") or [""])[0])
        if not uin or not ptsigx:
            raise _QQStageError(
                "sig", f"QQ 登录返回中缺少 uin 或 ptsigx 参数，{_COOKIE_HINT}"
            )
        check_params = {
            "uin": uin,
            "pttype": "1",
            "service": "ptqrlogin",
            "nodirect": "0",
            "ptsigx": ptsigx,
            "s_url": "https://graph.qq.com/oauth2.0/login_jump",
            "ptlang": "2052",
            "ptredirect": "100",
            "aid": "716027609",
            "daid": "383",
            "j_later": "0",
            "low_login_hour": "0",
            "regmaster": "0",
            "pt_login_type": "3",
            "pt_aid": "0",
            "pt_aaid": "16",
            "pt_light": "0",
            "pt_3rd_aid": "100497308",
        }
        check_headers = {**_QQ_HEADERS, "Referer": _QQ_LOGIN_REFERER}
        try:
            sig_response = await client.get(
                _QQ_CHECK_SIG_URL, params=check_params, headers=check_headers
            )
        except httpx.HTTPError:
            raise _QQStageError(
                "check_sig", f"QQ 登录鉴权阶段网络异常，{_COOKIE_HINT}"
            ) from None
        if sig_response.status_code >= 400:
            raise _QQStageError(
                "check_sig",
                f"QQ 登录鉴权被服务端拒绝（HTTP {sig_response.status_code}），{_COOKIE_HINT}",
                code=sig_response.status_code,
            )
        # 优先使用本次 check_sig 响应下发的票据：CookieJar 里可能同时存在
        # 多个域的 p_skey，直接 get 会抛 CookieConflict。
        signal_cookies = self._response_cookies(sig_response)
        p_skey = text_of(signal_cookies.get("p_skey")) or self._cookie_from_jar(
            client.cookies, "p_skey"
        )
        if not p_skey:
            raise _QQStageError(
                "check_sig", f"QQ 登录未获取到 p_skey 鉴权票据，请重新扫码，{_COOKIE_HINT}"
            )
        # 把响应票据落到 .qq.com，authorize 域名才一定收得到；同名 host-only
        # Cookie 不会被发往 graph.qq.com，也不会造成多域取值冲突。
        for ticket_name, ticket_value in signal_cookies.items():
            if ticket_name and ticket_value:
                self._set_domain_cookie(client.cookies, ticket_name, ticket_value)
        if not signal_cookies.get("p_skey"):
            self._set_domain_cookie(client.cookies, "p_skey", p_skey)
        p_uin = text_of(signal_cookies.get("p_uin")) or self._cookie_from_jar(
            client.cookies, "p_uin"
        )
        if p_uin:
            self._set_domain_cookie(client.cookies, "p_uin", p_uin)
        authorize_data = {
            "response_type": "code",
            "client_id": "100497308",
            "redirect_uri": _QQ_REDIRECT_URI,
            "scope": "get_user_info,get_app_friends",
            "state": "state",
            "switch": "",
            "from_ptlogin": "1",
            "src": "1",
            "update_auth": "1",
            "openapi": "1010_1030",
            "g_tk": str(hash33(p_skey, 5381)),
            "auth_time": str(int(time.time() * 1000)),
            "ui": secrets.token_hex(16),
        }
        try:
            # 与参考实现一致，通过表单提交授权参数。
            response = await client.post(
                _QQ_AUTHORIZE_URL, data=authorize_data, headers=dict(_QQ_HEADERS)
            )
        except httpx.HTTPError:
            raise _QQStageError(
                "authorize", f"QQ 登录 OAuth 授权阶段网络异常，{_COOKIE_HINT}"
            ) from None
        if response.status_code >= 400:
            raise _QQStageError(
                "authorize",
                f"QQ 登录 OAuth 授权被服务端拒绝（HTTP {response.status_code}），{_COOKIE_HINT}",
                code=response.status_code,
            )
        code = self._extract_oauth_code(response)
        if not code:
            raise _QQStageError(
                "authorize", f"QQ 登录 OAuth 授权未返回授权码，请重新扫码，{_COOKIE_HINT}"
            )
        payload = {
            "comm": {"tmeLoginType": 2},
            "req": {
                "module": "QQConnectLogin.LoginServer",
                "method": "QQLogin",
                "param": {"code": code},
            },
        }
        try:
            body = await client.post(_MUSICU_URL, headers=dict(_QQ_HEADERS), json=payload)
        except httpx.HTTPError:
            raise _QQStageError(
                "qqlogin", f"QQ 音乐登录服务网络异常，{_COOKIE_HINT}"
            ) from None
        if body.status_code >= 400:
            raise _QQStageError(
                "qqlogin",
                f"QQ 音乐登录服务暂时不可用（HTTP {body.status_code}），{_COOKIE_HINT}",
                code=body.status_code,
            )
        try:
            data = body.json()
        except ValueError:
            raise _QQStageError(
                "qqlogin", f"QQ 音乐登录返回数据无法解析，{_COOKIE_HINT}"
            ) from None
        req = data.get("req") if isinstance(data, dict) else None
        if not isinstance(req, dict):
            raise _QQStageError(
                "qqlogin", f"QQ 音乐登录响应格式不符合预期，{_COOKIE_HINT}"
            )
        req_code = _int_of(req.get("code"), -1)
        if req_code != 0:
            raise _QQStageError(
                "qqlogin",
                self._qq_login_rejection(req_code),
                code=req_code if req_code >= 0 else None,
            )
        credential = req.get("data")
        if not isinstance(credential, dict) or not credential:
            raise _QQStageError(
                "qqlogin", f"QQ 音乐登录未返回凭据数据，{_COOKIE_HINT}"
            )
        try:
            return _parse_credential(credential, nick=nick, fallback_uin=uin)
        except MusicError:
            raise _QQStageError(
                "credential", f"QQ 音乐登录凭据不完整，请重新扫码，{_COOKIE_HINT}"
            ) from None

    @staticmethod
    def _extract_oauth_code(response: httpx.Response) -> str:
        location = response.headers.get("location", "") or ""
        code = LoginManager._code_from_url(location)
        if code:
            return code
        try:
            body = response.text or ""
        except Exception:  # noqa: BLE001
            return ""
        match = re.search(r"[?&]code=([A-Za-z0-9_-]+)", body)
        return match.group(1) if match else ""

    @staticmethod
    def _code_from_url(url: str) -> str:
        if not url:
            return ""
        try:
            query = parse_qs(urlsplit(url).query)
        except ValueError:
            return ""
        return text_of((query.get("code") or [""])[0])

    @staticmethod
    def _parse_ptui_cb(text: str) -> list[str]:
        match = re.search(r"ptuiCB\((.*?)\)", text or "", re.S)
        if not match:
            return []
        parts = []
        for raw in match.group(1).split(","):
            parts.append(raw.strip().strip("'\"").strip())
        return parts

    @staticmethod
    def _response_cookies(response: httpx.Response) -> dict[str, str]:
        """把本次响应的 Set-Cookie 收成 name→value，不经过多域 CookieJar。"""
        values: dict[str, str] = {}
        for header in response.headers.get_list("set-cookie"):
            pair = header.split(";", 1)[0]
            key, sep, value = pair.partition("=")
            key = key.strip()
            value = value.strip()
            if sep and key and value:
                values.setdefault(key, value)
        return values

    @staticmethod
    def _set_domain_cookie(cookies: httpx.Cookies, name: str, value: str) -> None:
        """把票据统一落到 ``.qq.com``，并清掉同名旧域值，避免重复 Cookie。"""
        jar = getattr(cookies, "jar", None)
        if jar is not None:
            for cookie in [cookie for cookie in jar if cookie.name == name]:
                jar.clear(cookie.domain, cookie.path, cookie.name)
        cookies.set(name, value, domain=".qq.com")

    @staticmethod
    def _cookie_from_jar(cookies: httpx.Cookies, name: str) -> str:
        """遍历 CookieJar 取票据，绕开 ``Cookies.get`` 的同名多域冲突。"""
        jar = getattr(cookies, "jar", None)
        if jar is None:
            try:
                return text_of(cookies.get(name))
            except Exception:  # noqa: BLE001 - 冲突时按缺失处理
                return ""
        found: list[tuple[str, str]] = []
        try:
            for cookie in jar:
                if cookie.name != name:
                    continue
                value = text_of(cookie.value)
                if value:
                    found.append((str(cookie.domain or "").lstrip("."), value))
        except Exception:  # noqa: BLE001 - 坏 Cookie 不能中断登录
            return ""
        if not found:
            return ""
        for wanted in (
            "ssl.ptlogin2.graph.qq.com",
            "graph.qq.com",
            "ptlogin2.qq.com",
            "qq.com",
        ):
            for domain, value in found:
                if domain == wanted or domain.endswith("." + wanted):
                    return value
        return found[0][1]

    @staticmethod
    def _qq_login_rejection(code: int) -> str:
        """把已知上游整数码翻成安全文案，保留码值以区分服务端拒绝。"""
        if code < 0:
            return f"QQ 音乐登录未收到服务端结果，请稍后重试，{_COOKIE_HINT}"
        known: dict[int, str] = {
            1000: "QQ 音乐登录授权已失效，请重新扫码登录",
            104400: "QQ 音乐登录授权已失效，请重新扫码登录",
            104401: "QQ 音乐登录授权已失效，请重新扫码登录",
            20261: "QQ 音乐登录参数错误，请重新扫码重试",
            20271: "QQ 音乐登录验证码错误，请重新扫码重试",
            20272: "QQ 音乐登录账号绑定异常，请稍后重试",
            20274: "QQ 音乐登录缺少账号绑定，请稍后重试",
            20277: "QQ 音乐登录账号受限，请稍后重试",
            20278: "QQ 音乐登录账号受限，请稍后重试",
            20279: "QQ 音乐登录失败：设备登录数已达上限，请先在 QQ音乐客户端的设备管理中清理旧设备后重试",
            20450: "QQ 音乐登录失败：账号已被封禁",
            104604: "QQ 音乐登录失败：操作过于频繁，请稍后重试",
        }
        base = known.get(code)
        if base:
            return f"{base}（{code}），{_COOKIE_HINT}"
        return f"QQ 音乐登录被服务端拒绝（错误码 {code}），{_COOKIE_HINT}"

    @staticmethod
    def _cookie_from_response(response: httpx.Response, name: str) -> str:
        for header in response.headers.get_list("set-cookie"):
            for part in header.split(";"):
                key, _, value = part.partition("=")
                if key.strip() == name and value.strip():
                    return value.strip()
        return ""

    # -- QQ 音乐：续期 ------------------------------------------------------
    async def _refresh_qq(self, *, force: bool) -> dict[str, str]:
        self._ensure_open()
        lock = self._refresh_locks.setdefault("qqmusic", asyncio.Lock())
        async with lock:
            # Read the current token only after acquiring the lock: another
            # request may already have rotated it while this call was waiting.
            return await self._refresh_qq_locked(force=force)

    async def _refresh_qq_locked(self, *, force: bool) -> dict[str, str]:
        self._ensure_open()
        generation = self._generations["qqmusic"]
        cookie = await self._current_cookie("qqmusic")
        fingerprint = _fingerprint(cookie)
        if self._closed or generation != self._generations["qqmusic"]:
            return self._refresh_cancelled(force)
        if not cookie:
            if force:
                return {"state": _FAILED, "message": "没有 QQ 音乐登录态，请先扫码或导入 Cookie"}
            return {"state": _SUCCESS, "message": "没有 QQ 音乐登录态，已跳过自动续期"}
        state = await asyncio.to_thread(self._read_qq_state, fingerprint)
        if self._closed or generation != self._generations["qqmusic"]:
            return self._refresh_cancelled(force)
        if state is None:
            if force:
                return {"state": _FAILED, "message": "没有可续期的 QQ 音乐凭据，请重新扫码登录"}
            return {"state": _SUCCESS, "message": "没有可续期的 QQ 音乐凭据，已跳过自动续期"}
        if not force and not self._needs_refresh(state.credential):
            return {"state": _SUCCESS, "message": "QQ 音乐登录态仍然有效"}
        failed_at = self._refresh_failures.get(fingerprint, 0.0)
        if not force and failed_at and time.monotonic() - failed_at < REFRESH_FAIL_COOLDOWN_SEC:
            return {"state": _FAILED, "message": "QQ 音乐续期刚刚失败，请稍后再试"}
        last = self._last_refresh_at.get(fingerprint, 0.0)
        if force and last and time.monotonic() - last < 5.0:
            return {"state": _SUCCESS, "message": "QQ 音乐登录态刚刚已续期"}
        if self._closed or generation != self._generations["qqmusic"]:
            return self._refresh_cancelled(force)
        try:
            credential = await self._request_refresh(state.credential)
        except MusicError as exc:
            if self._closed or generation != self._generations["qqmusic"]:
                return self._refresh_cancelled(force)
            self._refresh_failures[fingerprint] = time.monotonic()
            diagnostic = exc if isinstance(exc, _LoginStageError) else _QQStageError("refresh", str(exc))
            _log_login_failure("qqmusic", diagnostic)
            return {"state": _FAILED, "message": self._with_hint(str(exc))}
        except Exception as exc:  # noqa: BLE001
            if self._closed or generation != self._generations["qqmusic"]:
                return self._refresh_cancelled(force)
            self._refresh_failures[fingerprint] = time.monotonic()
            _log_login_failure("qqmusic", _QQStageError("refresh_request", str(fetch_error(exc))))
            return {"state": _FAILED, "message": self._with_hint(str(fetch_error(exc)))}
        if self._closed or generation != self._generations["qqmusic"]:
            return self._refresh_cancelled(force)
        new_cookie = credential.to_cookie()
        if not new_cookie:
            self._refresh_failures[fingerprint] = time.monotonic()
            failure = _QQStageError("refresh_credential", "QQ 音乐续期未返回可用凭据")
            _log_login_failure("qqmusic", failure)
            return {"state": _FAILED, "message": self._with_hint(str(failure))}
        try:
            await self._persist_cookie(
                "qqmusic",
                new_cookie,
                fingerprint,
                generation,
                qq_credential=credential,
            )
        except MusicError as exc:
            if self._closed or generation != self._generations["qqmusic"]:
                return self._refresh_cancelled(force)
            self._refresh_failures[fingerprint] = time.monotonic()
            diagnostic = exc if isinstance(exc, _LoginStageError) else _QQStageError("refresh_persist", str(exc))
            _log_login_failure("qqmusic", diagnostic)
            return {"state": _FAILED, "message": str(exc)}
        self._refresh_failures.pop(fingerprint, None)
        self._last_refresh_at = {_fingerprint(new_cookie): time.monotonic()}
        return {"state": _SUCCESS, "message": "QQ 音乐登录态已自动续期"}

    @staticmethod
    def _refresh_cancelled(force: bool) -> dict[str, str]:
        if force:
            return {"state": _FAILED, "message": "QQ 音乐续期已取消，请重新扫码登录"}
        return {"state": _SUCCESS, "message": "登录态已变化，已跳过自动续期"}

    async def _request_refresh(self, credential: QQMobileCredential) -> QQMobileCredential:
        """按 MusicUID 的 refresh 请求体重换一次 musickey。"""
        login_type = credential.login_type
        if login_type not in (1, 2):
            raise _QQStageError("refresh_credentials", "QQ 音乐续期凭据的登录类型无法识别")
        if not credential.refresh_token or not credential.musickey:
            raise _QQStageError("refresh_credentials", "QQ 音乐续期凭据不完整，请重新扫码登录")
        if login_type == 2:
            musicid = _music_id_of(credential.musicid) or _music_id_of(credential.str_musicid)
            if not musicid:
                raise _QQStageError("refresh_credentials", "QQ 音乐续期凭据不完整，请重新扫码登录")
            param = {
                "openid": credential.openid,
                "access_token": credential.access_token,
                "refresh_token": credential.refresh_token,
                "expired_in": credential.expired_at,
                "musicid": int(musicid),
                "musickey": credential.musickey,
                "refresh_key": credential.refresh_key,
                "loginMode": 2,
            }
        else:
            str_musicid = _music_id_of(credential.str_musicid) or _music_id_of(credential.musicid)
            if not str_musicid:
                raise _QQStageError("refresh_credentials", "QQ 音乐续期凭据不完整，请重新扫码登录")
            param = {
                "openid": credential.openid,
                "refresh_token": credential.refresh_token,
                "str_musicid": str_musicid,
                "musickey": credential.musickey,
                "unionid": credential.unionid,
                "refresh_key": credential.refresh_key,
                "loginMode": 2,
            }
        payload = {
            "comm": {"ct": 11, "cv": "12080008", "tmeLoginType": login_type},
            "req": {
                "module": "music.login.LoginServer",
                "method": "Login",
                "param": param,
            },
        }
        try:
            async with self._client("qqmusic") as client:
                response = await client.post(_MUSICU_URL, headers=dict(_QQ_HEADERS), json=payload)
        except Exception as exc:  # noqa: BLE001 - never echo raw transport exceptions
            safe_error = fetch_error(exc)
            raise _QQStageError("refresh_request", str(safe_error)) from None
        if response.status_code >= 400:
            raise _QQStageError(
                "refresh_http", "QQ 音乐续期服务暂时不可用，请稍后重试", code=response.status_code
            )
        try:
            body = response.json()
        except (ValueError, UnicodeDecodeError):
            raise _QQStageError("refresh_response", "QQ 音乐续期返回数据格式不符合预期") from None
        req = body.get("req") if isinstance(body, dict) else None
        if not isinstance(req, dict):
            raise _QQStageError("refresh_response", "QQ 音乐续期返回数据格式不符合预期")
        code = _safe_business_code(req.get("code"))
        if code is None:
            raise _QQStageError("refresh_response", "QQ 音乐续期未返回有效状态码")
        if code != 0:
            if code == 20279:
                message = f"QQ 音乐续期失败：设备登录数已达上限（错误码 {code}），请在 QQ 音乐客户端的设备管理中清理旧设备后重试"
            elif code == 104604:
                message = f"QQ 音乐续期失败：操作过于频繁（错误码 {code}），请稍后重试"
            elif code in (1000, 104400, 104401):
                message = f"QQ 音乐续期授权已失效（错误码 {code}），请重新扫码登录"
            else:
                message = f"QQ 音乐续期被服务端拒绝（错误码 {code}）"
            raise _QQStageError("refresh_response", message, code=code)
        data = req.get("data")
        if not isinstance(data, dict) or not data:
            raise _QQStageError("refresh_response", "QQ 音乐续期返回了不完整结果", code=code)
        return _merge_refreshed_credential(data, credential)

    @staticmethod
    def _needs_refresh(credential: QQMobileCredential) -> bool:
        expires = credential.key_expires_in or DEFAULT_KEY_EXPIRES
        buffer_sec = min(max(expires // 5, 3600), 86400 * 7)
        created = credential.musickey_create_time or 0
        return time.time() >= created + expires - buffer_sec

    # -- 私有状态文件 -------------------------------------------------------
    def _state_path(self) -> Path:
        return self._data_dir / "qq_mobile.json"

    def _read_state_file(self) -> dict[str, Any]:
        path = self._state_path()
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return {}
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _read_qq_state(self, fingerprint: str) -> "_QQState | None":
        """只认指纹一致的记录：Cookie 换了，旧 refresh_token 就不能再用。"""
        if not fingerprint:
            return None
        node = self._read_state_file().get("platforms")
        entry = node.get("qqmusic") if isinstance(node, dict) else None
        if not isinstance(entry, dict):
            return None
        if text_of(entry.get("fingerprint")) != fingerprint:
            return None
        raw = entry.get("credential")
        if not isinstance(raw, dict):
            return None
        try:
            credential = QQMobileCredential(**_known_fields(raw))
        except TypeError:
            return None
        if not credential.musickey or not credential.refresh_token:
            return None
        return _QQState(fingerprint=fingerprint, credential=credential)

    def _store_qq_state(self, cookie: str, credential: QQMobileCredential) -> None:
        fingerprint = _fingerprint(cookie)
        if not fingerprint:
            return
        data = self._read_state_file()
        platforms = data.get("platforms")
        if not isinstance(platforms, dict):
            platforms = {}
        platforms["qqmusic"] = {
            "fingerprint": fingerprint,
            "credential": asdict(credential),
            "updated_at": int(time.time()),
        }
        self._write_state_file({"version": 1, "platforms": platforms})

    def _drop_qq_state(self) -> None:
        data = self._read_state_file()
        platforms = data.get("platforms")
        if isinstance(platforms, dict):
            platforms.pop("qqmusic", None)
        if not platforms:
            with contextlib.suppress(FileNotFoundError):
                self._state_path().unlink()
            return
        data["platforms"] = platforms
        self._write_state_file(data)

    def _write_state_file(self, data: dict[str, Any]) -> None:
        """临时文件 + ``os.replace``，避免断电/并发写出半个 JSON。"""
        path = self._state_path()
        temp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            with contextlib.suppress(OSError):
                os.chmod(temp, 0o600)
            os.replace(temp, path)
        except OSError as exc:
            raise MusicError("QQ 音乐续期凭据未能写入私有目录，请检查插件数据目录权限") from exc
        finally:
            with contextlib.suppress(OSError):
                temp.unlink(missing_ok=True)

    # -- 杂项 ---------------------------------------------------------------
    @staticmethod
    def _normalize_platform(platform: str) -> str:
        key = str(platform or "").strip().lower()
        if key not in PLATFORMS:
            raise MusicError("未知的音乐平台，只支持网易云音乐和 QQ 音乐")
        return key

    @staticmethod
    def _with_hint(message: str) -> str:
        text = str(message or "").strip() or "登录未完成"
        if "Cookie" in text:
            return text
        return f"{text}；{_COOKIE_HINT}"


@dataclass
class _QQState:
    fingerprint: str
    credential: QQMobileCredential


def _known_fields(raw: dict[str, Any]) -> dict[str, Any]:
    """过滤掉文件里多余的键，避免旧版本字段让 dataclass 构造失败。"""
    allowed = {field_info.name for field_info in QQMobileCredential.__dataclass_fields__.values()}
    return {key: value for key, value in raw.items() if key in allowed}


__all__ = [
    "LoginManager",
    "QQMobileCredential",
    "parse_qq_cookie",
    "format_qq_cookie",
    "PLATFORMS",
]
