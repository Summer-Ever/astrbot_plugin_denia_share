"""插件 WebUI 的后端接口层。

页面（``pages/denia/``）通过 AstrBot 的 bridge 调用这里注册的接口。
分工：

- 本模块只做「取参数 → 校验 → 调用插件已有能力 → 拼 JSON」，
  不重复实现解析/渲染/登录逻辑，全部复用 ``DeniaSharePlugin`` 上的方法。
- 路由统一带插件名前缀（``/astrbot_plugin_denia_share/xxx``），
  这是 AstrBot 的要求；页面侧通过 bridge 写的是去掉前缀的相对路径。
- 图片不靠 URL 暴露（受限 iframe 带不上鉴权头），而是缩成 base64 data URL
  随 JSON 返回；需要原图/原文件时走 ``bridge.download``。

安全约定：页面运行在受限 iframe 里，但后端仍按「不可信输入」处理——
配置只接受白名单键、缓存文件只允许访问 ``cache_dir`` 内且能被记录引用的文件、
链接长度与格式都做校验。

版本兼容：``register_web_api`` 从 AstrBot 4.24.2 起可用，而 ``astrbot.api.web``
要 4.27 才有。本模块不直接 import 后者，全部经 :mod:`.web_compat` 走，
否则 4.24.2~4.26.x 上会出现「路由注册成功但每次调用都抛 ImportError」的白屏。
"""

from __future__ import annotations

import asyncio
import base64
import io
import time
import uuid
import httpx
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Iterable

from astrbot.api import logger

from .card_renderer import LAYOUT_NAMES, ShareCardRenderer
from .config import config_meta_payload, get_config, verify_schema_alignment
from .constants import (
    PLATFORM_DISPLAY_NAMES,
    is_custom_platform,
    platform_order,
)
from .exception import IgnoreException, ParseException
from .media_utils import clear_cache_dir, cleanup_cache_dir, is_docker_environment
from .music.models import MusicError
from .relay import resolve_callback_base
from .screenshot import is_probably_screenshotable
# 注意：``json_response`` / ``error_response`` **故意不在这里别名导入** ——
# 本文件下方已经定义了同名薄封装 ``_json_response`` / ``_error``（它们才是
# 所有调用点实际用的东西）。先前的 ``error_response as _error`` 会被同名函数
# 定义完全遮蔽，纯属误导性死代码，已删。
from .web_compat import (
    HAS_WEB_API,
    file_response,
    get_json_body,
    query_arg,
)


def _renderer_kwargs(opts: dict[str, Any]) -> dict[str, Any]:
    """只保留当前 ShareCardRenderer 构造器认识的参数。

    外观项是一个个加进来的：配置项可能先于渲染器落地，这时多出来的键
    会把构造器直接炸成 TypeError。按签名过滤掉渲染器还不认识的键，
    缺的那项功能先不显示，其余预览照常出图。
    """
    from inspect import Parameter, signature

    try:
        params = signature(ShareCardRenderer.__init__).parameters
    except (TypeError, ValueError):
        return dict(opts)
    if any(p.kind is Parameter.VAR_KEYWORD for p in params.values()):
        return dict(opts)
    return {key: value for key, value in opts.items() if key in params}


def _astrbot_callback_base() -> str:
    """读取 AstrBot 全局 callback_api_base（取不到就返回空串）。"""
    try:
        return resolve_callback_base("")
    except Exception:
        return ""

PLUGIN_NAME = "astrbot_plugin_denia_share"

# 缩略图/预览图的最大宽度与体积上限。
# 预览图要经 postMessage 传进 iframe，几 MB 的 base64 会明显卡顿，所以宁可不给。
PREVIEW_MAX_WIDTH = 640
PREVIEW_MAX_BYTES = 3 * 1024 * 1024
MAX_URL_LENGTH = 2048
MAX_QUERY_LIMIT = 100


class PreviewOverridesError(Exception):
    """预览覆盖参数不合法。

    刻意和「没有覆盖项」区分开：没有覆盖项时用常驻渲染器按当前配置出图，
    参数非法时必须报错——否则用户改了一个非法颜色，看到的却是按旧配置渲染的
    图，还以为自己改的值生效了。
    """

try:  # Pillow 是插件依赖，缺了只影响预览图，不影响其余接口
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None  # type: ignore[assignment]


def _json_response(data: Any, status_code: int = 200):
    """成功响应（裸业务对象，不套信封）。

    实现见 :mod:`.web_compat` —— 那里统一处理了 ``astrbot.api.web``（4.27+）
    与 quart 两种宿主形态的差异。此处保留薄封装，是为了让 23 个调用点的
    写法与历史一致、不必逐个改成 import 别名。
    """
    from .web_compat import json_response

    return json_response(data, status_code=status_code)


def _error(message: str, status_code: int = 400):
    """失败响应。信封形状由 :mod:`.web_compat` 保证两条路径一致。"""
    from .web_compat import error_response

    return error_response(message, status_code=status_code)


def _response_status(response: Any) -> Any:
    """从返回值里取 HTTP 状态码，取不到就返回 "?"。

    两种宿主形态的返回值形状不同：quart 是带 ``status`` 的 Response 对象，
    而 ``astrbot.api.web`` 的响应对象字段名未公开、可能压根不叫 ``status``。
    记日志用，宁可显示 "?" 也不能让取状态码本身抛异常——那会把一次成功的
    接口调用变成 500。
    """
    if isinstance(response, tuple) and len(response) > 1:
        return response[1]
    return getattr(response, "status", None) or getattr(response, "status_code", None) or "?"


def _dir_stats(path: Path) -> tuple[int, int]:
    """统计目录下的文件数与总字节数。"""
    files = 0
    total = 0
    try:
        for item in path.rglob("*"):
            if item.is_file():
                files += 1
                try:
                    total += item.stat().st_size
                except OSError:
                    continue
    except OSError:
        return 0, 0
    return files, total


def _image_data_url(path: Path, max_width: int = PREVIEW_MAX_WIDTH) -> str | None:
    """把本地图片缩成 base64 data URL；不可用或过大时返回 None。"""
    if Image is None or not path.is_file():
        return None
    try:
        resample = getattr(Image, "Resampling", Image).LANCZOS
    except Exception:  # pragma: no cover - 老版本 Pillow
        resample = None

    try:
        with Image.open(path) as source:
            image = source.convert("RGBA")
            if max_width > 0 and image.width > max_width:
                ratio = max_width / image.width
                size = (max_width, max(1, int(image.height * ratio)))
                image = image.resize(size, resample) if resample else image.resize(size)

            buffer = io.BytesIO()
            has_alpha = image.mode in {"RGBA", "LA", "P"} and _has_alpha(image)
            if has_alpha:
                image.save(buffer, "PNG", optimize=True)
                mime = "png"
            else:
                image.convert("RGB").save(buffer, "JPEG", quality=86, optimize=True)
                mime = "jpeg"
            data = buffer.getvalue()
    except Exception:
        logger.debug("生成预览图失败: %s", path, exc_info=True)
        return None

    if len(data) > PREVIEW_MAX_BYTES:
        logger.debug("预览图过大，已跳过: %s (%d 字节)", path, len(data))
        return None
    return f"data:image/{mime};base64," + base64.b64encode(data).decode("ascii")


def _has_alpha(image: Any) -> bool:
    """判断图片是否真的用到了透明通道（纯不透明的 PNG 转 JPEG 能小很多）。"""
    try:
        if image.mode not in {"RGBA", "LA"}:
            return False
        alpha = image.getchannel("A")
        return alpha.getextrema()[0] < 255
    except Exception:
        return False


