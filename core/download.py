"""下载系统 - 提供媒体文件下载功能"""

import asyncio
import os
from uuid import uuid4
from pathlib import Path
from functools import partial
from urllib.parse import urlparse
from contextlib import contextmanager

import httpx
import aiofiles
from astrbot.api import logger

from .media_utils import (
    MAX_M3U8_PLAYLIST_DEPTH,
    merge_av,
    parse_m3u8_playlist,
    remux_to_mp4,
    safe_unlink,
    generate_file_name,
)
from .media_verify import (
    HEAD_PROBE_BYTES,
    classify_media_response,
    sniff_audio_ext,
    sniff_image_ext,
)
from .constants import COMMON_HEADER, DOWNLOAD_TIMEOUT
from .exception import IgnoreException, DownloadException, MediaSizeExceeded

# 导入即修一次 NO_PROXY：httpx 0.28 解析不了 ``[::1]`` 这种方括号写法，
# 而下载器也走 trust_env。详见 core/http_env.py。
from . import http_env  # noqa: F401  (import for side effect)

# 媒体下载的重试策略：连接被掐断时，最多试几次、每次隔多久。
#
# 为什么要重试：抖音/微博这类 CDN 的失败常常是**间歇性**的，不是凭证问题。
# 实测同一条 URL、同一套请求头，几十秒内会从「必失败」变成「十次全过」；
# 换不换 Referer、换不换 UA 都不构成因果。这种故障里，重试就是最有效的药。
#
# 为什么必须留退避：连发太快会把自己打进对方的限流页（见 douyin/session.py
# 里 MAX_REQUESTS_PER_WORK 的说明），那是比失败更糟的局面 —— 一旦进去，
# 之后怎么重试都没用。0.5s / 1.5s 是「够换个边缘节点、又不至于像压测」的量。
MEDIA_RETRY_ATTEMPTS = 3
MEDIA_RETRY_BACKOFF: tuple[float, ...] = (0.5, 1.5)

# 图片并发上限。
#
# 远端返回的图集长度不受控（微博长文、抖音图文、NGA 帖），而每个条目都会立刻
# 变成一条下载任务；没有闸门时并发数等于远端给的数量。这里按连接数与内存
# 取一个保守值，配合 base_parser 的数量截断一起用。
#
# 图片与视频**分池**：视频动辄几十上百 MB，一个就能占住槽几分钟，而头像、
# 封面这类几 KB 的小图和它排同一条队时，忙群里会出现「回复明显变慢，但日志里
# 什么异常都没有」——因为确实没有异常，只是小图在等大视频。
MAX_CONCURRENT_DOWNLOADS = 8

# 视频 / 音频并发上限（含 m3u8 合并下载）。
#
# 取 3 是因为 B站走 ``download_av_and_merge`` 时一次占 2 槽（视频 + 音频），
# 留 1 槽给「同时有人发了另一条视频链接」，不至于第二条链接完全排队。
MAX_CONCURRENT_MEDIA_DOWNLOADS = 3


def validate_cached_media(path: Path, maximum_mb: int) -> Path:
    """平台直接复用完整媒体缓存时，同样执行当前体积上限。"""
    configured = maximum_mb * 1024 * 1024
    limit = min(configured, HARD_MAX_MEDIA_BYTES) if configured > 0 else HARD_MAX_MEDIA_BYTES
    return StreamDownloader._cached_within_limit(path, limit)

# m3u8 分片数上限：字节上限之外再兜一层，防止「超多超小分片」把循环拖死
MAX_M3U8_SEGMENTS = 3000

# ============================ 独立于配置的硬上限 ============================
#
# 配置项 ``MAX_SIZE_MB`` 是**用户可控**的（能填到 4GB，也能填 0 = 不限制），
# 也就是说「用户配错了就没有闸」。而媒体体积完全由远端决定 —— 一个被投毒的
# 直链、一张解压炸弹图就能把磁盘吃光。下面两条是**最后一道闸**：
# 生效值 = min(用户配置, 硬上限)，用户配置为 0 时硬上限仍然生效。
#
# 单条媒体的硬上限为 2GB，单张图片为 64MB，避免异常下载耗尽磁盘。
HARD_MAX_MEDIA_BYTES = 2 * 1024 * 1024 * 1024  # 视频 / 音频 2GB
HARD_MAX_IMAGE_BYTES = 64 * 1024 * 1024  # 图片 64MB


