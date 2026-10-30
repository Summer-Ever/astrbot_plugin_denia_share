# 本文件包含衍生自 astrbot_plugin_rika_share（MIT License）的代码，
# 上游项目：https://github.com/iris1598/astrbot_plugin_rika_share
# 本仓库对其做过修改；完整归属见项目根目录 README「许可与致谢」。

"""BaseParser 基类 - 所有平台解析器的基类"""

import asyncio
import re
from abc import ABC
from pathlib import Path
from re import Match, Pattern, compile
from typing import Any, ClassVar, cast, final
from typing_extensions import Unpack

from .data import Platform, ParseResult, Author, VideoContent, ImageContent, AudioContent, ParseResultKwargs
from .task import PathTask
from .download import StreamDownloader

# 导入即修一次 NO_PROXY：httpx 0.28 解析不了 ``[::1]`` 这种方括号写法，
# 而本文件是所有解析器客户端的必经之路。详见 core/http_env.py。
from . import http_env  # noqa: F401  (import for side effect)
from .constants import IOS_HEADER, COMMON_HEADER, ANDROID_HEADER, COMMON_TIMEOUT, PlatformEnum
from .exception import ParseException, IgnoreException, DownloadException, SilentException

HandlerFunc = Any
KeyPatterns = list[tuple[str, Pattern[str]]]

_KEY_PATTERNS = "_key_patterns"

# 单条解析结果里最多下载多少张图。
#
# 远端返回的图集长度完全不受控（微博长文、抖音图文、NGA 帖），而 create_images
# 会对每个 URL 立刻 create_task，数量等于远端给的数量。渲染端最多只画 6 张
# （card_renderer 的图集网格上限 3×2），但聊天侧会把每张图都发出去，
# 所以不能按渲染端上限截断，只能设一个覆盖真实场景的宽松上限。
MAX_IMAGES_PER_RESULT = 50


def _retrieve_task_exception(task: "asyncio.Task") -> None:
    """把已结束任务的异常「取回」一次，避免无人 await 时 asyncio 打整段堆栈。

    ``Task`` 的异常只有在被 ``await`` / ``exception()`` 取回后才算已处理；
    ``PathTask`` 包出来的任务**有可能永远没人 await**（结果在交付前就被丢掉），
    这时 asyncio 会把异常打成「Task exception was never retrieved」+ traceback。

    取回**不改变语义**：之后 ``await`` 同一个任务照样抛原异常。
    只对「异常本身是设计好的信号（如按策略跳过）」的任务用，别拿它掩盖真故障 ——
    真故障的留痕在 ``PathTask.safe_get`` 与缺料审计里。
    """
    if not task.cancelled():
        task.exception()


def handle(keyword: str, pattern: str):
    """注册处理器装饰器"""
    def decorator(func):
        if not hasattr(func, _KEY_PATTERNS):
            setattr(func, _KEY_PATTERNS, [])
        key_patterns: KeyPatterns = getattr(func, _KEY_PATTERNS)
        key_patterns.append((keyword, compile(pattern)))
        return func
    return decorator


def _platform_key(platform: Any) -> str:
    """从 ``platform`` ClassVar 里取出平台键（如 "bilibili"）。

    ``Platform`` 是 dataclass（``name`` 字段），但自定义解析器可能塞别的形态，
    所以按 name → value → str 依次回退，取不到就返回空串由调用方兜。
    """
    if platform is None:
        return ""
    name = getattr(platform, "name", None)
    if name:
        return str(name)
    value = getattr(platform, "value", None)
    if value:
        return str(value)
    return str(platform)