# ---------------- 预览图编码缓存 ----------------
#
# 缓存页每次 render() 都会 clear 容器重建整张列表，每一行重新请求
# cache/thumbnail —— 也就是把同一批卡片图重新开一遍、缩一遍、base64 编一遍。
# 按「刷新」按钮、切走再切回来都会重来一次，一页 20 条就是 20 次 Pillow 全量重编码。
#
# 按字节预算而不是条数淘汰：单张 900px 预览就有可能几百 KB，条数上限换算出来的
# 内存占用太难看。存的是 base64 字符串（比二进制大约 1.37 倍），预算就按字符串长度算。
_IMAGE_CACHE_BUDGET = 24 * 1024 * 1024
_image_cache: "OrderedDict[tuple[str, int, int, int], str]" = OrderedDict()
_image_cache_bytes = 0


def _image_data_url_cached(path: Path, max_width: int = PREVIEW_MAX_WIDTH) -> str | None:
    """带缓存的 :func:`_image_data_url`。

    缓存键带上 mtime_ns 与文件大小：卡片被重新渲染（重新生成一张图）时这两个都会变，
    因此不会把上一版的预览图配给新的卡片文件。stat 失败（文件刚好被清掉）就不走缓存。
    """
    global _image_cache_bytes

    try:
        stat = path.stat()
    except OSError:
        stat = None
    key = (str(path), stat.st_mtime_ns, stat.st_size, max_width) if stat else None

    if key is not None:
        hit = _image_cache.get(key)
        if hit is not None:
            _image_cache.move_to_end(key)
            return hit

    value = _image_data_url(path, max_width)
    # 只缓存成功的结果：失败（文件已清理、格式不认识）多半是暂时的，
    # 缓存下来会让「文件后来补齐了」也永远拿不到图。
    if key is None or value is None:
        return value

    _image_cache[key] = value
    _image_cache_bytes += len(value)
    while _image_cache_bytes > _IMAGE_CACHE_BUDGET and _image_cache:
        _, evicted = _image_cache.popitem(last=False)
        _image_cache_bytes -= len(evicted)
    return value


def clear_image_cache() -> None:
    """清空预览图缓存（清理缓存/删除记录后调用，避免留着指向已删文件的条目）。"""
    global _image_cache_bytes
    _image_cache.clear()
    _image_cache_bytes = 0