def _is_stale_connection_error(error: Exception) -> bool:
    """是不是「连接已被对端关掉」这一类错误。

    `RemoteProtocolError("Server disconnected without sending a response")`
    是最典型的症状：连接池里那根 keep-alive 连接服务端早就关了，httpx 却还
    拿它去发请求。`ReadError` / `WriteError` / `ConnectError` /
    `ProtocolError` 同理 —— 这类错误只说明**这一根连接**死了，同一条 URL
    换根新连接往往一次就过，不该直接判死、也不该立刻降级到 curl_cffi
    （那是另一套 TLS 指纹栈，代价高得多）。

    特意**不含**超时类错误：大文件本来就慢，超时重试容易把带宽白白烧掉。
    """
    return isinstance(
        error,
        (
            httpx.RemoteProtocolError,
            httpx.ProtocolError,
            httpx.ReadError,
            httpx.WriteError,
            httpx.ConnectError,
        ),
    )


class StreamDownloader:
    def __init__(
        self,
        cache_dir: Path,
        proxies: str | None = None,
        max_size_mb: int = 0,
        verify_ssl: bool = True,
    ):
        self.headers: dict[str, str] = COMMON_HEADER.copy()
        self.cache_dir: Path = cache_dir
        # max_size_mb <= 0 表示不限制
        self.max_size_mb: int = max(0, int(max_size_mb or 0))
        self.proxies: str | None = (proxies or "").strip() or None
        # 下载请求带含 Cookie 的 ext_headers，默认必须校验证书
        self.verify_ssl: bool = bool(verify_ssl)
        self._image_slots = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
        self._media_slots = asyncio.Semaphore(MAX_CONCURRENT_MEDIA_DOWNLOADS)
        self.client: httpx.AsyncClient = self._new_client(self.proxies)

    def _new_client(self, proxy: str | None) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=DOWNLOAD_TIMEOUT, verify=self.verify_ssl, proxy=proxy,
        )

    async def aclose(self):
        await self.client.aclose()

    async def reconfigure(
        self,
        *,
        proxies: str | None = None,
        max_size_mb: int | None = None,
        cache_dir: Path | None = None,
        verify_ssl: bool | None = None,
    ) -> bool:
        """就地更新下载参数，供 WebUI 保存配置后热生效。

        体积上限与缓存目录只影响后续写入，直接改即可；代理与证书校验绑定在
        连接池上，变化时才重建 client 并关掉旧的（避免每次保存配置都泄漏一个连接池）。

        Returns:
            是否重建了连接池。
        """
        if max_size_mb is not None:
            self.max_size_mb = max(0, int(max_size_mb))
        if cache_dir is not None:
            self.cache_dir = Path(cache_dir)

        normalized = (proxies or "").strip() or None
        new_verify = self.verify_ssl if verify_ssl is None else bool(verify_ssl)
        if normalized == self.proxies and new_verify == self.verify_ssl:
            return False

        self.proxies = normalized
        self.verify_ssl = new_verify

        old_client = self.client
        self.client = self._new_client(normalized)
        try:
            await old_client.aclose()
        except Exception:
            logger.debug("关闭旧下载连接池失败", exc_info=True)
        return True

    @staticmethod
    def _part_path(file_path: Path) -> Path:
        """下载中的临时文件名（每次调用都不同）。

        先写 ``*.part`` 再 ``os.replace`` 成正式名：中途失败留下的半截文件
        不会顶着正式文件名被下一次的 ``exists()`` 当成有效缓存复用。

        **随机后缀不能省**：同一条链接被并发解析两次、或图集里同一个 URL 出现
        两次时，两个任务会拿到同一个正式名。若 .part 名也是确定的，两者会向
        同一个文件交叉写入，然后**都**执行 ``os.replace`` —— 半截内容照样
        顶着正式名落地，正是这个机制本来要防的事。加随机后缀后各写各的，
        最后由先完成的那份胜出，两份都是完整文件。
        """
        return file_path.with_name(f"{file_path.name}.{uuid4().hex[:8]}.part")

    def _validate_content_length(self, response: httpx.Response, max_bytes: int) -> int | None:
        """校验明确声明的响应大小。

        抖音等平台 CDN 常用 ``Transfer-Encoding: chunked``，此时**不会**返回
        ``Content-Length``。缺少该头并不代表响应为空，因此这里返回 ``None``
        让下载继续，真实大小在流式写入过程中统计（见 ``_validate_downloaded_bytes``）。

        上游 rika 在 2026-09 修复了此问题：旧实现把缺失的 Content-Length 当成 0，
        会直接取消下载——抖音视频因此全部下不下来。

        ``max_bytes`` 由调用方传入（图片与视频的上限不同，见 ``_max_bytes`` /
        ``_image_max_bytes``）。
        """
        content_length = response.headers.get("Content-Length")
        if not content_length:
            return None

        content_length = int(content_length)
        if content_length == 0:
            logger.warning(f"媒体 url: {response.url}, 大小为 0, 取消下载")
            raise IgnoreException

        # 体积上限：超过限制的媒体直接放弃，避免下载完才发现发不出去。
        # 抛 MediaSizeExceeded（而非普通 IgnoreException）：只有这一类「跳过」需要
        # 告诉用户确切原因，审计靠异常类型认它，见 core/exception.py 的说明。
        if max_bytes > 0 and content_length > max_bytes:
            size_mb = content_length / 1024 / 1024
            limit_mb = max_bytes // 1024 // 1024
            logger.warning(
                f"媒体大小 {size_mb:.1f}MB 超过上限 {limit_mb}MB，取消下载: {response.url}"
            )
            raise MediaSizeExceeded(received_bytes=content_length, limit_bytes=max_bytes)
        return content_length

    @staticmethod
    async def _validate_downloaded_bytes(file_path: Path, url: str, received_bytes: int, max_bytes: int = 0):
        """防止把空响应当成成功下载，并在超限时清理已写入的文件。"""
        if received_bytes <= 0:
            await safe_unlink(file_path)
            logger.warning(f"媒体 url: {url}, 实际写入 0 字节, 取消下载")
            raise IgnoreException

        if max_bytes > 0 and received_bytes > max_bytes:
            await safe_unlink(file_path)
            mb = received_bytes / 1024 / 1024
            logger.warning(f"媒体 url: {url}, 实际下载 {mb:.1f}MB 超过上限, 已丢弃")
            raise MediaSizeExceeded(received_bytes=received_bytes, limit_bytes=max_bytes)

    @property
    def _max_bytes(self) -> int:
        """视频 / 音频的生效上限 = min(用户配置, 硬上限)。

        用户配置为 0（不限制）时**硬上限仍然生效** —— 否则「不限制」等于没闸，
        而媒体体积由远端决定（见 ``HARD_MAX_MEDIA_BYTES`` 处的说明）。
        """
        maximum_mb = getattr(self, "max_size_mb", 0)
        configured = maximum_mb * 1024 * 1024 if maximum_mb > 0 else 0
        if configured <= 0:
            return HARD_MAX_MEDIA_BYTES
        return min(configured, HARD_MAX_MEDIA_BYTES)

    @property
    def _image_max_bytes(self) -> int:
        """图片的生效上限 = min(用户配置, 图片硬上限)。"""
        configured = self.max_size_mb * 1024 * 1024 if self.max_size_mb > 0 else 0
        if configured <= 0:
            return HARD_MAX_IMAGE_BYTES
        return min(configured, HARD_MAX_IMAGE_BYTES)

    @staticmethod
    def _reject_if_not_media(response, head: bytes) -> None:
        """把「200 OK + 错误页」认出来，别当媒体存盘发出去。

        判定规则见 :mod:`.media_verify`（只拒绝有把握的：明确的非媒体 Content-Type，
        或泛型 Content-Type 下开头就是 HTML/JSON/文本错误）。认不出的一律放行 ——
        把签名做成白名单会在平台换容器格式时误杀。

        抛 ``DownloadException`` 而不是 ``IgnoreException``：错误页意味着**媒体确实
        没拿到**，属于缺料审计口径里的真失败；说成「按策略跳过」会让审计不再报缺料，
        用户就会拿到一张少了这张图的卡片而毫无提示。
        """
        headers = getattr(response, "headers", None) or {}
        content_type = None
        if hasattr(headers, "get"):
            # httpx 的 Headers 大小写不敏感，curl_cffi 的也基本是；两个都试一遍最稳
            content_type = headers.get("content-type") or headers.get("Content-Type")

        reason = classify_media_response(content_type, head)
        if reason is None:
            return
        logger.warning(
            f"媒体响应不是有效内容，已丢弃 | url: {getattr(response, 'url', '?')} | {reason}"
        )
        raise DownloadException(f"媒体响应不是有效内容：{reason}")

    async def _download_httpx_with_retries(
        self,
        url: str,
        *,
        file_path: Path,
        headers: dict[str, str],
        max_bytes: int | None = None,
        chunk_size: int = 64 * 1024,
    ) -> Path:
        """httpx 下载 + 退避重试。

        第一次用共享连接池；之后的每次都用**全新的一次性连接池**，而不是原地
        重试 —— 原地的池子里躺着的那根连接本来就是坏的，复用它只会再失败一次。

        只有连接层错误（被对端掐断）才重试；4xx/5xx、证书错误重试没有意义，
        立刻抛出去交给 curl_cffi 兜底。
        """
        last_error: httpx.HTTPError | None = None
        for attempt in range(MEDIA_RETRY_ATTEMPTS):
            client = None if attempt == 0 else self._new_client(self.proxies)
            try:
                return await self._download_file_with_httpx(
                    url,
                    file_path=file_path,
                    headers=headers,
                    max_bytes=max_bytes,
                    chunk_size=chunk_size,
                    client=client,
                )
            except httpx.HTTPError as error:
                last_error = error
                if not _is_stale_connection_error(error):
                    raise
                if attempt + 1 >= MEDIA_RETRY_ATTEMPTS:
                    raise
                delay = MEDIA_RETRY_BACKOFF[min(attempt, len(MEDIA_RETRY_BACKOFF) - 1)]
                logger.debug(
                    f"下载连接被掐断（{type(error).__name__}: {error}），"
                    f"{delay:.1f}s 后换新连接重试"
                    f"（第 {attempt + 2}/{MEDIA_RETRY_ATTEMPTS} 次）| url: {url}"
                )
                await asyncio.sleep(delay)
            finally:
                if client is not None:
                    try:
                        await client.aclose()
                    except Exception:
                        logger.debug("关闭重试连接池失败", exc_info=True)
        # 循环正常退出只可能是 MEDIA_RETRY_ATTEMPTS <= 0；留个兜底，别返回 None
        raise last_error or DownloadException("媒体下载失败")

    async def _download_file_with_httpx(
        self,
        url: str,
        *,
        file_path: Path,
        headers: dict[str, str],
        max_bytes: int | None = None,
        chunk_size: int = 64 * 1024,
        client: httpx.AsyncClient | None = None,
    ) -> Path:
        part = self._part_path(file_path)
        # 不传就是媒体上限；图片由 download_img 显式传图片上限（见 _image_max_bytes）
        limit = self._max_bytes if max_bytes is None else max_bytes
        # client 参数供「连接已死、换池重试」的路径传入一次性客户端；
        # 默认走共享连接池。
        async with (client or self.client).stream(
            "GET", url, headers=headers, follow_redirects=True
        ) as response:
            response.raise_for_status()
            self._validate_content_length(response, limit)
            received_bytes = 0
            head = b""
            try:
                async with aiofiles.open(part, "wb") as file:
                    async for chunk in response.aiter_bytes(chunk_size):
                        if not chunk:
                            continue
                        if not head:
                            # 只探开头一段：CDN 的错误页一定从头开始，
                            # 所以不需要多一次请求，也不影响流式写入
                            head = chunk[:HEAD_PROBE_BYTES]
                            self._reject_if_not_media(response, head)
                        await file.write(chunk)
                        received_bytes += len(chunk)
                        # chunked 场景没有 Content-Length，边下边判大小
                        if limit > 0 and received_bytes > limit:
                            mb = received_bytes / 1024 / 1024
                            logger.warning(
                                f"媒体 url: {response.url}, 下载到 {mb:.1f}MB 超过上限，中断"
                            )
                            # **不能在这里删**：aiofiles 的句柄还开着，Windows 上
                            # unlink 会 PermissionError(WinError 32)，而 safe_unlink
                            # 把它吞成一条日志 —— 文件反而留了下来。抛出去，由下面的
                            # except 在句柄关闭后统一删（对照 _validate_downloaded_bytes：
                            # 它在句柄外，删得掉）。
                            raise MediaSizeExceeded(received_bytes=received_bytes, limit_bytes=limit)
            except BaseException:
                # 句柄已关闭，这里删得掉。必须写 BaseException：CancelledError 不是
                # Exception 的子类，只捕 Exception 时「任务被取消」留下的半截 .part
                # 会一直躺在缓存目录里（TTL 兜底是事后补救，不如当场删）。
                await safe_unlink(part)
                raise
            await self._validate_downloaded_bytes(part, str(response.url), received_bytes, limit)
        os.replace(part, file_path)
        return file_path

    async def _download_file_with_curl_cffi(
        self,
        url: str,
        *,
        file_path: Path,
        headers: dict[str, str],
        max_bytes: int | None = None,
    ) -> Path:
        try:
            import curl_cffi
        except ImportError:
            raise DownloadException("curl_cffi 未安装")

        # 兜底通道同样要带代理：代理-only 环境里不带代理等于必失败，
        # 而这条路径的存在意义恰恰是「httpx 走不通时再试一次」
        session_kwargs: dict[str, object] = {
            "allow_redirects": True,
            "verify": self.verify_ssl,
        }
        if self.proxies:
            session_kwargs["proxies"] = {"http": self.proxies, "https": self.proxies}

        part = self._part_path(file_path)
        limit = self._max_bytes if max_bytes is None else max_bytes
        async with curl_cffi.AsyncSession(**session_kwargs) as session:
            response: curl_cffi.Response = await session.get(
                url, headers=headers, timeout=DOWNLOAD_TIMEOUT, stream=True,
            )
            response.raise_for_status()
            self._validate_content_length(response, limit)
            received_bytes = 0
            head = b""
            try:
                async with aiofiles.open(part, "wb") as file:
                    async for chunk in response.aiter_content(chunk_size=8192):
                        if not chunk:
                            continue
                        if not head:
                            # 与 httpx 通道同形：只探开头一段
                            head = chunk[:HEAD_PROBE_BYTES]
                            self._reject_if_not_media(response, head)
                        await file.write(chunk)
                        received_bytes += len(chunk)
                        if limit > 0 and received_bytes > limit:
                            # 同 httpx 通道：**不能在句柄内删**（Windows 上
                            # unlink 会 PermissionError(WinError 32) 并被 safe_unlink
                            # 吞掉），抛出去交给下面的 except 在句柄关闭后删。
                            raise MediaSizeExceeded(received_bytes=received_bytes, limit_bytes=limit)
            except BaseException:
                # 与 httpx 通道保持一致：必须捕 BaseException，
                # CancelledError 不是 Exception 的子类，取消时的半截 .part 也要清。
                await safe_unlink(part)
                raise
            await self._validate_downloaded_bytes(part, str(response.url), received_bytes, limit)
        os.replace(part, file_path)
        return file_path

    def _media_limit(self) -> int | None:
        """当前媒体上限；未指定用户上限时仍保留硬上限。"""
        return self._max_bytes

    @staticmethod
    def _cached_within_limit(path: Path, limit: int | None) -> Path:
        """命中已有磁盘缓存时，**按当前上限复核体积**。

        为什么要复核：缓存文件可能是在「上限还调得很大」时下载的。用户后来把
        媒体体积上限调小，旧文件仍会被 ``exists()`` 直接返回 —— 于是新上限对
        已缓存的媒体**完全无效**，用户只会看到「我明明调小了却还是发不出去 /
        发出来这么大」。

        超限时抛 :class:`MediaSizeExceeded` 走既有提示通道，与流式下载时超限
        的表现一致；**不删缓存、也不重新下载**：文件本身是合法的，删掉等于让
        用户再花一次流量重新下载一遍明知不该发的东西。
        """
        if not path.exists():
            raise FileNotFoundError(str(path))
        if limit:
            size = path.stat().st_size
            if size > limit:
                logger.info(
                    f"[denia_share] 缓存文件超过当前上限，跳过使用: "
                    f"{path.name} ({size} > {limit})"
                )
                raise MediaSizeExceeded(received_bytes=size, limit_bytes=limit)
        return path

    async def _download_file(
        self,
        url: str,
        *,
        file_name: str | None = None,
        ext_headers: dict[str, str] | None = None,
        chunk_size: int = 64 * 1024,
        slots: asyncio.Semaphore | None = None,
        max_bytes: int | None = None,
    ) -> Path:
        if not file_name:
            file_name = generate_file_name(url)
        file_path = self.cache_dir / file_name
        headers = {**self.headers, **(ext_headers or {})}
        # 不传就是「媒体上限」（视频 / 音频）；图片由 download_img 传图片上限。
        # 两者都由 `min(用户配置, 硬上限)` 算出 —— 见 HARD_MAX_* 的说明。
        limit = self._max_bytes if max_bytes is None else max_bytes

        # 缓存命中也要复核体积（见 _cached_within_limit）。
        if file_path.exists():
            return self._cached_within_limit(file_path, limit)

        # 并发闸门：图集场景下每个条目都会走到这里，远端给多少就并发多少。
        # 视频 / 音频走单独的池（见 MAX_CONCURRENT_MEDIA_DOWNLOADS 的说明）。
        async with (slots or self._image_slots):
            if file_path.exists():
                return self._cached_within_limit(file_path, limit)
            try:
                return await self._download_httpx_with_retries(
                    url, file_path=file_path, headers=headers,
                    max_bytes=limit, chunk_size=chunk_size,
                )
            except IgnoreException:
                # 「体积超限」这类策略跳过不是下载失败，绝不能被下面的兜底
                # 抹成 DownloadException —— 缺料审计正是靠异常类型区分的。
                raise
            except Exception as primary_error:
                from .config import get_config
                try:
                    return await self._download_file_with_curl_cffi(
                        url, file_path=file_path, headers=headers, max_bytes=limit,
                    )
                except IgnoreException:
                    # 「体积超限 / 分片超限」这类**策略跳过**不是下载失败。
                    # 包装成 DownloadException 会让缺料审计把它当成真失败 ——
                    # 审计正是靠异常类型区分「按策略跳过」与「真失败」的，
                    # 在这里把它抹平，等于把那条判定打回原形。
                    # （download.py 内部已经 warning 过，不必再记一次。）
                    raise
                except Exception as fallback_error:
                    # 两条通道都失败才算真失败：这属于「产物缺料」，必须无条件留痕，
                    # 否则用户看到卡片少图、日志里却找不到原因。开关只决定要不要堆栈。
                    logger.warning(
                        f"媒体下载失败（httpx 与 curl_cffi 均失败）| url: {url} | "
                        f"httpx: {type(primary_error).__name__}: {primary_error} | "
                        f"curl_cffi: {type(fallback_error).__name__}: {fallback_error}",
                        exc_info=get_config().DEBUG_LOG_ENABLED,
                    )
                    raise DownloadException("媒体下载失败") from fallback_error

    async def download_video(
        self,
        url: str,
        *,
        video_name: str | None = None,
        ext_headers: dict[str, str] | None = None,
    ) -> Path:
        if video_name is None:
            video_name = generate_file_name(url, ".mp4")
        return await self._download_file(
            url,
            file_name=video_name,
            ext_headers=ext_headers,
            chunk_size=1024 * 1024,
            slots=self._media_slots,
        )

    async def download_m3u8(
        self,
        m3u8_url: str,
        *,
        video_name: str | None = None,
        ext_headers: dict[str, str] | None = None,
    ) -> Path:
        """下载 m3u8 视频 - 解析分片列表并合并下载"""
        if video_name is None:
            video_name = generate_file_name(m3u8_url, ".mp4")

        video_path = self.cache_dir / video_name
        if video_path.exists():
            # 合并产物同样按当前上限复核：上限调小后旧合并文件不能直接复用。
            return self._cached_within_limit(video_path, self._media_limit())

        headers = {**self.headers, **(ext_headers or {})}
        part = self._part_path(video_path)

        # m3u8 合并下载原先完全在信号量之外：它同样是一次「大文件下载」，
        # 不限流的话同时来几条就又是一次资源上溢。整段（含最后 rename）都占槽，
        # 避免「文件还没落盘但槽已释放」。
        async with self._media_slots:
            if video_path.exists():
                return self._cached_within_limit(video_path, self._media_limit())
            try:
                # 1. 解析清单。可能是主播放列表（里面每行是**子清单**而不是分片），
                #    所以要跟着最高变体再解析一层，深度封顶防死循环。
                playlist_url = m3u8_url
                seen_playlists: set[str] = set()
                init_seg_url: str | None = None
                slices: list[str] = []
                for _ in range(MAX_M3U8_PLAYLIST_DEPTH):
                    seen_playlists.add(playlist_url)
                    response = await self.client.get(playlist_url, headers=headers)
                    response.raise_for_status()
                    init, segments, variants = parse_m3u8_playlist(
                        response.text, playlist_url
                    )
                    if variants and not segments:
                        # 主播放列表：换到清晰度最高的变体，下一轮继续解析
                        if variants[0] in seen_playlists:
                            raise DownloadException("m3u8 子清单指向自身，无法解析")
                        playlist_url = variants[0]
                        continue
                    slices = segments
                    init_seg_url = init
                    break

                if not slices:
                    raise DownloadException("m3u8 分片列表为空")

                if len(slices) > MAX_M3U8_SEGMENTS:
                    logger.warning(
                        f"m3u8 分片数 {len(slices)} 超过上限 {MAX_M3U8_SEGMENTS}，取消下载: {m3u8_url}"
                    )
                    raise IgnoreException(f"m3u8 分片数超过上限({MAX_M3U8_SEGMENTS})")

                # 2. 逐个下载分片并追加到文件
                #
                # 体积上限必须在这里也算一遍：分片是逐片流式写盘的，不经过
                # _validate_content_length / _validate_downloaded_bytes，
                # 只判 Content-Length 的话 VIDEO_SIZE_MAXIMUM_MB 在 m3u8 上完全失效。
                received_bytes = 0

                async def _write_stream(target, url: str) -> int:
                    """把一个 URL 的字节流写进 target，返回累计字节数。"""
                    written = 0
                    async with self.client.stream("GET", url, headers=headers) as resp:
                        resp.raise_for_status()
                        async for chunk in resp.aiter_bytes(chunk_size=1024 * 1024):
                            if not chunk:
                                continue
                            await target.write(chunk)
                            written += len(chunk)
                    return written

                # **不用 `async with`**：见下面 finally 里的说明 —— 句柄的关闭必须
                # 排在 unlink 之前，而 `async with` 的 __aexit__ 是 await 点，
                # 它和 except 分支的先后关系不受保证。
                f = await aiofiles.open(part, "wb")
                try:
                    # init segment（fMP4 的 moov 头）必须排在所有分片之前，
                    # 缺了它整段流是播放器读不出索引的空壳。
                    if init_seg_url:
                        received_bytes += await _write_stream(f, init_seg_url)
                    for seg_url in slices:
                        received_bytes += await _write_stream(f, seg_url)
                        if self._max_bytes and received_bytes > self._max_bytes:
                            mb = received_bytes / 1024 / 1024
                            logger.warning(
                                f"m3u8 视频下载到 {mb:.1f}MB 超过上限 "
                                f"{self._max_bytes / 1024 / 1024:g}MB，中断: {m3u8_url}"
                            )
                            raise MediaSizeExceeded(
                                received_bytes=received_bytes, limit_bytes=self._max_bytes
                            )
                finally:
                    # **必须在清理之前关掉句柄。** Windows 上文件被占用时 unlink
                    # 抛 WinError 32，而 safe_unlink 会把它吞成一条日志 ——
                    # 于是「已经清了」和「没清掉」在日志里长得一样，半截 .part
                    # 静默留在缓存目录里（自检的 m3u8 取消用例就是这样间歇性失败）。
                    # 用 finally 而不是放在 except 里：正常结束与异常路径都要关。
                    try:
                        await f.close()
                    except Exception:  # noqa: BLE001 —— 关闭失败不该盖住原始异常
                        logger.warning(f"关闭 m3u8 临时文件失败: {part}", exc_info=True)

            except BaseException as exc:
                # 半截 .part 必须无条件删掉，所以这里**必须写 BaseException**：
                # CancelledError 不是 Exception 的子类，原先三个分支
                # （DownloadException/IgnoreException、httpx.HTTPError、Exception）
                # 一个都不匹配，取消时合并到一半的文件会一直躺在缓存目录里。
                # 这是同一形状的第三处 —— 前两处是 _download_file_with_httpx 与
                # _download_file_with_curl_cffi，它们早就用 BaseException 了。
                #
                # 到这一步时上面 finally 已经关掉句柄，unlink 才真的删得掉。
                await safe_unlink(part)
                if isinstance(exc, (DownloadException, IgnoreException)):
                    # 「分片数超限 / 体积超限」是**策略跳过**，不是下载失败。
                    # 包装成 DownloadException 会让缺料审计把它当成真失败 ——
                    # 审计正是靠异常类型区分「按策略跳过」与「真失败」的。
                    raise
                if not isinstance(exc, Exception):
                    # CancelledError / KeyboardInterrupt / SystemExit：原样向上抛。
                    # 包装成 DownloadException 会把「任务被取消」说成「下载失败」，
                    # 而且取消语义不该被吞。
                    raise
                # 视频下不下来属于「产物缺料」，日志不能被调试开关门控
                if isinstance(exc, httpx.HTTPError):
                    logger.warning(f"m3u8 视频下载失败 | url: {m3u8_url}", exc_info=True)
                else:
                    # 写盘失败等意外异常：同样要留痕，避免留下坏缓存被后续命中
                    logger.warning(f"m3u8 视频下载异常 | url: {m3u8_url}", exc_info=True)
                raise DownloadException("m3u8 视频下载失败") from exc

            # 3. 提交。分片拼出来的只是**字节流**，扩展名叫 .mp4 不会让它变成
            #    MP4：宽松的播放器能猜着播，严格的（尤其电脑版 QQ）会直接拒绝，
            #    于是「下载成功」却发不出能看的视频。交给 ffmpeg 零转码重封装
            #    （-c copy -movflags +faststart），没装 ffmpeg 就用裸拼接兜底。
            remuxed = part.with_suffix(".remux.mp4")
            if await remux_to_mp4(part, remuxed):
                await safe_unlink(part)
                part = remuxed
            os.replace(part, video_path)
        return video_path

    async def download_audio(
        self,
        url: str,
        *,
        audio_name: str | None = None,
        ext_headers: dict[str, str] | None = None,
    ) -> Path:
        """下载音频**原文件**。这里不做任何转码，也不需要 ffmpeg。

        ``.mp3`` 只是「URL 路径里没有后缀」时的**命名回退**，不是格式结论：
        B站 DASH 音轨落盘本就是 ``.m4s``。所以下载完成后会用文件头校正一次
        后缀（``_align_audio_suffix``），免得把 M4A 当成 MP3 交出去 ——
        下游按错的容器解码，用户看到的是「播放失败」而不是「这是个音频」。

        校正的判据是「URL 路径里到底有没有后缀」，**不是**「调用方有没有显式传名字」：
        ``generate_file_name(url, ".mp3")`` 在 URL 带 ``.m4s`` 时会用 ``.m4s``，
        那仍然是上游给的真实信息，不能被我们自己的魔数判断改掉。
        """
        url_has_suffix = bool(Path(urlparse(url).path).suffix)
        invented_default = audio_name is None and not url_has_suffix
        if audio_name is None:
            audio_name = generate_file_name(url, ".mp3")

        # 缓存命中要**在下载之前**判一次：上一轮可能已经把 ``<hash>.mp3``
        # 校正成了 ``<hash>.flac``，那 ``<hash>.mp3`` 就不存在了。此时若直接
        # 去下，等于同一个 URL 每来一次就重下一次（还可能因为改名撞名而
        # 返回一个后缀不对的副本）。所以先按同名族找已校正过的那一份。
        if invented_default:
            cached = self._find_aligned_audio(self.cache_dir / audio_name)
            if cached is not None:
                # 校正过的旧缓存同样按当前音频上限复核。
                return await self._align_audio_suffix(
                    self._cached_within_limit(cached, self._media_limit()),
                    rename_default=True,
                )

        path = await self._download_file(
            url, file_name=audio_name, ext_headers=ext_headers,
            slots=self._media_slots,
        )
        return await self._align_audio_suffix(path, rename_default=invented_default)

    #: 校正后可能出现的音频后缀。用于「按同名族找已校正的缓存」。
    _ALIGNED_AUDIO_EXTS: tuple[str, ...] = (
        ".mp3", ".m4a", ".m4s", ".flac", ".ogg", ".wav", ".aiff", ".amr", ".silk", ".mp4",
    )

    @classmethod
    def _find_aligned_audio(cls, path: Path) -> Path | None:
        """按同名族找**已校正后缀**的缓存文件；没有就返回 ``None``。

        只在「原始名是我们编的默认名」时调用 —— 这种情况下后缀是我们猜的，
        才需要认这个族。URL 自带 ``.m4s`` 的路径不进这里：那个后缀是上游给的
        事实，不该被「同名族」规则影响。
        """
        if path.exists():
            return path
        stem = path.with_suffix("")
        for ext in cls._ALIGNED_AUDIO_EXTS:
            candidate = stem.with_suffix(ext)
            if candidate.exists():
                return candidate
        return None

    async def _align_audio_suffix(self, path: Path, *, rename_default: bool) -> Path:
        """按实际字节校正音频后缀，**只在默认命名不成立时才动**。

        ``rename_default=False``（调用方自己给了文件名，或 URL 本身带真实后缀）
        时一律原样返回：那个后缀是上游给的真实信息，比我们的猜测更可信，
        凭魔数把它改掉反而是在制造「已确认的格式结论」。
        """
        if not rename_default:
            return path
        try:
            with path.open("rb") as fp:
                head = fp.read(HEAD_PROBE_BYTES)
        except OSError:
            return path
        real = sniff_audio_ext(head)
        if not real or path.suffix.lower() == real:
            return path
        target = path.with_suffix(real)
        if target.exists():
            # 校正后的文件**已经存在**（并发调用 / 上一轮改过名）：
            # 它才是那个「后缀正确」的产物。返回 `path` 等于把一个仍然叫
            # .mp3 的副本交出去，下游会按错的容器去解。
            await safe_unlink(path)
            return target
        try:
            path.rename(target)
        except OSError:
            # 改名失败不该让整条下载失败：原文件还在，只是后缀不准
            logger.debug(f"音频后缀校正失败: {path}", exc_info=True)
            return path
        logger.debug(f"音频后缀校正: {path.name} -> {target.name}")
        return target

    async def download_img(
        self,
        url: str,
        *,
        img_name: str | None = None,
        ext_headers: dict[str, str] | None = None,
    ) -> Path:
        if img_name is None:
            img_name = generate_file_name(url, ".jpg")
        path = await self._download_file(
            url, file_name=img_name, ext_headers=ext_headers,
            max_bytes=self._image_max_bytes,
        )
        return await self._align_image_suffix(path)

    async def _align_image_suffix(self, path: Path) -> Path:
        """按实际字节把图片后缀校正成真实格式。

        本仓原先按 URL 后缀定名，URL 没有后缀就一律 ``.jpg`` —— 于是 WebP / PNG
        会被存成 ``.jpg``。后缀不对会让下游（协议端、图片查看器）按错的容器去解，
        人工排查时也看不出真实格式。**认不出格式时保持原样**（不猜），
        与 ``media_verify`` 的「只拒绝有把握的」同一个原则。
        """
        try:
            with path.open("rb") as fp:
                head = fp.read(16)
        except OSError:
            return path
        real = sniff_image_ext(head)
        if not real or path.suffix.lower() == real:
            return path
        target = path.with_suffix(real)
        if target.exists():
            return path
        try:
            path.rename(target)
        except OSError:
            # 改名失败不该让整条下载失败：原文件还在，只是后缀不准
            logger.debug(f"图片后缀校正失败: {path}", exc_info=True)
            return path
        return target

    async def download_av_and_merge(
        self,
        v_url: str,
        a_url: str,
        *,
        output_path: Path,
        ext_headers: dict[str, str] | None = None,
    ) -> Path:
        if output_path.exists():
            return self._cached_within_limit(output_path, self._max_bytes)
        v_path, a_path = await asyncio.gather(
            self._download_file(v_url, ext_headers=ext_headers, slots=self._media_slots),
            self._download_file(a_url, ext_headers=ext_headers, slots=self._media_slots),
        )
        await merge_av(v_path=v_path, a_path=a_path, output_path=output_path)
        return self._cached_within_limit(output_path, self._max_bytes)
