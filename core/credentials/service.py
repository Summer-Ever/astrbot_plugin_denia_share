"""统一凭据服务：快照 / 导入保存 / 主动检查 / 清除。

与其它模块的边界（防止同文件冲突）：

* 不改 ``main.py`` / ``core/webui.py`` / ``core/config.py``；
* 只在**明确调用** ``check()`` 时发网络请求；
* 所有 HTTP 目标 URL 在 :data:`CHECK_TARGETS` 里写死，不接受调用方传入；
* 状态缓存只留**指纹**（sha256），不留 secret 原文；
* 每个平台一把锁 + 返回前复查指纹，避免旧检查结果覆盖新凭据。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from .. import http_env  # noqa: F401 -- 与下载链路共用 NO_PROXY 兼容处理

from .importers import (
    PLATFORM_DOMAINS,
    TOKEN_PLATFORMS,
    CredentialImportError,
    parse_credential,
)
from ..music.client import text_of

# ---- 状态取值（契约固定） ---------------------------------------------- #
STATUS_UNCONFIGURED = "unconfigured"
STATUS_CONFIGURED = "configured"
STATUS_VALID = "valid"
STATUS_INVALID = "invalid"
STATUS_UNKNOWN = "unknown"
STATUS_ERROR = "error"

#: 固定检查目标 URL：不可由调用方覆盖，不跟随任意重定向。
CHECK_TARGETS: dict[str, dict[str, Any]] = {
    "bilibili": {
        "url": "https://api.bilibili.com/x/web-interface/nav",
        "method": "GET",
        "credential": "cookie",
    },
    "xiaohongshu": {
        "url": "https://edith.xiaohongshu.com/api/sns/web/v2/user/me",
        "method": "GET",
        "credential": "cookie",
    },
    "pixiv": {
        "url": "https://www.pixiv.net/ajax/user/extra?lang=zh",
        "method": "GET",
        "credential": "cookie",
    },
    "github": {
        "url": "https://api.github.com/user",
        "method": "GET",
        "credential": "token",
    },
}

_DEFAULT_TIMEOUT = 15.0


@dataclass(frozen=True)
class PlatformSpec:
    id: str
    label: str
    kind: str
    config_key: str
    #: 是否为「配置项优先」的平台（B 站还有扫码登录态）。
    config_first: bool = False


PLATFORMS: tuple[PlatformSpec, ...] = (
    PlatformSpec("bilibili", "B站", "cookie", "BILI_CK", config_first=True),
    PlatformSpec("xiaohongshu", "小红书", "cookie", "XHS_CK"),
    PlatformSpec("pixiv", "Pixiv", "cookie", "PIXIV_CK"),
    PlatformSpec("github", "GitHub", "token", "GITHUB_TOKEN"),
    PlatformSpec("netease", "网易云音乐", "cookie", "NETEASE_CK"),
    PlatformSpec("qqmusic", "QQ音乐", "cookie", "QQMUSIC_CK"),
    PlatformSpec("xiaoheihe", "小黑盒", "cookie", "XIAOHEIHE_CK"),
)

_PLATFORM_BY_ID: dict[str, PlatformSpec] = {p.id: p for p in PLATFORMS}


class CredentialError(Exception):
    """面向用户的安全错误。

    消息里只允许出现**用户自己看得懂的描述**，不得回显导入原文、
    请求头或 token 片段。
    """


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _fingerprint(secret: str | None) -> str:
    if not secret:
        return ""
    return hashlib.sha256(secret.encode("utf-8", "ignore")).hexdigest()


def _config_value(pconfig: Any, key: str) -> str:
    try:
        return str(getattr(pconfig, key, "") or "").strip()
    except Exception:
        return ""


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def public_config_values(pconfig: Any) -> tuple[dict[str, Any], dict[str, dict[str, bool]]]:
    """配置接口的脱敏视图。

    返回 ``(values, secret_status)``：

    * ``values`` 键集合与内部一致，``secret=True`` 的键值置空（键保留）；
    * ``secret_status[key] = {"configured": bool}``，不含长度/片段。

    **不修改** ``pconfig`` 内部的 ``current_values``，只影响 HTTP 输出。
    """
    from ..config import CONFIG_META

    source = pconfig.current_values
    current = dict(source() if callable(source) else source)

    values: dict[str, Any] = {}
    secret_status: dict[str, dict[str, bool]] = {}
    for item in CONFIG_META:
        key = item.get("key")
        if not key or item.get("internal"):
            continue
        raw = current.get(key, item.get("default"))
        if item.get("secret"):
            configured = bool(str(current.get(key, "") or "").strip())
            secret_status[key] = {"configured": configured}
            values[key] = ""
        else:
            values[key] = raw
    return values, secret_status


class CredentialManager:
    """平台凭据的统一入口。

    ``plugin`` 需要提供（都是**已存在**的成员，本包不改它们）：

    * ``apply_runtime_config()`` —— 保存后热应用；
    * ``bili_logout()`` —— B 站退出的唯一正确实现；
    * ``_bili_cookie`` / ``_bili_cookie_file`` —— B 站生效态与扫码持久化。

    测试桩若缺成员，用 :meth:`_optional` 做**合理降级**而不是崩掉。
    """

    def __init__(self, plugin: Any) -> None:
        self._plugin = plugin
        self._locks: dict[str, asyncio.Lock] = {p.id: asyncio.Lock() for p in PLATFORMS}
        # platform -> {"fingerprint", "status", "message", "checked_at"}
        self._status: dict[str, dict[str, Any]] = {}
        self._sources: dict[str, str] = {}
        self._source_listener = None
        # 标记「本次探针自己轮换了 Cookie」（QQ 续期式校验），check 复查指纹时放行。
        self._probe_rotated: dict[str, bool] = {}

    # ------------------------------------------------------------------ #
    # 内部小工具
    # ------------------------------------------------------------------ #
    @property
    def _pconfig(self) -> Any:
        from ..config import get_config

        return get_config()

    def _optional(self, name: str, default: Any = None) -> Any:
        return getattr(self._plugin, name, default)

    def _spec(self, platform: str) -> PlatformSpec:
        spec = _PLATFORM_BY_ID.get(str(platform or "").strip().lower())
        if spec is None:
            raise CredentialError("未知平台")
        return spec

    def _current_secret(self, platform: str) -> str:
        """**实际生效**的凭据，不只是「配置项非空」。"""
        spec = self._spec(platform)
        if spec.config_first:
            live = self._optional("_bili_cookie", "")
            live = str(live or "").strip()
            if live:
                return live
        return _config_value(self._pconfig, spec.config_key)

    def _source_of(self, spec: PlatformSpec, effective: str, configured: str) -> str:
        """来源依据「实际生效值」与配置/扫码记录的关系，不是 bool 配置。"""
        if not effective:
            return "none"
        if self._sources.get(spec.id) == _fingerprint(effective):
            return "cookiecloud"
        if not spec.config_first:
            return "config"
        # 扫码文件先判：扫码登录会把结果同时写进 BILI_CK（跨重启持久化），
        # 只看配置的话会把「扫码登录」误标成「配置保存」。
        scanned = self._bili_scanned_cookie()
        if scanned and effective == scanned:
            return "qrcode"
        if configured and effective == configured:
            return "config"
        return "memory"

    def _bili_scanned_cookie(self) -> str:
        path = self._optional("_bili_cookie_file")
        if not path:
            return ""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return ""
        if isinstance(data, dict):
            return str(data.get("cookie", "") or "").strip()
        return ""

    # ------------------------------------------------------------------ #
    # 快照
    # ------------------------------------------------------------------ #
    def snapshot(self) -> dict[str, Any]:
        """同步返回 GET 形状；**不含任何 secret**。"""
        platforms: list[dict[str, Any]] = []
        try:
            pconfig = self._pconfig
        except Exception:
            pconfig = None

        for spec in PLATFORMS:
            effective = self._current_secret(spec.id)
            configured = _config_value(pconfig, spec.config_key) if pconfig else ""
            cached = self._status.get(spec.id, {})
            fingerprint = _fingerprint(effective)

            # 凭据被替换/清除 → 旧检查结论立即失效。
            if cached.get("fingerprint") != fingerprint:
                cached = {}
                self._status.pop(spec.id, None)

            if not effective:
                status, message = STATUS_UNCONFIGURED, "尚未配置凭据"
            else:
                status = cached.get("status") or STATUS_CONFIGURED
                message = cached.get("message") or "已保存，尚未检查"

            platforms.append(
                {
                    "id": spec.id,
                    "label": spec.label,
                    "kind": spec.kind,
                    "configured": bool(effective),
                    "source": self._source_of(spec, effective, configured),
                    "status": status,
                    "message": message,
                    "checked_at": cached.get("checked_at"),
                }
            )
        return {"platforms": platforms}

    def _record(self, platform: str, fingerprint: str, status: str, message: str) -> None:
        self._status[platform] = {
            "fingerprint": fingerprint,
            "status": status,
            "message": message,
            "checked_at": _now_iso(),
        }

    # ------------------------------------------------------------------ #
    # 导入 + 保存
    # ------------------------------------------------------------------ #
    async def import_and_save(
        self, platform: str, content: str, fmt: str = "auto", *,
        overwrite_existing: bool = True, source: str = "config",
    ) -> dict[str, Any]:
        spec = self._spec(platform)
        async with self._locks[spec.id]:
            current = self._current_secret(spec.id)
            if not overwrite_existing and current and self._sources.get(spec.id) != _fingerprint(current):
                return {**self.snapshot(), "import_skipped": True}
            try:
                value = parse_credential(spec.id, content, fmt)
            except CredentialImportError as exc:
                # 消息已由解析层保证不含原文。
                raise CredentialError(str(exc)) from None

            if spec.id == "bilibili":
                # 旧的扫码轮询可能在我们写入新凭据之后才判定成功并回写，先停掉它。
                await self._cancel_bili_login()

            pconfig = self._pconfig
            try:
                changed, errors = pconfig.apply_updates({spec.config_key: value})
                if not changed and not errors and not pconfig.save():
                    errors = ["persistence failed"]
            except Exception:
                raise CredentialError("保存未完成，请检查配置存储后重试") from None

            if errors:
                raise CredentialError("凭据未能持久保存，当前内存配置可能已变更，请检查配置存储后重试")

            # 保存成功即登记来源：后续「清理旧登录态 / 热更新」即使失败，也不能让
            # 自动同步以为「值没变」而永久跳过——否则用户手动修好的值会被一直忽略。
            # 失败时仍抛 CredentialError（带明确的部分成功信息），用户可原样重试。
            self._status.pop(spec.id, None)
            if source == "cookiecloud":
                self._sources[spec.id] = _fingerprint(value)
            else:
                self._sources.pop(spec.id, None)
            if self._source_listener is not None:
                self._source_listener()

            if spec.id == "bilibili" and hasattr(self._plugin, "_bili_ck_applied"):
                # 这是明确的替换操作；即使配置值相同，也要覆盖期间的扫码态。
                self._plugin._bili_ck_applied = None
            if spec.id in {"netease", "qqmusic"}:
                # A manual replacement must invalidate older QR / refresh sessions,
                # including a replacement whose Cookie text happens to be identical.
                await self._clear_music_login(spec.id)
            applied = await self._apply_runtime()
            if applied is False:
                raise CredentialError("配置已写入，但运行时应用失败，可能尚未生效，请到配置页确认")

            return self.snapshot()

    async def _apply_runtime(self) -> bool:
        apply_fn = self._optional("apply_runtime_config")
        if apply_fn is None:
            return True
        try:
            result = await apply_fn()
        except Exception:
            return False
        if result is False or isinstance(result, dict) and result.get("runtime_ok") is False:
            return False
        return True

    # ------------------------------------------------------------------ #
    # 主动检查
    # ------------------------------------------------------------------ #
    async def check(self, platform: str) -> dict[str, Any]:
        spec = self._spec(platform)
        async with self._locks[spec.id]:
            effective = self._current_secret(spec.id)
            if not effective:
                return self.snapshot()
            before = _fingerprint(effective)
            try:
                status, message = await self._probe(spec, effective)
            except CredentialError:
                raise
            except Exception:
                status, message = STATUS_ERROR, "检查请求出错，请稍后重试"

            # 返回前复查指纹：QQ 探针内的服务端续期可能合法地轮换了 Cookie——
            # 那是本次检查自己的产物，按新指纹记录；除此之外的指纹变化意味着
            # 期间被扫码/导入/清除换掉，就不覆盖新状态。
            after = _fingerprint(self._current_secret(spec.id))
            if after != before and not self._probe_rotated.pop(spec.id, False):
                self._status.pop(spec.id, None)
            else:
                self._probe_rotated.pop(spec.id, None)
                self._record(spec.id, after or before, status, message)
            return self.snapshot()

    def _client_kwargs(self, spec: PlatformSpec) -> dict[str, Any]:
        pconfig = self._pconfig
        kwargs: dict[str, Any] = {
            "verify": _truthy(getattr(pconfig, "HTTP_VERIFY_SSL", True)),
            "trust_env": True,
            "follow_redirects": False,  # 固定目标，不跟任意重定向
            "timeout": _DEFAULT_TIMEOUT,
        }
        try:
            proxy = pconfig.proxy_for(spec.id)
        except Exception:
            proxy = None
        if proxy:
            kwargs["proxy"] = proxy
        return kwargs

    async def _probe_qqmusic_via_refresh(self) -> tuple[str, str] | None:
        """用续期端点做 QQ 音乐的权威判活；结论不明确时返回 None 走探针兜底。

        成功即服务端已确认凭据并轮换了 Cookie（等价一次续期），调用方要靠
        ``_probe_rotated`` 放行 check 的指纹复查。错误信息只看状态机给出的
        安全文案，不解析任何敏感内容。
        """
        try:
            controller = getattr(self._plugin, "_get_music_controller", None)
            manager = controller().login if callable(controller) else None
            if manager is None:
                return None
            result = await manager.refresh("qqmusic")
        except Exception:
            return None
        message = str(result.get("message", ""))
        if result.get("state") == "success":
            self._probe_rotated["qqmusic"] = True
            return STATUS_VALID, "QQ 音乐登录态有效（服务端已确认并续期）"
        if "没有可续期" in message or "没有 QQ 音乐登录态" in message:
            return None  # 纯 Cookie 导入，没有移动端凭据，交给探针兜底
        if "授权已失效" in message:
            return STATUS_INVALID, "QQ 音乐服务端确认登录态已失效，请重新扫码登录"
        return None  # 频控/设备上限/网络波动/已取消：结论不明确，探针再试一次

    async def _probe(self, spec: PlatformSpec, secret: str) -> tuple[str, str]:
        if spec.id == "xiaoheihe":
            return STATUS_UNKNOWN, "已保存；请用帖子链接确认可用性，平台要求验证时请先在小黑盒网页完成"
        if spec.id == "qqmusic":
            # 判活选型（2026-11-01 真机实测）：
            # * GetLoginUserInfo 对鉴权完全不敏感（空/垃圾 authst 也回 code 0 与
            #   全量资料），没有判别力，绝不能用；
            # * 个人主页端点判别力成立（有效→0 / 无效→1000）但有 IP 限流，被限流
            #   时一律 404；
            # * 续期端点 music.login.LoginServer 是唯一权威信号：服务端接受凭据
            #   并换发新 key（0），明确宣告失效（1000/104400/104401）。扫码用户
            #   优先走它——顺带完成续期；没有移动端凭据（纯 Cookie 导入）才回退
            #   到主页探针。
            rotated = await self._probe_qqmusic_via_refresh()
            if rotated is not None:
                return rotated
        if spec.id in {"netease", "qqmusic"}:
            from ..music.netease import NeteaseProvider
            from ..music.qqmusic import QQMusicProvider
            provider_type = NeteaseProvider if spec.id == "netease" else QQMusicProvider
            provider = provider_type(cookie=secret, proxy=self._pconfig.proxy_for(spec.id),
                                     verify_ssl=self._pconfig.HTTP_VERIFY_SSL)
            try:
                result = await provider.check_login()
                status = result.get("status")
                if status not in {STATUS_VALID, STATUS_INVALID, STATUS_UNKNOWN}:
                    status = STATUS_UNKNOWN
                # provider 的文案已按安全规范生成（含昵称等自查信息），原样沿用。
                message = text_of(result.get("message")) or {
                    STATUS_VALID: "登录态有效", STATUS_INVALID: "登录态已失效",
                    STATUS_UNKNOWN: "暂时无法确认，请稍后重试"}[status]
                return status, message
            except Exception:
                return STATUS_UNKNOWN, "暂时无法确认（网络或代理不可用）"
        target = CHECK_TARGETS[spec.id]
        try:
            import httpx
        except ImportError:  # pragma: no cover - 运行期依赖
            return STATUS_ERROR, "缺少 httpx，无法检查"

        headers: dict[str, str] = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Accept": "application/json, text/plain, */*",
        }
        if target["credential"] == "token":
            headers["Authorization"] = f"Bearer {secret}"
        else:
            headers["Cookie"] = secret
            headers["Referer"] = {
                "bilibili": "https://www.bilibili.com/",
                "xiaohongshu": "https://www.xiaohongshu.com/",
                "pixiv": "https://www.pixiv.net/",
            }.get(spec.id, "")

        try:
            async with httpx.AsyncClient(**self._client_kwargs(spec)) as client:
                resp = await client.get(target["url"], headers=headers)
                body = resp.text
        except Exception:
            return STATUS_UNKNOWN, "暂时无法确认（网络或代理不可用）"

        return self._judge(spec.id, resp.status_code, body)

    # ---- 判读：只有**明确的**结论才给 valid/invalid，其余一律 unknown ---- #
    def _judge(self, platform: str, code: int, body: str) -> tuple[str, str]:
        if platform == "github":
            if code == 401:
                return STATUS_INVALID, "Token 无效或已过期"
            if code in (403, 429):
                return STATUS_UNKNOWN, "暂时无法确认（被限流或风控）"
            if code != 200:
                return STATUS_UNKNOWN, f"暂时无法确认（HTTP {code}）"
            data = _json_or_none(body)
            if isinstance(data, dict) and data.get("id") and data.get("login"):
                return STATUS_VALID, "Token 有效"
            return STATUS_UNKNOWN, "暂时无法确认（返回结构无法识别）"

        if platform == "bilibili":
            if code != 200:
                return STATUS_UNKNOWN, f"暂时无法确认（HTTP {code}）"
            if "<html" in body[:512].lower():
                return STATUS_UNKNOWN, "暂时无法确认（风控挑战页）"
            data = _json_or_none(body)
            if not isinstance(data, dict):
                return STATUS_UNKNOWN, "暂时无法确认（返回结构无法识别）"
            if data.get("code") == -412:
                return STATUS_UNKNOWN, "暂时无法确认（B 站风控）"
            payload = data.get("data") if isinstance(data.get("data"), dict) else {}
            if data.get("code") == -101 or payload.get("isLogin") is False:
                return STATUS_INVALID, "Cookie 已失效"
            if data.get("code") == 0 and payload.get("isLogin") is True:
                return STATUS_VALID, "登录态有效"
            return STATUS_UNKNOWN, "暂时无法确认（返回结构无法识别）"

        if platform == "xiaohongshu":
            if code != 200:
                return STATUS_UNKNOWN, f"暂时无法确认（HTTP {code}）"
            if "<html" in body[:512].lower():
                return STATUS_UNKNOWN, "暂时无法确认（验证挑战页）"
            data = _json_or_none(body)
            if not isinstance(data, dict):
                return STATUS_UNKNOWN, "暂时无法确认（返回结构无法识别）"
            payload = data.get("data")
            if not isinstance(payload, dict) or "guest" not in payload:
                return STATUS_UNKNOWN, "暂时无法确认（返回结构无法识别）"
            if payload.get("guest") is True:
                return STATUS_INVALID, "未登录（guest=true）"
            user = payload.get("userInfo") or payload.get("user_info")
            if payload.get("guest") is False and data.get("success") is True and isinstance(user, dict) and (user.get("user_id") or user.get("userId")):
                return STATUS_VALID, "登录态有效"
            return STATUS_UNKNOWN, "暂时无法确认（返回结构无法识别）"

        # pixiv
        if code != 200:
            return STATUS_UNKNOWN, f"暂时无法确认（HTTP {code}）"
        if "<html" in body[:512].lower():
            return STATUS_UNKNOWN, "暂时无法确认（风控挑战页）"
        data = _json_or_none(body)
        if not isinstance(data, dict):
            return STATUS_UNKNOWN, "暂时无法确认（返回结构无法识别）"
        if data.get("error") is True:
            return STATUS_UNKNOWN, "暂时无法确认（接口返回错误）"
        body_field = data.get("body")
        if data.get("error") is False and isinstance(body_field, dict) and body_field.get("isLogin") is True:
            return STATUS_VALID, "登录态有效"
        if isinstance(body_field, dict) and body_field.get("isLogin") is False:
            return STATUS_INVALID, "Cookie 已失效"
        return STATUS_UNKNOWN, "暂时无法确认（返回结构无法识别）"

    # ------------------------------------------------------------------ #
    # 清除
    # ------------------------------------------------------------------ #
    async def clear(self, platform: str) -> dict[str, Any]:
        spec = self._spec(platform)
        async with self._locks[spec.id]:
            if spec.id == "bilibili":
                # 清除前先让扫码任务退出：否则它可能在清除之后判定成功，
                # 把刚清掉的 Cookie 又写回来（服务层显式做一遍，不依赖
                # 插件 ``bili_logout`` 的实现细节）。取消是幂等的。
                await self._cancel_bili_login()
                ok = await self._bili_logout()
                if ok is False:
                    raise CredentialError("B 站退出未完全成功，请检查持久化文件")
            else:
                pconfig = self._pconfig
                try:
                    changed, errors = pconfig.apply_updates({spec.config_key: ""})
                    if not changed and not errors and not pconfig.save():
                        errors = ["persistence failed"]
                except Exception:
                    raise CredentialError("清除未完成，请检查配置存储后重试") from None
                if errors:
                    raise CredentialError("清除未能持久保存，当前内存配置可能已变更，请检查配置存储后重试")
                if await self._apply_runtime() is False:
                    raise CredentialError("配置已清除，但运行时应用失败，请到配置页确认")
                if spec.id in {"netease", "qqmusic"}:
                    await self._clear_music_login(spec.id)
            self._status.pop(spec.id, None)
            self._sources.pop(spec.id, None)
            if self._source_listener is not None:
                self._source_listener()
            return self.snapshot()

    async def _clear_music_login(self, platform):
        try:
            login = self._optional("_music_login_manager")
            controller = self._optional("_get_music_controller")
            if login is None and controller is not None:
                login = controller().login
            if login is not None:
                await login.clear(platform)
        except Exception:
            raise CredentialError("凭据已保存，但旧音乐登录态未能清理，请重试") from None

    async def _cancel_bili_login(self) -> None:
        """让 B站扫码登录的后台轮询先退出。

        它是「清除后又被扫码结果写回来」的唯一来源：任务可能在清除之后才轮询到
        成功。取消是幂等的，且 ``bili_login_cancel`` 只取消**未完成**的任务，
        因此不会误删已经成功的登录。
        """
        fn = self._optional("bili_login_cancel")
        if fn is None:
            return
        try:
            await fn()
        except Exception:
            raise CredentialError("B站扫码登录未能停止，请稍后重试") from None

    async def _bili_logout(self) -> bool:
        """复用既有 ``bili_logout``：内存 + 两处持久化 + 配置项一起清。"""
        fn: Callable[..., Any] | None = self._optional("bili_logout")
        if fn is None:
            # 测试桩降级：至少清配置项，不假装成功。
            try:
                self._pconfig.apply_updates({"BILI_CK": ""})
            except Exception:
                return False
            self._plugin._bili_cookie = ""
            return True
        try:
            result = await fn()
        except Exception:
            return False
        return result is not False


def _json_or_none(body: str) -> Any:
    try:
        return json.loads(body)
    except (ValueError, TypeError):
        return None


__all__ = [
    "CHECK_TARGETS",
    "PLATFORMS",
    "STATUS_CONFIGURED",
    "STATUS_ERROR",
    "STATUS_INVALID",
    "STATUS_UNCONFIGURED",
    "STATUS_UNKNOWN",
    "STATUS_VALID",
    "CredentialError",
    "CredentialManager",
    "PlatformSpec",
    "PLATFORM_DOMAINS",
    "TOKEN_PLATFORMS",
    "public_config_values",
]