def _safe_under(base: Path, candidate: Path) -> Path | None:
    """把 candidate 解析到 base 之内，越界返回 None（防目录穿越）。"""
    try:
        base_resolved = base.resolve()
        target = (base_resolved / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
        target.relative_to(base_resolved)
    except (OSError, ValueError):
        return None
    return target


class WebUIApi:
    """插件 WebUI 的接口集合。"""

    def __init__(self, plugin: Any):
        self.plugin = plugin
        self._credentials_manager = None

    # ==================== 注册 ==================== #

    def register(self) -> None:
        context = self.plugin.context
        routes: tuple[tuple[str, Callable[..., Any], tuple[str, ...], str], ...] = (
            ("/overview", self.overview, ("GET",), "插件总览与状态"),
            ("/config", self.get_config, ("GET",), "读取全部配置项"),
            ("/config", self.save_config, ("POST",), "保存配置项"),
            ("/config/reset", self.reset_config, ("POST",), "恢复默认配置"),
            ("/credentials", self.credentials, ("GET",), "凭据状态"),
            ("/credentials/import", self.import_credential, ("POST",), "导入并保存凭据"),
            ("/credentials/check", self.check_credential, ("POST",), "检查凭据"),
            ("/credentials/clear", self.clear_credential, ("POST",), "清除凭据"),
            ("/cookiecloud", self.cookiecloud_status, ("GET",), "CookieCloud 同步状态"),
            ("/cookiecloud/sync", self.cookiecloud_sync, ("POST",), "同步选定平台登录态"),
            ("/diagnostics", self.diagnostics, ("GET",), "最近解析诊断"),
            ("/diagnostics/export", self.export_diagnostic, ("POST",), "导出脱敏诊断"),
            ("/music/login/start", self.music_login_start, ("POST",), "获取音乐登录二维码"),
            ("/music/login/poll", self.music_login_poll, ("POST",), "音乐扫码状态"),
            ("/music/login/refresh", self.music_login_refresh, ("POST",), "续期音乐登录态"),
            ("/platforms", self.toggle_platform, ("POST",), "启用/禁用平台"),
            ("/custom_parsers", self.custom_parsers, ("GET",), "自定义解析器列表"),
            ("/custom_parsers/reload", self.reload_custom_parsers, ("POST",), "重新加载自定义解析器"),
            ("/parse", self.parse_url, ("POST",), "手动解析链接"),
            ("/render", self.render_cached, ("POST",), "按指定外观重新渲染卡片"),
            ("/preview/sample", self.sample_preview, ("POST",), "离线示例卡片预览"),
            ("/appearance", self.get_appearance, ("GET",), "读取界面外观偏好"),
            ("/appearance", self.save_appearance, ("POST",), "保存界面外观偏好"),
            ("/screenshot", self.screenshot, ("POST",), "网页截图测试"),
            ("/cache", self.list_cache, ("GET",), "解析记录列表"),
            ("/cache/delete", self.delete_cache, ("POST",), "删除解析记录"),
            ("/cache/clear", self.clear_cache, ("POST",), "清空解析记录/缓存文件"),
            ("/cache/cleanup", self.cleanup_cache, ("POST",), "按过期时间清理缓存文件"),
            ("/cache/thumbnail", self.cache_thumbnail, ("GET",), "解析记录卡片缩略图"),
            ("/cache/download", self.cache_download, ("GET",), "下载解析记录卡片原图"),
            ("/bili/status", self.bili_status, ("GET",), "B站登录状态"),
            ("/bili/qrcode", self.bili_qrcode, ("POST",), "获取B站登录二维码"),
            ("/bili/qrcode/status", self.bili_qrcode_status, ("GET",), "B站扫码状态"),
            ("/bili/qrcode/cancel", self.bili_qrcode_cancel, ("POST",), "取消B站扫码登录"),
            ("/bili/logout", self.bili_logout, ("POST",), "清除B站 Cookie"),
        )
        for suffix, handler, methods, desc in routes:
            context.register_web_api(
                f"/{PLUGIN_NAME}{suffix}",
                self._with_timing(suffix, handler),
                list(methods),
                desc,
            )

    @staticmethod
    def _with_timing(suffix: str, handler: Callable[..., Any]) -> Callable[..., Any]:
        """给每个 handler 包一层耗时日志。

        WebUI 的排障有个特点：用户报「点了没反应 / 一直是转圈」，能看到的只有
        浏览器控制台，服务器这边没有对应记录。给全部 23 个路由统一记
        「开始 / 完成（含状态码与耗时）/ 失败（含异常栈）」，用户截图里的时间点
        就能和日志对上——否则只能靠猜。

        失败路径必须重新抛出：吞掉异常会返回一个 200 空响应，页面上表现为
        「按钮点了没反应」，比报错更难查。
        """

        async def timed(*args: Any, **kwargs: Any):
            started = time.perf_counter()
            try:
                response = await handler(*args, **kwargs)
            except Exception:
                elapsed = int((time.perf_counter() - started) * 1000)
                logger.error(
                    "[denia_share] WebUI %s 失败 耗时=%dms", suffix, elapsed, exc_info=True
                )
                raise
            elapsed = int((time.perf_counter() - started) * 1000)
            logger.info(
                "[denia_share] WebUI %s 状态=%s 耗时=%dms",
                suffix,
                _response_status(response),
                elapsed,
            )
            return response

        timed.__name__ = f"webui_{handler.__name__}"
        timed.__doc__ = handler.__doc__
        return timed

    # ==================== 总览 ==================== #

    async def overview(self):
        plugin = self.plugin
        pconfig = get_config()

        platforms = [
            {
                "name": name,
                "label": PLATFORM_DISPLAY_NAMES.get(name, name),
                "enabled": name in plugin.parsers,
                # 前端把自定义解析器单独分组显示（它们能加载失败、能被重新加载，
                # 内置平台没有这些状态）
                "custom": is_custom_platform(name),
            }
            for name in platform_order()
        ]
        files, size = await asyncio.to_thread(_dir_stats, plugin.cache_dir)
        history = await asyncio.to_thread(plugin.history.stats)

        active_login = any(
            not task.done() for task in plugin._bili_login_tasks.values()
        )

        return _json_response(
            {
                "version": _plugin_version(),
                "platforms": platforms,
                "enabled_count": sum(1 for item in platforms if item["enabled"]),
                "disabled_platforms": pconfig.DISABLED_PLATFORMS,
                "render": {
                    "enabled": plugin._renderer.enabled,
                    "theme": plugin._renderer.theme_name,
                    "layout": plugin._renderer.layout_name,
                    "width": plugin._renderer.width,
                    "cover_full_size": plugin._renderer.cover_full_size,
                },
                "screenshot": {
                    "backend": plugin.screenshot.backend,
                    "configured": plugin.screenshot.is_configured,
                    "fallback": plugin._screenshot_fallback,
                },
                "bili": {
                    "configured": bool(plugin._bili_cookie),
                    # 前端总览页读它显示「已配置（N 字符）」。这个字段原先只在
                    # bili/status 里给过，这里漏了 → 总览恒显「已配置（0 字符）」。
                    "cookie_length": len(plugin._bili_cookie or ""),
                    "quality": pconfig.BILI_QUALITY,
                    "login_active": active_login,
                },
                "cache": {
                    "files": files,
                    "size_bytes": size,
                    "size_text": fmt_size_bytes(size),
                    "dir": str(plugin.cache_dir),
                    "source": getattr(plugin, "cache_dir_source", "default"),
                    "ttl_hours": pconfig.CACHE_TTL_HOURS,
                },
                "media": {
                    "docker": is_docker_environment(),
                    "cache_dir": str(plugin.cache_dir),
                    "cache_dir_source": getattr(plugin, "cache_dir_source", "default"),
                    "shared_dir_configured": bool(pconfig.CACHE_DIR),
                    "relay_enabled": pconfig.MEDIA_RELAY_ENABLED,
                    "relay_callback": pconfig.MEDIA_RELAY_CALLBACK_URL
                    or str(_astrbot_callback_base()),
                    "relay_callback_from_config": bool(pconfig.MEDIA_RELAY_CALLBACK_URL),
                    "relay_ttl": pconfig.MEDIA_RELAY_TTL,
                },
                "history": history,
                "memory": {
                    "result_cache": len(plugin._result_cache),
                    "render_cache": len(plugin._render_cache),
                },
                "send_error_messages": plugin._send_errors,
                "data_dir": str(plugin.cache_dir.parent),
            }
        )

    # ==================== 配置 ==================== #

    async def get_config(self):
        pconfig = get_config()
        payload = config_meta_payload()
        payload["values"], payload["secret_status"] = self._public_config(pconfig)
        payload["platforms"] = [
            {"name": name, "label": PLATFORM_DISPLAY_NAMES.get(name, name)}
            for name in platform_order()
        ]
        payload["problems"] = self._schema_problems()
        return _json_response(payload)

    @staticmethod
    def _public_config(pconfig):
        from .credentials import public_config_values
        return public_config_values(pconfig)

    def _credential_manager(self):
        from .credentials import CredentialManager
        if self._credentials_manager is None:
            self._credentials_manager = getattr(self.plugin, '_credentials_manager', None) or CredentialManager(self.plugin)
            self.plugin._credentials_manager = self._credentials_manager
        return self._credentials_manager

    def _cookiecloud_service(self):
        from .credentials.sync import service_for
        self._credential_manager()
        return service_for(self.plugin)

    async def cookiecloud_status(self):
        return _json_response(self._cookiecloud_service().snapshot())

    async def cookiecloud_sync(self):
        from .credentials.cookiecloud import CookieCloudError
        body = await get_json_body()
        if not isinstance(body, dict) or not isinstance(body.get('overwrite', False), bool):
            return _error('同步参数无效')
        try:
            payload = await self._cookiecloud_service().sync(overwrite=body.get('overwrite', False))
        except CookieCloudError as error:
            return _error(str(error), 422)
        return _json_response(payload)

    async def diagnostics(self):
        from .diagnostics import store_for
        return _json_response(store_for(self.plugin).snapshot())

    async def export_diagnostic(self):
        from .diagnostics import store_for
        body = await get_json_body()
        identifier = body.get('id') if isinstance(body, dict) else None
        if not isinstance(identifier, str) or len(identifier) > 64:
            return _error('请选择一条诊断记录')
        report = store_for(self.plugin).export(identifier)
        if report is None:
            return _error('诊断记录已过期，请重新解析', 404)
        return _json_response({'report': report})

    async def credentials(self):
        return _json_response(self._credential_manager().snapshot())

    async def _credential_action(self, action):
        from .credentials import CredentialError, CredentialImportError
        body = await get_json_body()
        if not isinstance(body, dict) or not isinstance(body.get("platform"), str):
            return _error("请选择凭据平台")
        try:
            manager = self._credential_manager()
            if action == "import":
                content = body.get("content")
                fmt = body.get("format", "auto")
                if not isinstance(content, str) or not isinstance(fmt, str):
                    return _error("导入内容必须是文本")
                result = await manager.import_and_save(body["platform"], content, fmt)
            elif action == "check":
                result = await manager.check(body["platform"])
            else:
                result = await manager.clear(body["platform"])
            return _json_response(result)
        except (CredentialError, CredentialImportError) as exc:
            return _error(str(exc))
        except Exception:
            # 不回显第三方异常，避免其中的 Cookie、Token 或请求头进入页面。
            logger.warning("[denia_share] 凭据操作未完成")
            return _error("凭据操作未完成，请检查配置存储或稍后重试", 500)

    async def import_credential(self):
        return await self._credential_action("import")

    async def check_credential(self):
        return await self._credential_action("check")

    async def clear_credential(self):
        return await self._credential_action("clear")

    async def _music_login_action(self, action):
        body = await get_json_body()
        if not isinstance(body, dict):
            return _error("请求格式不正确")
        if action == "poll":
            value = body.get("task_id")
            if not isinstance(value, str) or not 1 <= len(value) <= 100:
                return _error("扫码会话无效，请重新获取二维码")
        else:
            value = body.get("platform")
            if value not in {"netease", "qqmusic"}:
                return _error("请选择 QQ音乐或网易云音乐")
        try:
            manager = self.plugin._get_music_controller().login
            result = await getattr(manager, action)(value)
            # No private credential or session signature is returned by the manager.
            allowed = {"task_id", "state", "message", "qr_data_url", "nickname"}
            public = {key: val for key, val in result.items() if key in allowed}
            return _json_response(public)
        except MusicError as exc:
            logger.warning("[denia_share] 音乐登录操作未完成: action=%s error=%s", action, type(exc).__name__)
            return _error(str(exc))
        except Exception as exc:
            logger.warning("[denia_share] 音乐登录操作失败: action=%s error=%s", action, type(exc).__name__)
            return _error("音乐登录操作未完成，请重试或使用 Cookie 导入", 400)

    async def music_login_start(self):
        return await self._music_login_action("start")

    async def music_login_poll(self):
        return await self._music_login_action("poll")

    async def music_login_refresh(self):
        return await self._music_login_action("refresh")

    async def save_config(self):
        body = await get_json_body()
        values = body.get("values") if isinstance(body, dict) else None
        if not isinstance(values, dict) or not values:
            return _error("没有需要保存的配置项")

        pconfig = get_config()
        changed, errors = pconfig.apply_updates(values)
        runtime: dict[str, Any] = {}
        runtime_ok = True
        if changed:
            try:
                runtime = await self.plugin.apply_runtime_config()
            except Exception as exc:
                runtime_ok = False
                logger.warning("[denia_share] 应用新配置失败", exc_info=True)
                errors.append("配置已保存，但运行时热更新失败，请检查 AstrBot 日志")
        return _json_response(
            {
                "changed": changed,
                "errors": errors,
                "runtime_ok": runtime_ok,
                "runtime": runtime,
                "values": self._public_config(pconfig)[0],
                "secret_status": self._public_config(pconfig)[1],
            }
        )

    async def reset_config(self):
        pconfig = get_config()
        changed = pconfig.reset_to_defaults()
        errors: list[str] = []
        # reset_to_defaults 只返回变更键；显式保存以检查落盘失败，也支持原值重试。
        if not pconfig.save():
            errors.append("默认配置未能写入磁盘，请检查存储后重试")
        # 「恢复默认」的确认文案承诺了「包括 Cookie、Token」—— 而 B站扫码登录态
        # 不在配置项里（它有自己的 bili_cookie.json），只重置配置清不掉它。
        # bili_logout 本来就会把三处一起清，只是一直没人调它。
        try:
            if await self.plugin.bili_logout() is False:
                errors.append("B站登录态未完全清除，请重试")
        except Exception:
            logger.warning("[denia_share] 恢复默认时清除 B站登录态失败", exc_info=True)
            errors.append("B站登录态未完全清除，请重试")
        if hasattr(self.plugin, "_get_music_controller"):
            # 两个平台各自独立清理：任一平台失败都不能跳过另一个平台，
            # 也不能提前 return 掉后面的运行时热更新（否则配置已重置但内存里还是旧值）。
            for platform in ("netease", "qqmusic"):
                try:
                    login = self.plugin._get_music_controller().login
                    await login.clear(platform)
                except Exception:
                    logger.warning(
                        "[denia_share] 恢复默认时清除音乐登录态失败: %s",
                        platform,
                        exc_info=True,
                    )
                    label = "网易云音乐" if platform == "netease" else "QQ音乐"
                    errors.append(f"{label}登录态未能清除，请重试")
        runtime: dict[str, Any] = {}
        runtime_ok = True
        # 无论本次是否真的有配置项变化都要应用运行时配置：
        # 重试请求（changed 为空）也可能带着上一次失败留下的待生效状态。
        try:
            applied = await self.plugin.apply_runtime_config()
            if isinstance(applied, dict):
                runtime = applied
            if applied is False or (
                isinstance(applied, dict) and applied.get("runtime_ok") is False
            ):
                runtime_ok = False
                errors.append("已恢复默认，但运行时热更新失败，请检查 AstrBot 日志")
        except Exception:
            runtime_ok = False
            logger.warning("[denia_share] 恢复默认配置后热更新失败", exc_info=True)
            errors.append("已恢复默认，但运行时热更新失败，请检查 AstrBot 日志")
        return _json_response(
            {
                "changed": changed,
                "errors": errors,
                "runtime_ok": runtime_ok,
                "runtime": runtime,
                "values": self._public_config(pconfig)[0],
                "secret_status": self._public_config(pconfig)[1],
            }
        )

    async def toggle_platform(self):
        body = await get_json_body()
        name = str(body.get("name") or "").strip().lower()
        enabled = bool(body.get("enabled"))
        if name not in PLATFORM_DISPLAY_NAMES:
            return _error("未知平台")

        pconfig = get_config()
        disabled = [item for item in pconfig.DISABLED_PLATFORMS if item != name]
        if not enabled:
            disabled.append(name)
        # 保持 platform_order() 的顺序，读起来稳定
        disabled = [item for item in platform_order() if item in disabled]

        changed, errors = pconfig.apply_updates(
            {"DISABLED_PLATFORMS": ",".join(disabled)}
        )
        runtime: dict[str, Any] = {}
        if changed:
            runtime = await self.plugin.apply_runtime_config()
        else:
            # 没变化也要给一份 runtime：前端用 result.runtime.platforms 判断
            # 「这个平台现在还是不是启用的」，字段缺失时前端会把该平台显示成禁用
            # （后端明明什么都没改）。这里不调 apply_runtime_config —— 它带副作用
            # （重建解析器/渲染器），没变化时不该跑。
            runtime = {"rebuilt": False, "platforms": sorted(self.plugin.parsers)}
        return _json_response(
            {
                "changed": changed,
                "errors": errors,
                "runtime": runtime,
                "disabled_platforms": get_config().DISABLED_PLATFORMS,
                "enabled": name in self.plugin.parsers,
            }
        )

    # ==================== 自定义解析器 ==================== #

    def _custom_parser_payload(self) -> dict[str, Any]:
        """把加载结果整理成页面需要的结构。

        刻意把 ``errors`` 一并返回：用户写错了文件，唯一的反馈渠道就是这个页面
        （日志在服务器上，写自定义解析器的人未必看得到）。
        """
        from .custom_parsers import API_VERSION, MAX_FILE_BYTES, TEMPLATE_NAME

        plugin = self.plugin
        loader = getattr(plugin, "_custom_loader", None)
        result = getattr(plugin, "_custom_result", None)
        directory = (
            loader.directory
            if loader is not None
            else plugin._data_dir / "custom_parsers"
        )

        entries = []
        for entry in (result.entries if result else []):
            entries.append(
                {
                    "file": entry.file,
                    "key": entry.key,
                    "label": entry.display_name,
                    "keywords": list(entry.keywords),
                    "enabled": not entry.disabled and entry.parser is not None,
                }
            )
        errors = [
            {"file": item.file, "reason": item.reason}
            for item in (result.errors if result else [])
        ]
        return {
            "dir": str(directory),
            "api_version": API_VERSION,
            "max_file_kb": MAX_FILE_BYTES // 1024,
            "template": TEMPLATE_NAME,
            "entries": entries,
            "errors": errors,
        }

    async def custom_parsers(self):
        return _json_response(self._custom_parser_payload())

    async def reload_custom_parsers(self):
        """重新扫描目录并立刻生效（不需要重载插件）。

        这里**不用 ``to_thread``**：加载过程会写全局平台注册表（``PLATFORMS`` /
        ``platform_order()``）与解析器表，跨线程改这些只会引入难查的中间态。
        扫描与 exec 都是毫秒级，且这是用户手动点的低频操作。
        """
        try:
            self.plugin.reload_custom_parsers()
        except Exception as error:  # noqa: BLE001 —— 兜底，加载器本身不该抛
            logger.warning("[denia_share] 重新加载自定义解析器失败", exc_info=True)
            return _error(f"重新加载失败：{type(error).__name__}: {error}")
        payload = self._custom_parser_payload()
        payload["platforms"] = sorted(self.plugin.parsers)
        return _json_response(payload)

    # ==================== 手动解析 ==================== #

    async def parse_url(self):
        from .diagnostics import store_for
        diagnostic = store_for(self.plugin).start('webui')
        try:
            return await self._parse_url(diagnostic)
        except Exception:
            diagnostic.stage('parse', 'error', 'parse_failed')
            diagnostic.finish('error')
            logger.warning('[denia_share] 手动解析未完成', exc_info=True)
            return _error('解析未完成，请查看解析诊断或稍后重试', 500)
        finally:
            if diagnostic.status == 'running':
                diagnostic.finish('unknown')

    async def _parse_url(self, diagnostic):
        body = await get_json_body()
        if not isinstance(body, dict):
            diagnostic.stage('match', 'error', 'invalid_link')
            diagnostic.finish('error')
            return _error('请填写要解析的链接或分享口令')
        # 与聊天链路对齐：口令 / 整段分享文本直接整段交给解析器匹配，不再要求
        # http(s) 开头。聊天侧 _process_url 就是把整条 message_str 传给
        # parser.search_url（各解析器自己的 pattern 负责从文本里摘链接），所以
        # 「4.33 a@a.AG ... https://v.douyin.com/xxx/ 复制此链接…」这类口令在群里
        # 能解析，在这里也必须能。
        #
        # **缓存键要用 `searched.group(0)`（解析器实际匹配到的那一段），不是整段文本。**
        # 聊天侧现在就是这么算的（见 main._process_url），两边算法必须一致 ——
        # 否则同一条分享在网页里解析一次、群里再解析一次，各存一份缓存。
        url = str(body.get("url") or "").strip()
        if not url:
            diagnostic.stage('match', 'error', 'invalid_link')
            diagnostic.finish('error')
            return _error("请填写要解析的链接或分享口令")
        if len(url) > MAX_URL_LENGTH:
            diagnostic.stage('match', 'error', 'invalid_link')
            diagnostic.finish('error')
            return _error("内容过长")

        plugin = self.plugin
        name = plugin._match_parser(url)
        if name is None:
            diagnostic.stage('match', 'error', 'not_supported')
            diagnostic.finish('error')
            return _error("没有平台规则能匹配这段内容；可以改用「网页截图」试试", 422)
        from .diagnostics import _safe_platform
        diagnostic.platform = _safe_platform(name)
        diagnostic.stage('match', 'ok')
        parser = plugin.parsers.get(name)
        if parser is None:
            return _error(f"{PLATFORM_DISPLAY_NAMES.get(name, name)} 平台当前是禁用状态", 409)

        started = time.perf_counter()
        try:
            diagnostic.stage('parse', 'running')
            keyword, searched = parser.search_url(url)
            result = await parser.parse(keyword, searched)
            diagnostic.stage('parse', 'ok')
        except IgnoreException as exc:
            diagnostic.stage('parse', 'skipped', 'skipped')
            diagnostic.finish('skipped')
            return _error(f"已忽略：{exc.message}", 422)
        except ParseException as exc:
            diagnostic.stage('parse', 'error', 'parse_failed')
            diagnostic.finish('error')
            return _error(f"解析失败：{exc.message}", 422)
        except Exception as exc:
            diagnostic.stage('parse', 'error', 'network' if isinstance(exc, httpx.RequestError) else 'parse_failed')
            diagnostic.finish('error')
            logger.exception("[denia_share] WebUI 手动解析出错")
            return _error('解析出错，请查看解析诊断或稍后重试', 500)

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        # 与聊天链路用**同一个键算法**：解析器匹配到的那一段，而不是整段文本。
        # 也必须走 _remember_result —— 直接写字典会绕过 MAX_RESULT_CACHE_ENTRIES
        # 上限，只用网页解析的部署里这个缓存会无界增长。
        cache_key = plugin.result_cache_key(searched.group(0) or url)
        plugin._remember_result(cache_key, result)

        payload = await self._preview_payload(
            result,
            cache_key,
            elapsed_ms=elapsed_ms,
            overrides=body.get("preview"),
            record=True,
            diagnostic=diagnostic,
        )
        payload['diagnostic_id'] = diagnostic.id
        return _json_response(payload)

    async def render_cached(self):
        """用内存里缓存的解析结果，按指定外观重新渲染（不重新联网解析）。"""
        body = await get_json_body()
        cache_key = str(body.get("cache_key") or "").strip()
        if not cache_key:
            return _error("缺少 cache_key")
        # **必须走 _get_cached_result，不能直接 .get()**：缓存值是
        # (写入时刻, 结果) 元组，直接取会拿到元组、当成 ParseResult 往下传就崩。
        # 而且「过了重复解析间隔 / 媒体文件已被清理」这两种失效只有它知道 ——
        # 直接 .get() 会让「解析结果已过期」这个 410 永远不触发。
        result = self.plugin._get_cached_result(cache_key)
        if result is None:
            return _error(
                "解析结果已不可用（插件重启、已过重复解析间隔、缓存被清理，"
                "或媒体文件已删除），请重新解析一次",
                410,
            )

        payload = await self._preview_payload(
            result,
            cache_key,
            elapsed_ms=None,
            overrides=body.get("preview"),
            record=False,
        )
        return _json_response(payload)

    async def _preview_payload(
        self,
        result: Any,
        cache_key: str,
        *,
        elapsed_ms: int | None,
        overrides: Any = None,
        record: bool,
        diagnostic=None,
    ) -> dict[str, Any]:
        """把 ParseResult 整理成页面需要的预览结构。"""
        if diagnostic is None:
            from .diagnostics import store_for
            diagnostic = store_for(self.plugin).start('webui', result.platform.name)
            diagnostic.stage('parse', 'ok', 'cached')
        # 手动预览只获取卡片用图，与聊天输出选项独立；不下载视频或音频本体。
        # 先审计再渲染，让本轮图片限制提示进入卡片缓存键。
        try:
            missing = await result.audit_missing_media(img_only=True)
            diagnostic.stage('media', 'error' if missing else 'ok', 'media_missing' if missing else '')
        except Exception:
            diagnostic.stage('media', 'error', 'media_missing')
            logger.warning("[denia_share] WebUI 媒体结算失败", exc_info=True)

        card = await self._render_card(result, cache_key, overrides)
        enabled = bool(getattr(self.plugin._renderer, 'enabled', False))
        diagnostic.stage('render', 'ok' if card else 'error' if enabled else 'skipped',
            '' if card else 'render_failed' if enabled else 'skipped')
        diagnostic.stage('send', 'skipped', 'preview_only')
        diagnostic.finish('error' if any(s['state'] == 'error' for s in diagnostic.to_dict()['stages']) else 'ok')

        record_id = ""
        if record:
            saved = await self.plugin._record_history(
                result, Path(card["path"]) if card else None,
                via="webui", elapsed_ms=elapsed_ms,
            )
            record_id = saved.id if saved else ""

        payload: dict[str, Any] = {
            "cache_key": cache_key,
            "record_id": record_id,
            "platform": result.platform.name,
            "platform_display": result.platform.display_name,
            "content_type": result.content_type,
            "title": result.title or "",
            "author": result.author.name if result.author else "",
            "url": result.url or "",
            "text": (result.text or "")[:1000],
            "detail": self.plugin._history_detail(result),
            "elapsed_ms": elapsed_ms,
            "card": None,
        }
        if card is not None:
            payload["card"] = {
                "file": card["file"],
                "data_url": card["data_url"],
                "theme": card["theme"],
                "layout": card["layout"],
                "width": card["width"],
            }
        return payload

    async def _render_card(
        self, result: Any, cache_key: str, overrides: Any = None
    ) -> dict[str, Any] | None:
        """渲染卡片并生成预览图。

        ``overrides`` 非空时用一份临时渲染器（只预览、不写进插件缓存），
        这样「换主题看看效果」不需要改动用户的实际配置。
        """
        renderer = self.plugin._renderer
        temporary = False
        if isinstance(overrides, dict) and overrides:
            try:
                renderer = self._build_renderer(overrides)
            except PreviewOverridesError as exc:
                # 解析链路里卡片只是附带产物，参数填错不该把整条解析判死，
                # 记日志后按当前配置出图（页面会另外提示参数无效）。
                logger.warning("[denia_share] 预览覆盖参数无效，回退当前配置：%s", exc)
                renderer = self.plugin._renderer
            else:
                temporary = True

        if renderer is None or not renderer.enabled:
            return None
        try:
            path = await renderer.render(result, cache_key=cache_key)
        except Exception:
            logger.warning("[denia_share] WebUI 渲染卡片失败", exc_info=True)
            return None

        if path is None:
            return None
        if not temporary:
            self.plugin._remember_render(cache_key, path)

        data_url = await asyncio.to_thread(_image_data_url_cached, path)
        if data_url is None:
            return None
        return {
            "path": str(path),
            "file": path.name,
            "data_url": data_url,
            "theme": renderer.theme_name,
            "layout": renderer.layout_name,
            "width": renderer.width,
        }

    def _build_renderer(self, overrides: dict[str, Any]) -> ShareCardRenderer:
        """按预览参数构造临时渲染器。

        以当前配置为底，仅覆盖预览里显式给出的外观项，这样「换个颜色看看」
        不需要改动用户的实际配置。

        参数不合法时抛 ``PreviewOverridesError``——调用方必须显式处理，
        不能当成「没有覆盖项」静默回退。
        """
        pconfig = get_config()
        opts = pconfig.renderer_options()
        opts["enabled"] = True

        theme = str(overrides.get("theme") or opts["theme"]).strip().lower()
        layout = str(overrides.get("layout") or opts["layout"]).strip().lower()
        if theme not in {"dark", "light"}:
            raise PreviewOverridesError(f"主题只能是 dark 或 light，收到 {theme!r}")
        if layout not in LAYOUT_NAMES:
            raise PreviewOverridesError(
                f"布局只能是 {'/'.join(sorted(LAYOUT_NAMES))}，收到 {layout!r}"
            )
        opts["theme"] = theme
        opts["layout"] = layout

        if "width" in overrides and overrides.get("width") is not None:
            try:
                opts["width"] = max(520, min(1080, int(overrides.get("width"))))
            except (TypeError, ValueError):
                raise PreviewOverridesError(
                    f"宽度需要是数字，收到 {overrides.get('width')!r}"
                ) from None
        if isinstance(overrides.get("cover_full_size"), bool):
            opts["cover_full_size"] = overrides["cover_full_size"]
        if isinstance(overrides.get("show_avatar"), bool):
            opts["show_avatar"] = overrides["show_avatar"]
        if isinstance(overrides.get("show_play_button"), bool):
            opts["show_play_button"] = overrides["show_play_button"]
        if isinstance(overrides.get("show_ai_summary"), bool):
            opts["show_ai_summary"] = overrides["show_ai_summary"]
        if overrides.get("desc_max_lines") is not None:
            try:
                opts["desc_max_lines"] = max(0, min(12, int(overrides.get("desc_max_lines"))))
            except (TypeError, ValueError):
                raise PreviewOverridesError(
                    f"正文行数需要是数字，收到 {overrides.get('desc_max_lines')!r}"
                ) from None
        for key in ("accent_color", "gradient_top", "gradient_bottom"):
            if key in overrides:
                raw = overrides.get(key)
                if raw in (None, ""):
                    opts[key] = None
                    continue
                from .card_renderer import normalize_hex_color

                normalized = normalize_hex_color(str(raw))
                if normalized is None:
                    raise PreviewOverridesError(
                        f"{key} 需要是 #RRGGBB 形式的颜色，收到 {raw!r}"
                    )
                opts[key] = normalized
        if "watermark" in overrides:
            opts["watermark"] = str(overrides.get("watermark") or "").strip()[:12]

        try:
            return ShareCardRenderer(self.plugin.cache_dir, **_renderer_kwargs(opts))
        except Exception:
            logger.warning("[denia_share] 构造临时渲染器失败", exc_info=True)
            raise PreviewOverridesError("这些外观参数无法生成渲染器") from None

    # ==================== 离线示例预览 ==================== #

    _SAMPLE_LOCK: asyncio.Lock | None = None

    async def sample_preview(self):
        """用内置示例数据渲染一张卡片预览（不联网，不写解析记录）。

        供「外观」页实时预览：改一个参数立刻看到效果，无需真的去解析一条链接。
        """
        body = await get_json_body()
        overrides = body.get("preview")
        overrides = overrides if isinstance(overrides, dict) else {}

        renderer = None
        if overrides:
            try:
                renderer = self._build_renderer(overrides)
            except PreviewOverridesError as exc:
                # 预览的全部意义就是「所见即所填」，参数非法必须显式报错，
                # 不能悄悄按当前配置出图。
                return _error(str(exc))
        if renderer is None:
            # 没有覆盖项时直接用常驻渲染器；没开渲染时也要能给预览（强制启用）
            renderer = self.plugin._renderer
            if renderer is None or not renderer.enabled:
                opts = get_config().renderer_options()
                opts["enabled"] = True
                renderer = ShareCardRenderer(self.plugin.cache_dir, **_renderer_kwargs(opts))

        result = self.plugin.build_sample_result()
        # 示例渲染不进入渲染缓存，也不污染记录；文件名带外观指纹自然去重
        try:
            if WebUIApi._SAMPLE_LOCK is None:
                WebUIApi._SAMPLE_LOCK = asyncio.Lock()
            async with WebUIApi._SAMPLE_LOCK:
                path = await renderer.render(result, cache_key=None)
        except Exception:
            logger.warning("[denia_share] 示例卡片预览失败", exc_info=True)
            return _error("示例卡片渲染失败，请检查字体配置", 500)
        if path is None:
            return _error("示例卡片渲染失败，请检查字体配置", 500)

        data_url = await asyncio.to_thread(_image_data_url_cached, path, 720)
        if data_url is None:
            return _error("预览图生成失败", 500)
        return _json_response(
            {
                "data_url": data_url,
                "theme": renderer.theme_name,
                "layout": renderer.layout_name,
                "width": renderer.width,
                "file": path.name,
            }
        )

    # ==================== 界面外观偏好 ==================== #

    _APPEARANCE_KEYS = {
        "accent": (str, ""),
        "accent_custom": (str, ""),
        "radius": (str, "m"),
        "compact": (bool, False),
        "animations": (bool, True),
        "theme_override": (str, "follow"),
    }

    def _appearance_path(self) -> Path:
        return self.plugin.history.path.parent / "webui_appearance.json"

    async def get_appearance(self):
        prefs = await asyncio.to_thread(self._read_appearance)
        return _json_response({"prefs": prefs})

    def _read_appearance(self) -> dict[str, Any]:
        path = self._appearance_path()
        try:
            import json

            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        prefs: dict[str, Any] = {}
        for key, (kind, default) in self._APPEARANCE_KEYS.items():
            value = raw.get(key, default)
            if kind is bool:
                prefs[key] = bool(value)
            else:
                value = str(value or "").strip()
                # 白名单收敛，避免把脏数据写进 CSS 变量
                if key == "radius" and value not in {"s", "m", "l"}:
                    value = "m"
                if key == "theme_override" and value not in {"follow", "light", "dark"}:
                    value = "follow"
                if key in {"accent", "accent_custom"}:
                    import re as _re

                    if value and not _re.match(r"^#[0-9a-fA-F]{6}$", value):
                        value = ""
                prefs[key] = value
        return prefs

    async def save_appearance(self):
        body = await get_json_body()
        prefs = body.get("prefs") if isinstance(body, dict) else None
        if not isinstance(prefs, dict):
            return _error("缺少 prefs 对象")
        # 只接受白名单键，值在 _read_appearance 回读时还会再校验一次
        cleaned = {k: prefs[k] for k in self._APPEARANCE_KEYS if k in prefs}
        try:
            await asyncio.to_thread(self._write_appearance, cleaned)
        except OSError as exc:
            logger.warning("[denia_share] 外观偏好写入失败", exc_info=True)
            return _error(f"写入失败：{str(exc)[:120]}", 500)
        return _json_response({"prefs": self._read_appearance()})

    def _write_appearance(self, prefs: dict[str, Any]) -> None:
        import json
        import os

        path = self._appearance_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        merged = self._read_appearance()
        merged.update(prefs)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    # ==================== 网页截图 ==================== #

    async def screenshot(self):
        body = await get_json_body()
        url = str(body.get("url") or "").strip()
        if not url:
            return _error("请填写要截图的网址")
        if len(url) > MAX_URL_LENGTH:
            return _error("网址过长")
        if not is_probably_screenshotable(url):
            return _error("这个地址不支持截图（本地地址或非法主机名）")

        service = self.plugin.screenshot
        if not service.is_configured:
            return _error("当前截图后端未配置：使用 Cloudflare 时请先填 Account ID 与 API Token", 409)

        path = await service.capture(url)
        if path is None:
            return _error(f"截图失败：{service.last_error or '未知原因'}", 502)

        data_url = await asyncio.to_thread(_image_data_url_cached, path, 720)
        return _json_response(
            {
                "url": url,
                "backend": service.backend,
                "file": path.name,
                "size_text": fmt_size_bytes(path.stat().st_size),
                "data_url": data_url,
            }
        )

    # ==================== 解析记录 / 缓存 ==================== #

    async def list_cache(self):
        keyword = query_arg("keyword").strip()
        platform = query_arg("platform").strip().lower()
        via = query_arg("via").strip().lower()

        def _query_int(name: str, default: int, upper: int) -> int:
            """query 参数转 int，非法值回退默认（不依赖框架 type=int 的失败语义）。"""
            raw = query_arg(name)
            if not raw:
                return default
            try:
                return max(0 if name == "offset" else 1, min(upper, int(raw)))
            except (TypeError, ValueError):
                return default

        limit = _query_int("limit", 20, MAX_QUERY_LIMIT)
        offset = _query_int("offset", 0, 10_000)

        store = self.plugin.history
        items, total = await asyncio.to_thread(
            store.query,
            keyword=keyword,
            platform=platform,
            via=via,
            limit=limit,
            offset=offset,
        )
        stats = await asyncio.to_thread(store.stats)
        cache_dir = self.plugin.cache_dir

        def _assemble_records():
            """逐条检查卡片/媒体文件是否还在盘上（含 resolve 磁盘 IO），放线程里做。"""
            assembled = []
            for record in items:
                data = record.to_dict()
                card_exists = False
                if record.card_file:
                    target = _safe_under(cache_dir, Path(record.card_file))
                    card_exists = bool(target and target.is_file())
                data["card_exists"] = card_exists
                data["media_existing"] = sum(
                    1
                    for name in record.media_files
                    if (target := _safe_under(cache_dir, Path(name))) and target.is_file()
                )
                assembled.append(data)
            return assembled

        records = await asyncio.to_thread(_assemble_records)

        files, size = await asyncio.to_thread(_dir_stats, cache_dir)
        return _json_response(
            {
                "items": records,
                "total": total,
                "limit": limit,
                "offset": offset,
                "stats": stats,
                "cache": {
                    "files": files,
                    "size_bytes": size,
                    "size_text": fmt_size_bytes(size),
                    "dir": str(cache_dir),
                    "ttl_hours": get_config().CACHE_TTL_HOURS,
                },
                "platforms": [
                    {"name": name, "label": PLATFORM_DISPLAY_NAMES.get(name, name)}
                    for name in platform_order()
                ],
            }
        )

    async def delete_cache(self):
        body = await get_json_body()
        raw_ids = body.get("ids")
        if isinstance(raw_ids, str):
            raw_ids = [raw_ids]
        if not isinstance(raw_ids, list) or not raw_ids:
            return _error("请提供要删除的记录 id")
        if len(raw_ids) > 200:
            return _error("一次最多删除 200 条")
        ids = [str(item) for item in raw_ids]
        purge = bool(body.get("purge_files"))

        store = self.plugin.history
        removed_files = 0
        if purge:
            removed_files = await asyncio.to_thread(self._purge_files, ids)
            if removed_files:
                # 卡片文件没了，缓存里对应的 base64 也没用了（键里的
                # mtime/size 变了自然不会命中，但条目会白占预算直到被淘汰）
                clear_image_cache()
        removed = await asyncio.to_thread(store.delete, ids)
        return _json_response(
            {"removed": removed, "removed_files": removed_files, "ids": ids}
        )

    async def clear_cache(self):
        body = await get_json_body()
        scope = str(body.get("scope") or "all").strip().lower()
        if scope not in {"records", "files", "all"}:
            return _error("scope 只能是 records / files / all")

        result: dict[str, Any] = {"scope": scope, "removed_records": 0, "removed_files": 0}

        if scope in {"records", "all"}:
            result["removed_records"] = await asyncio.to_thread(
                self.plugin.history.clear
            )
        if scope in {"files", "all"}:
            try:
                result["removed_files"] = await clear_cache_dir(self.plugin.cache_dir)
            except Exception as exc:
                logger.warning("[denia_share] 清空缓存目录失败", exc_info=True)
                return _error(f"清空缓存文件失败：{str(exc)[:160]}", 500)
            self.plugin._result_cache.clear()
            self.plugin._render_cache.clear()
            # 去重窗口也要清：窗口里的「只解析第一个」是基于上一次解析建立的，
            # 缓存都清空了还锁着，后来者会被无故静默掉。main.py 的四处清理
            # 同样都清，缺这一处就等于破坏了那条不变量。
            self.plugin._link_dedup.clear()
            # 卡片文件被清空，缓存里的 base64 全部指向不存在的文件
            clear_image_cache()

        files, size = await asyncio.to_thread(_dir_stats, self.plugin.cache_dir)
        result["cache"] = {"files": files, "size_bytes": size, "size_text": fmt_size_bytes(size)}
        return _json_response(result)

    async def cleanup_cache(self):
        """按 CACHE_TTL_HOURS 清理过期的缓存文件（不删除解析记录）。"""
        body = await get_json_body()
        ttl = body.get("ttl_hours")
        if ttl is None:
            ttl = get_config().CACHE_TTL_HOURS
        try:
            ttl = int(ttl)
        except (TypeError, ValueError):
            return _error("ttl_hours 需要是整数")
        if ttl <= 0:
            return _error("ttl_hours 需要大于 0（0 表示不自动清理）")

        removed = await cleanup_cache_dir(self.plugin.cache_dir, ttl_hours=ttl)
        if removed:
            # 删掉的文件可能正是内存缓存里记着的本地路径 —— 不清的话，之后命中缓存
            # 会把**已删除的路径**交给协议端（到发送时才发现文件没了）。
            # 与 clear_cache(scope=files) 保持一致，别只清一半。
            self.plugin._result_cache.clear()
            self.plugin._render_cache.clear()
            self.plugin._link_dedup.clear()
            clear_image_cache()
        files, size = await asyncio.to_thread(_dir_stats, self.plugin.cache_dir)
        return _json_response(
            {
                "ttl_hours": ttl,
                "removed_files": removed,
                "cache": {"files": files, "size_bytes": size, "size_text": fmt_size_bytes(size)},
            }
        )

    async def cache_thumbnail(self):
        record = await self._record_or_error()
        if not isinstance(record, dict):
            return record

        path = self._record_card_path(record)
        if path is None:
            return _error("卡片文件已被清理", 404)
        data_url = await asyncio.to_thread(_image_data_url_cached, path, 900)
        if data_url is None:
            return _error("缩略图生成失败", 500)
        return _json_response({"id": record["id"], "data_url": data_url})

    async def cache_download(self):
        record = await self._record_or_error()
        if not isinstance(record, dict):
            return record
        path = self._record_card_path(record)
        if path is None:
            return _error("卡片文件已被清理", 404)
        safe_name = f"denia_card_{record['id']}{path.suffix or '.png'}"
        return file_response(
            path,
            filename=safe_name,
            content_type="image/png" if path.suffix.lower() == ".png" else None,
        )

    async def _record_or_error(self):
        """按 id 取记录；出错时直接返回响应对象。"""
        record_id = query_arg("id").strip()
        if not record_id:
            return _error("缺少记录 id")

        record = await asyncio.to_thread(self.plugin.history.get, record_id)
        if record is None:
            return _error("记录不存在", 404)
        return record.to_dict()

    def _record_card_path(self, record: dict[str, Any]) -> Path | None:
        card_file = record.get("card_file")
        if not card_file:
            return None
        target = _safe_under(self.plugin.cache_dir, Path(str(card_file)))
        if target is None or not target.is_file():
            return None
        return target

    def _purge_files(self, record_ids: Iterable[str]) -> int:
        """删除记录引用的卡片与媒体文件，返回删除的文件数。"""
        store = self.plugin.history
        removed = 0
        for record_id in record_ids:
            record = store.get(record_id)
            if record is None:
                continue
            names = [record.card_file] if record.card_file else []
            names.extend(record.media_files)
            for name in names:
                if not name:
                    continue
                target = _safe_under(self.plugin.cache_dir, Path(str(name)))
                if target is None or not target.is_file():
                    continue
                try:
                    target.unlink()
                    removed += 1
                except OSError as exc:
                    # 静默 continue 会让「一个文件都没删掉」看起来像成功
                    logger.warning(f"[denia_share] 删除缓存文件失败: {target} ({exc})")
                    continue
        # 渲染缓存按 cache_key 索引、存的是路径，删了文件后难以精确对应，
        # 直接整体清掉：代价只是下次重新渲染一次。
        if removed:
            self.plugin._render_cache.clear()
        return removed

    # ==================== B站登录 ==================== #

    async def bili_status(self):
        plugin = self.plugin
        pconfig = get_config()
        check = query_arg("check").strip() in {"1", "true", "yes"}
        payload: dict[str, Any] = {
            "configured": bool(plugin._bili_cookie),
            "quality": pconfig.BILI_QUALITY,
            "cookie_length": len(plugin._bili_cookie or ""),
            "login_active": any(
                not task.done() for task in plugin._bili_login_tasks.values()
            ),
            "has_config_cookie": bool(str(pconfig.BILI_CK or "").strip()),
            "valid": None,
            "username": "",
            "uid": 0,
            "error": "",
        }
        if check and payload["configured"]:
            result = await plugin.bili_cookie_status()
            payload.update(
                valid=result.get("valid"),
                username=result.get("username", ""),
                uid=result.get("uid", 0),
                error=result.get("error", ""),
            )
        return _json_response(payload)

    async def bili_qrcode(self):
        plugin = self.plugin
        try:
            qrcode_url, qrcode_key = await plugin.bili_qr_create()
        except ParseException as exc:
            return _error(f"获取二维码失败：{exc.message}", 502)
        except Exception as exc:
            logger.warning("[denia_share] WebUI 获取B站二维码失败", exc_info=True)
            return _error(f"获取二维码失败：{str(exc)[:160]}", 502)

        try:
            png = plugin.render_qr_png(qrcode_url)
        except Exception as exc:
            return _error(f"二维码生成失败：{str(exc)[:120]}（请确认已安装 qrcode）", 500)

        task_id = uuid.uuid4().hex[:12]
        plugin.bili_login_start(task_id, qrcode_key)
        return _json_response(
            {
                "task_id": task_id,
                "data_url": "data:image/png;base64," + base64.b64encode(png).decode("ascii"),
                "state": "waiting",
                "message": "等待扫码",
                "expires_in": plugin.bili_login_state(task_id).get("expires_in", 180),
            }
        )

    async def bili_qrcode_status(self):
        task_id = query_arg("task_id").strip()
        if not task_id:
            return _error("缺少 task_id")
        state = self.plugin.bili_login_state(task_id)
        if state.get("state") == "success":
            state["configured"] = True
        return _json_response(state)

    async def bili_qrcode_cancel(self):
        """取消指定的 B站扫码登录任务；幂等，不登出、不清除已保存的 Cookie。

        只接受不透明的 ``task_id``，不回显也不记录 ``qrcode_key`` 或 Cookie。
        """
        body = await get_json_body()
        raw = body.get("task_id") if isinstance(body, dict) else None
        if not isinstance(raw, str) or not 1 <= len(raw.strip()) <= 100:
            return _error("扫码会话无效，请重新获取二维码")
        task_id = raw.strip()
        cancel = getattr(self.plugin, "bili_login_cancel", None)
        if cancel is not None:
            await cancel(task_id)
        return _json_response({"task_id": task_id, "cancelled": True})

    async def bili_logout(self):
        ok = await self.plugin.bili_logout()
        if not ok:
            return _error("清除 Cookie 失败，请检查插件数据目录权限", 500)
        return _json_response({"configured": False})

    # ==================== 工具 ==================== #

    def _schema_problems(self) -> list[str]:
        try:
            import json

            schema_path = Path(__file__).resolve().parent.parent / "_conf_schema.json"
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
        except Exception:
            return ["无法读取 _conf_schema.json"]
        return verify_schema_alignment(schema)


def fmt_size_bytes(size: int) -> str:
    """把字节数格式化成便于阅读的字符串。"""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def _plugin_version() -> str:
    try:
        from .. import __version__

        return __version__
    except Exception:
        return "unknown"