class BaseParser:
    platform: ClassVar[Platform]
    _registry: ClassVar[list[type["BaseParser"]]] = []

    def __init__(self, downloader: StreamDownloader):
        self.headers = COMMON_HEADER.copy()
        self.ios_headers = IOS_HEADER.copy()
        self.android_headers = ANDROID_HEADER.copy()
        self.downloader = downloader
        self.timeout = COMMON_TIMEOUT
        # 分解析器代理：代理地址填了**且**本平台的「走代理」开关打开才走代理，
        # 其余一律直连（开关默认全关）。下载走哪个下载器由 main.py 按同一开关挑，
        # 两边必须同源 —— 都问 proxy_for(platform.name)。
        try:
            from .config import get_config
            platform = getattr(type(self), "platform", None)
            platform_key = _platform_key(platform)
            self.proxies = get_config().proxy_for(platform_key) if platform_key else None
        except Exception:
            # 不能只静默降级直连：代理配错时表现为「该平台解析失败」，排查方向
            # 会被带偏，所以这里至少留一条线索
            from astrbot.api import logger
            logger.warning(
                "读取代理配置失败，本次请求将直连（代理配错时会导致解析失败）",
                exc_info=True,
            )
            self.proxies = None

    @staticmethod
    def verify_ssl_enabled() -> bool:
        """HTTPS 证书校验开关。读配置失败时按最安全的一侧回退：校验。"""
        try:
            from .config import get_config
            return get_config().HTTP_VERIFY_SSL
        except Exception:
            return True

    @classmethod
    def _platform_proxy(cls) -> str | None:
        """本解析器平台的生效代理；BaseParser 自身（无平台上下文）返回 None。

        与实例侧 ``self.proxies`` 同源（都走 :meth:`ParserConfig.proxy_for`），
        给那些**按类**拿 client_kwargs 的地方用（如抖音的 DouyinShareSession）。
        """
        platform = getattr(cls, "platform", None)
        if platform is None:
            return None
        platform_key = _platform_key(platform)
        if not platform_key:
            return None
        try:
            from .config import get_config
            return get_config().proxy_for(platform_key)
        except Exception:
            return None

    @classmethod
    def client_kwargs(cls, **overrides) -> dict:
        """默认 httpx 客户端参数：超时 / 证书校验 / 分解析器代理。

        给那些**自己 new AsyncClient** 的解析器用（微博 / 小红书 / NGA / AcFun /
        快手 / 抖音）。这些地方原先只传 headers 与 timeout，于是「代理」对它们
        完全无效 —— 在直连不通的服务器上表现为「这几个平台解析失败」，而媒体下载
        却是通的（下载器有代理），排查方向很容易被带偏。

        代理只在本平台开了「走代理」开关时才带上（见 :meth:`_platform_proxy`）。
        注意 ``trust_env`` 恒为 True：环境变量代理（``HTTP_PROXY`` 等）与插件
        代理是**两条独立的来源**，开关只决定「要不要用插件配的那条」——
        「只在环境变量里配代理」的部署（tests/smoke/harness.py 走的就是这条）
        不填插件代理也能整体生效。所以配置页文案说的是「不走插件代理」，
        而不是绝对的「直连」。

        **一条 httpx 的已知副作用**（不要试图在插件里绕开）：
        ``proxy`` 一旦非空，httpx 的 ``_get_proxy_map`` 直接返回
        ``{"all://": proxy}``，**整张环境映射连同 ``NO_PROXY`` 绕过列表一起丢掉**。
        也就是说「给某平台打开走代理开关」会让该平台不再享受环境变量的 NO_PROXY
        例外（比如内网域名、169.254.169.254 这类元数据地址）。需要例外时，
        把该地址直接排除在代理目标之外，或不给该平台开开关。

        调用方传的键覆盖默认值，所以 ``follow_redirects=False`` 这类特例照常可用。
        """
        kwargs: dict[str, object] = {
            "timeout": COMMON_TIMEOUT,
            "verify": cls.verify_ssl_enabled(),
            # 代理来源有两处：插件配置的 PROXY + 平台开关（下面显式传 proxy）与
            # 环境变量（httpx 的 trust_env）。**这里显式写 True 并要求调用方别
            # 各写各的**：原先微博/小红书写了 trust_env=False，于是
            # 「只在环境变量里配代理」的部署中，这两个平台会悄悄直连。
            "trust_env": True,
        }
        if proxy := cls._platform_proxy():
            kwargs["proxy"] = proxy
        kwargs.update(overrides)
        return kwargs

    def new_client(self, **kwargs) -> "AsyncClient":
        """创建带代理与默认超时的 httpx 客户端。

        ``self.proxies`` 与 ``client_kwargs`` 走同一个裁决点
        (:meth:`ParserConfig.proxy_for`)，实例侧再显式盖一次是为了让
        「构造时读到的平台代理」与「实时读到的」必然一致 —— 热更新后
        ``self.proxies`` 会由 main.py 重建解析器来刷新，两者不会长期分叉。
        """
        from httpx import AsyncClient
        merged = self.client_kwargs(
            timeout=self.timeout,
            follow_redirects=True,
            verify=self.verify_ssl_enabled(),
        )
        if self.proxies:
            merged["proxy"] = self.proxies
        merged.update(kwargs)
        return AsyncClient(**merged)

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if ABC not in cls.__bases__:
            BaseParser._registry.append(cls)

        cls._handlers = {}
        cls._key_patterns = []

        for attr_name in dir(cls):
            attr = getattr(cls, attr_name)
            if callable(attr) and hasattr(attr, _KEY_PATTERNS):
                key_patterns: KeyPatterns = getattr(attr, _KEY_PATTERNS)
                for keyword, pattern in key_patterns:
                    # **按 pattern 索引，不能按 keyword**。同一个 keyword 下允许注册
                    # 多条 pattern（小红书 xiaohongshu.com 既有 explore/discovery 的
                    # 带 id 链接，又有安全落地页的 originalUrl 链接）。按 keyword 索引
                    # 时后遍历到的 handler 会覆盖先遍历到的，而 search_url 返回的是
                    # 「先匹配上的那条 pattern」—— 两者不是同一个 handler 时就会出现
                    # 「A 的 pattern 匹配上、却派发到 B」，B 里 group("...") 直接
                    # IndexError: no such group。按 pattern 索引让匹配与派发一一对应。
                    cls._handlers[pattern] = attr
                    cls._key_patterns.append((keyword, pattern))

        cls._key_patterns.sort(key=lambda x: -len(x[0]))

    @classmethod
    def get_all_subclass(cls) -> list[type["BaseParser"]]:
        return cls._registry

    @final
    async def parse(self, keyword: str, searched: Match[str]) -> ParseResult:
        # 用**匹配上的那条 pattern** 定位 handler，而不是 keyword —— 同一个 keyword
        # 可能对应多个 handler（见 __init_subclass__ 里的说明）。keyword 只用来报错。
        handler = self._handlers.get(searched.re)
        if handler is None:
            raise SilentException(f"无法匹配 {keyword}")
        result = await handler(self, searched)
        self.fill_result_url(result, searched)
        return result

    @staticmethod
    def fill_result_url(result: ParseResult, searched: Match[str]) -> ParseResult:
        """解析结果的 ``url`` 缺失时，用解析器实际匹配到的那一段链接补上。

        **为什么必须在这里兜底**：卡片页脚左侧画的就是 ``short_url(result.url)``，
        而 ``url`` 完全由每个 handler 自己在 ``self.result(...)`` 里传，漏传没有任何
        报错 —— 卡片只是安安静静地少一行。实测抖音（视频/图文两条路径）、小红书、
        AcFun、Pixiv 收藏、B站动态/专栏一共 10 处 ``result()`` 都没传，页脚于是空着。

        放在 ``parse`` 里而不是让各解析器自己记得传，是因为这是所有平台、所有 handler
        （含自定义解析器、``parse_with_redirect``、WebUI 手动解析）的**唯一收口点**：
        在这里补一次，所有路径同时得救；散在各解析器里补，下次新增 handler 又会漏。

        只在为空时补，绝不覆盖解析器给出的规范链接 —— 有些平台会给出比用户发的
        短链更完整的地址（展开后的作品号、无追踪参数的干净链接），那才是要显示的。

        用 ``searched.group(0)`` 而不是用户发的整条消息：后者可能是一段带口令的
        分享文案，把它写进 url 会让页脚显示一整段废话，也可能被原样回显到历史记录。
        """
        if not getattr(result, "url", None):
            matched = searched.group(0)
            if matched:
                result.url = matched
        return result

    @final
    async def parse_with_redirect(self, url: str, headers: dict[str, str] | None = None) -> ParseResult:
        # 重定向这一跳必须显式带本平台的代理：get_redirect_url 是 staticmethod，
        # 它内部的 client_kwargs 按 BaseParser 自己解析，那边没有平台上下文、
        # 拿不到 proxy —— 于是「平台开了走代理、重定向这一跳却静默直连」。
        # 抖音/小红书/微博/B站的短链全走这里，是真实路径不是理论问题。
        redirect_url = await self.get_redirect_url(
            url, headers=headers or self.headers, proxy=self.proxies
        )
        if redirect_url == url:
            raise ParseException(f"无法重定向 URL: {url}")
        keyword, searched = self.search_url(redirect_url)
        return await self.parse(keyword, searched)

    @classmethod
    def search_url(cls, url: str) -> tuple[str, Match[str]]:
        for keyword, pattern in cls._key_patterns:
            if keyword not in url:
                continue
            if searched := pattern.search(url):
                return keyword, searched
        raise SilentException(f"无法匹配 {url}")

    @classmethod
    def dedup_identity(cls, searched: Match[str]) -> str | None:
        """本次匹配到的链接的「规范身份」，供同群去重跨输入形式识别同一条资源。

        默认 ``None`` —— 表示按解析器匹配到的原文（``searched.group(0)``）去重，
        与旧行为完全一致。需要跨形式去重的平台在自己的解析器里覆写本方法，例如
        B站：裸 ``BV1xx``、``bilibili.com/video/BV1xx?share_source=…`` 与
        ``bilibili.com/BV1xx`` 指向同一个视频，应当算一条。

        三条硬约束（写新实现前先读）：

        1. **纯本地、同步、不联网**。去重判定排在真正的解析**之前**，三条入口
           （内置平台处理器 / 自定义解析器 / JSON 分享卡片）都会走到这里；一旦
           在这里发请求，等于把「去重」变成一次隐式网络调用。
        2. **认不出就返回 ``None``**（或空串）。调用方会回落到原文键 —— 宁可少
           去重一次，也不能把两条不同的资源归一成同一条、让后一条再也发不出来。
        3. **稳定且与输入形式无关**：同一个资源换个说法（带不带跟踪参数、裸号还是
           链接）必须得到同一个字符串；分集 / 分P 这类**确实不同**的资源必须得到
           不同的字符串，不能一并了事。
        """
        return None

    async def dedup_expand(self, keyword: str, searched: Match[str]) -> tuple[str, Match[str]] | None:
        """异步去重预处理：BV 藏在跳转后面时（短链）先把它展开出来。

        默认 ``None`` —— 表示「本次匹配无需展开」，调用方原样拿 ``search_url``
        的结果去去重 / 解析，与旧行为完全一致。只有确实要做一跳的平台才覆写，
        例如 B站 ``b23.tv`` / ``bili2233.cn``。

        返回值约定：``(展开后的 keyword, 展开后的 searched)``。调用方会**直接用它
        取代原来的匹配**去 ``parse`` —— 所以这里的跳转就是原本 ``parse_short``
        要跳的那一次，整个请求仍然只跳一次，不会「为拿身份跳一次、parse 又跳一次」。

        两条硬约束：

        1. **只有需要展开时才联网**：不是短链、或已经能从本地认出身份时直接返回
           ``None``，别把它变成「每条链接都先发一次请求」。
        2. **失败按现有解析失败策略抛异常**（如 :class:`ParseException`）。调用方
           会撤销去重占位，让下一个人能重试；不同短链各自失败、各自回落原文键，
           绝不互相合并。
        """
        return None

    @classmethod
    def result(cls, **kwargs: Unpack[ParseResultKwargs]) -> ParseResult:
        return ParseResult(platform=cls.platform, **kwargs)

    @staticmethod
    def _permanent_network_error(exc: BaseException) -> bool:
        """瞬断重试的唯一「不重试」判据：明确的永久性 DNS / TLS 证书错误。

        短链只是一跳 302，扛不住的是抖动（连接被拒、超时、EAI_AGAIN），
        而 EAI_NONAME（域名不存在）/ EAI_FAIL 与证书校验失败**重试多少次都
        一样**：只会把一条必然失败的链接拖慢 2 秒，还白占连接。所以这两类
        直接放行给调用方，由现有失败策略（ParseException / 撤销去重占位）处理。

        异常可能被 httpcore / anyio 逐层包成 ``ConnectError``，所以同时看
        异常链与 str()（httpx 会把底层 OSError 的文本带进自己的 message）。
        """
        import socket
        import ssl

        permanent_errnos = {socket.EAI_NONAME, socket.EAI_FAIL}
        permanent_names = (
            "nodename nor servname",  # macOS/BSD 的 EAI_NONAME 文案
            "name or service not known",  # Linux 的 EAI_NONAME 文案
            "certificate verify failed",  # 证书链/主机名校验失败
        )
        current: BaseException | None = exc
        seen: set[int] = set()
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            if isinstance(current, socket.gaierror) and current.errno in permanent_errnos:
                return True
            # 只认证书校验失败，别把 SSLError 其它子类（握手中断，可重试）算进来
            if isinstance(current, ssl.SSLCertVerificationError):
                return True
            text = str(current).lower()
            if any(name in text for name in permanent_names):
                return True
            current = current.__cause__ or current.__context__
        return False

    @staticmethod
    async def get_redirect_url(
        url: str,
        headers: dict[str, str] | None = None,
        proxy: str | None = None,
    ) -> str:
        from httpx import AsyncClient, ConnectError, RequestError, TimeoutException
        headers = headers or COMMON_HEADER.copy()
        # proxy 由调用方（实例方法）显式传入：BaseParser 自身没有平台上下文，
        # client_kwargs 解析不出代理，只能靠这里补上调用方的开关结果
        overrides: dict[str, object] = {
            "headers": headers,
            "follow_redirects": False,
        }
        if proxy:
            overrides["proxy"] = proxy
        async with AsyncClient(
            **BaseParser.client_kwargs(**overrides)
        ) as client:
            # 短链这一跳是纯网络瞬时故障（连接被重置 / 超时 / 临时 DNS）就重试：
            # 复用同一个 client，退避 0.5s / 1.5s，总共最多 3 次。
            # 只包 client.get —— raise_for_status 在循环外，HTTPStatusError
            # 与 HTTP 状态错误沿用现有处理，不在这一跳自动重试。
            attempts = 3
            response = None
            last_error: RequestError | None = None
            for attempt in range(attempts):
                try:
                    response = await client.get(url)
                    break
                except (ConnectError, TimeoutException) as exc:
                    if BaseParser._permanent_network_error(exc):
                        raise
                    last_error = exc
                    if attempt + 1 < attempts:
                        await asyncio.sleep(0.5 if attempt == 0 else 1.5)
            if response is None:
                # 原样抛最后一次的异常实例：异常类型与身份都不变，
                # 调用方的失败策略（含 ConnectError 判定）照旧。取消（CancelledError
                # 非 RequestError）在 sleep/请求处直接穿透，不做任何处理。
                assert last_error is not None
                raise last_error
            if response.status_code >= 400:
                response.raise_for_status()
            return response.headers.get("Location", url)

    @staticmethod
    async def get_final_url(
        url: str,
        headers: dict[str, str] | None = None,
        proxy: str | None = None,
    ) -> str:
        from httpx import AsyncClient
        headers = headers or COMMON_HEADER.copy()
        overrides: dict[str, object] = {
            "headers": headers,
            "follow_redirects": True,
        }
        if proxy:
            overrides["proxy"] = proxy
        async with AsyncClient(
            **BaseParser.client_kwargs(**overrides)
        ) as client:
            response = await client.get(url)
            if response.status_code >= 400:
                response.raise_for_status()
            return str(response.url)

    def create_author(self, name: str, avatar_url: str | None = None, description: str | None = None):
        author = Author(name=name, description=description)
        if avatar_url:
            # 惰性：仅文本模式下头像一次都不会下载
            author.avatar = PathTask.lazy(
                lambda url=avatar_url: self.downloader.download_img(
                    url, ext_headers=self.headers
                )
            )
        return author

    def create_video(self, url_or_task: str | asyncio.Task[Path] | PathTask, cover_url: str | None = None, duration: float | None = None, is_gif: bool = False):
        if duration is not None:
            from .config import get_config
            from .media_utils import fmt_duration
            pconfig = get_config()
            if duration > pconfig.VIDEO_DURATION_MAXIMUM:
                from astrbot.api import logger
                logger.warning(
                    f"视频时长 {fmt_duration(duration)} "
                    f"超过限制 {fmt_duration(pconfig.VIDEO_DURATION_MAXIMUM)}, 跳过下载"
                )
                # 提前返回前取消已调度的下载任务，避免后台协程跑完整个下载而无人回收
                if isinstance(url_or_task, asyncio.Task):
                    url_or_task.cancel()
                # 模仿 B站：创建一个 download_video 闭包，被 await 时抛出 IgnoreException
                # 这样解析结果能正常返回（标题/作者/封面），但实际不下载视频
                async def _skip_video_download():
                    raise IgnoreException(
                        f"视频时长({fmt_duration(duration)})超过限制({fmt_duration(pconfig.VIDEO_DURATION_MAXIMUM)})，跳过下载"
                    )
                # 惰性 + 立刻取回一次异常：这个 Task 一旦被 get() 就会失败，而
                # 失败信息若无人取回会被 asyncio 打成「Task exception was never
                # retrieved」+ 整段堆栈（自检里已现过一次）。done_callback 只是
                # 把异常**读出来**（不影响后续 await 照样抛），所以它不会提前
                # 让跳过信号丢失。
                def _make_skip_task() -> "asyncio.Task[Path]":
                    task = asyncio.create_task(_skip_video_download())
                    task.add_done_callback(_retrieve_task_exception)
                    return task

                # 惰性：跳过分支里本来就没有任何东西要下载，唯一代价是那个
                # IgnoreException 信号，构造时就 create_task 属于纯浪费
                video_content = VideoContent(
                    PathTask.lazy(_make_skip_task), duration=duration, is_gif=is_gif
                )
                if cover_url:
                    video_content.cover = PathTask.lazy(
                        lambda url=cover_url: self.downloader.download_img(
                            url, ext_headers=self.headers
                        )
                    )
                return video_content

        # 统一先包成 PathTask：抽封面 / 转 GIF 与内容本体都要 await 同一个任务，
        # 直接 await 裸协程会导致重复 await 报错（上游 rika 2026-09 修复）
        #
        # URL 形态走**惰性**工厂：仅文本/仅卡片模式下根本不会有人 get 它，
        # 构造那一刻就不该有下载跑起来。外部传进来的 Task / PathTask 保持原样
        # （那是别人已经调度好的，不归我们管，也没法再变惰性）。
        if isinstance(url_or_task, str):
            path_task = PathTask.lazy(
                lambda url=url_or_task: self.downloader.download_video(
                    url, ext_headers=self.headers
                )
            )
        elif isinstance(url_or_task, PathTask):
            path_task = url_or_task
        else:
            path_task = PathTask(url_or_task)

        video_content = VideoContent(path_task, duration=duration, is_gif=is_gif)

        if cover_url:
            cover_task = PathTask.lazy(
                lambda url=cover_url: self.downloader.download_img(
                    url, ext_headers=self.headers
                )
            )
            video_content.cover = cover_task
        else:
            async def extract_cover():
                """从视频里抽第一帧当封面；失败返回 ``None``，**不向上抛**。

                这个协程有可能**没有人 await**（视频被策略跳过、结果在交付前就
                被丢弃、解析器随后抛了别的异常），异常若裸着出去就会以
                「Task exception was never retrieved」+ 一整段 ffmpeg stderr 的
                形态落进日志（自检里已经现过一次）。这里收口成 ``None``：
                与「封面没落盘」的既有语义一致（缺料审计照常记一笔「封面」），
                只是把不可读的噪音换成一条能定位的 warning。
                """
                from astrbot.api import logger
                from .media_utils import extract_video_first_frame

                try:
                    video_path = await path_task.get()
                except Exception as exc:  # noqa: BLE001 —— 视频本身就不可用
                    logger.warning(
                        f"抽取视频封面失败（视频不可用）: {type(exc).__name__}: {exc}"
                    )
                    return None
                try:
                    return await extract_video_first_frame(video_path)
                except Exception as exc:  # noqa: BLE001 —— ffmpeg 失败/未安装
                    logger.warning(f"抽取视频封面失败: {type(exc).__name__}: {exc}")
                    return None

            # needs_media=True：**必须先下载整段视频**才能抽帧。仅卡片模式下
            # 这一步被跳过，卡片用占位背景 —— 为一张封面下整段视频不划算。
            # 它内部会 await path_task，也就是顺带把视频本体拉下来。
            video_content.cover = PathTask.lazy(extract_cover, needs_media=True)

        # 这里原有一段 `if is_gif: 生成 gif_path` 的自动转换，已删除：
        # 全仓没有任何地方读 gif_path，ffmpeg 每次都白跑一遍、产物没人取用，
        # 而它唯一的调用者（抖音图文帖）正是「图集被当成视频」那个 bug 的来源。
        # is_gif 标记本身保留 —— Twitter 用它表达「这是动图，不算视频作品」。
        return video_content

    def _add_limit_warning(
        self, result: ParseResult, duration: float | None, media_type: str = "视频"
    ):
        """检查时长，超限时添加 limit_warnings 到 result.extra。

        ``media_type`` 让音频复用同一条文案路径：超限原因要出现在用户**已经选了**
        的文本/卡片里，而不是靠插件擅自改发别的形式。
        """
        if duration is not None:
            from .config import get_config
            from .media_utils import fmt_duration
            pconfig = get_config()
            if duration > pconfig.VIDEO_DURATION_MAXIMUM:
                from astrbot.api import logger
                msg = (
                    f"⚠️ {media_type}时长({fmt_duration(duration)})"
                    f"超过限制({fmt_duration(pconfig.VIDEO_DURATION_MAXIMUM)})，不会下载{media_type}"
                )
                logger.warning(msg)
                result.extra.setdefault("limit_warnings", []).append(msg)

    def create_images(self, image_urls: list[str]):
        """把一批图片 URL 转成 ImageContent（每个 URL 一条**惰性**下载任务）。

        惰性是「仅文本」不下载图片的前提：图片归富媒体一路，文本不该顺手
        把它们拉下来。超过 :data:`MAX_IMAGES_PER_RESULT` 的部分直接丢弃 ——
        远端列表长度不可控，不截断的话并发下载数与磁盘占用都跟着远端走。
        """
        contents: list[ImageContent] = []
        for url in self.cap_image_urls(image_urls):
            contents.append(
                ImageContent(
                    PathTask.lazy(
                        lambda u=url: self.downloader.download_img(
                            u, ext_headers=self.headers
                        )
                    ),
                    url=url,
                )
            )
        return contents

    @staticmethod
    def cap_image_urls(image_urls: list[str]) -> list[str]:
        """截断远端图集长度，超限时记一条警告。"""
        if len(image_urls) <= MAX_IMAGES_PER_RESULT:
            return image_urls
        from astrbot.api import logger
        logger.warning(
            f"图集条目 {len(image_urls)} 超过上限 {MAX_IMAGES_PER_RESULT}，"
            f"只处理前 {MAX_IMAGES_PER_RESULT} 张"
        )
        return image_urls[:MAX_IMAGES_PER_RESULT]

    def create_image(self, url_or_task: str | asyncio.Task[Path], alt: str | None = None):
        if isinstance(url_or_task, str):
            url = url_or_task
            path_task = PathTask.lazy(
                lambda url=url_or_task: self.downloader.download_img(
                    url, ext_headers=self.headers
                )
            )
        else:
            # 外部已调度好的 Task：保持急式语义（不归我们管，也没法再变惰性）
            # 这条路径拿不到原始 URL，Markdown 展示时会被跳过。
            url = None
            path_task = (
                url_or_task if isinstance(url_or_task, PathTask) else PathTask(url_or_task)
            )
        return ImageContent(path_task, alt=alt, url=url)

    def create_audio(self, url_or_task: str | asyncio.Task[Path], duration: float = 0.0):
        """把音频 URL / 任务包成 :class:`AudioContent`（**URL 形态是惰性的**）。

        时长上限复用 :attr:`VIDEO_DURATION_MAXIMUM` —— 同一个「媒体时长」旋钮，
        音频用**秒**（与 ``AudioContent.duration``、``VideoContent.duration`` 一致）。
        超限时返回的内容在 ``get()`` 时抛 ``IgnoreException``：解析结果照常返回
        （标题/作者/封面仍在），只是不下载音频本体。
        """
        from .config import get_config
        from .media_utils import fmt_duration

        maximum = get_config().VIDEO_DURATION_MAXIMUM
        if duration and duration > maximum:
            from astrbot.api import logger
            logger.warning(
                f"音频时长 {fmt_duration(duration)} 超过限制 {fmt_duration(maximum)}, 跳过下载"
            )
            if isinstance(url_or_task, asyncio.Task):
                # 外部传进来的是已经调度好的急式任务：策略已决定不下载，
                # 必须取消，否则它会在后台把整个音频白下完。
                url_or_task.cancel()

            async def _skip_audio_download():
                raise IgnoreException(
                    f"音频时长({fmt_duration(duration)})超过限制({fmt_duration(maximum)})，跳过下载"
                )

            def _make_skip_task() -> "asyncio.Task[Path]":
                task = asyncio.create_task(_skip_audio_download())
                task.add_done_callback(_retrieve_task_exception)
                return task

            # 跳过分支里没有任何东西要下载，构造时就 create_task 属于纯浪费。
            return AudioContent(PathTask.lazy(_make_skip_task), duration)

        if isinstance(url_or_task, str):
            path_task = PathTask.lazy(
                lambda url=url_or_task: self.downloader.download_audio(
                    url, ext_headers=self.headers
                )
            )
        else:
            path_task = (
                url_or_task if isinstance(url_or_task, PathTask) else PathTask(url_or_task)
            )
        return AudioContent(path_task, duration)
