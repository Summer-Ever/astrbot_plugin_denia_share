# 本文件包含衍生自 astrbot_plugin_rika_share（MIT License）的代码，
# 上游项目：https://github.com/iris1598/astrbot_plugin_rika_share
# 本仓库对其做过修改；完整归属见项目根目录 README「许可与致谢」。

"""数据模型 - 解析结果和媒体内容定义"""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar, TypedDict
from pathlib import Path
from datetime import datetime
from dataclasses import field, dataclass
from collections.abc import Iterator, Awaitable

from .task import PathTask
from .constants import PlatformEnum, platform_meta
from .exception import IgnoreException, MediaSizeExceeded


@dataclass(repr=False, slots=True)
class MediaContent:
    path_task: PathTask

    async def get_path(self) -> Path:
        return await self.path_task.get()

    def __repr__(self) -> str:
        prefix = self.__class__.__name__
        return f"{prefix}({self.path_task})"


@dataclass(repr=False, slots=True)
class AudioContent(MediaContent):
    """音频内容"""
    duration: float | None = None
    cover: PathTask | None = None


@dataclass(repr=False, slots=True)
class VideoContent(MediaContent):
    """视频内容"""
    cover: PathTask | None = None
    duration: float | None = None
    # 标记「这是一段动图」。Twitter 用它表达「动图不算视频作品」——
    # ``ParseResult.video`` 会跳过它，于是卡片按图文处理。
    # （原有一个 gif_path 字段用于存放 ffmpeg 转出的 GIF，全仓无读取点，已删除）
    is_gif: bool = False

    def __repr__(self) -> str:
        repr = f"VideoContent({self.path_task}"
        if self.cover is not None:
            repr += f", cover={self.cover}"
        if self.duration:
            repr += f", duration={self.duration}"
        return repr + ")"


@dataclass(repr=False, slots=True)
class ImageContent(MediaContent):
    """图片内容"""
    alt: str | None = None
    #: 原始图片公网 URL。惰性下载任务只记路径、不保留来源地址，
    #: 所以需要「用 Markdown 集中展示图片」时得单独存一份。
    #: 由外部传入现成 Task 的构造方式拿不到 URL，此时保持 ``None``。
    url: str | None = None

    def __repr__(self) -> str:
        parts = [str(self.path_task)]
        if self.alt:
            parts.append(f"alt={self.alt}")
        if self.url:
            parts.append(f"url={self.url}")
        return f"ImageContent({', '.join(parts)})"


@dataclass(slots=True)
class Platform:
    name: str
    display_name: str


def platform_of(key: str | PlatformEnum) -> Platform:
    """按平台键构造 ``Platform``。

    展示名一律取自 ``constants.PLATFORMS`` —— 那是平台名的唯一真相。
    **不要在解析器或 main.py 里再手写展示名字面量**：以前那样散着写，
    改一个名字要翻三四处，漏一处就出现「卡片写哔哩哔哩、列表写 B站」，
    而且不会有任何报错。
    """
    meta = platform_meta(key)
    return Platform(name=meta.key, display_name=meta.label)


@dataclass(repr=False, slots=True)
class Author:
    name: str
    avatar: PathTask | None = None
    description: str | None = None

    def __repr__(self) -> str:
        repr = f"Author(name={self.name}"
        if self.avatar:
            repr += f", avatar={self.avatar}"
        if self.description:
            repr += f", description={self.description}"
        return repr + ")"


@dataclass(repr=False, slots=True)
class ParseResult:
    """完整的解析结果"""
    platform: Platform
    author: Author | None = None
    title: str | None = None
    text: str | None = None
    timestamp: int | None = None
    url: str | None = None
    contents: list[MediaContent] = field(default_factory=list)
    graphics: list[str | ImageContent] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)
    repost: ParseResult | None = None
    render_image: Path | None = None
    #: 可选的**惰性**卡片附加信息加载入口（例如 B站在线人数 / AI 摘要）。
    #: 只存可调用对象，绝不放进 ``extra``（extra 会被序列化、做历史详情）；
    #: 只有真正渲染卡片时，renderer 才会按需 ``await card_info_loader.load(...)``。
    #: 缺省 ``None``，因此所有既有构造方式保持兼容。
    card_info_loader: Any = None

    @property
    def header(self) -> str | None:
        header = self.platform.display_name
        if self.author:
            header += f" @{self.author.name}"
        if self.title:
            header += f" | {self.title}"
        return header

    @property
    def display_url(self) -> str | None:
        return f"链接: {self.url}" if self.url else None

    @property
    def repost_display_url(self) -> str | None:
        return f"原帖: {self.repost.url}" if self.repost and self.repost.url else None

    @property
    def extra_info(self) -> str | None:
        return self.extra.get("info")

    @property
    def video(self) -> VideoContent | None:
        """结果里的首个视频内容（GIF 不算）。

        不能要求 ``len(contents) == 1``：同一条作品可以既有视频又有图集。
        推特的三条取流路径（``media_extended`` / ``media.videos`` / GraphQL
        ``media``）都是把视频和图片**一起** append 进 contents，微博
        ``_collect_result`` 也是两个独立的 if。要求长度为 1 会让这类作品
        拿不到封面 hero，而 ``skip_text`` 又因为 video_contents 非空把正文掐掉
        —— 卡片既没封面也没文字。
        """
        for cont in self.contents:
            if isinstance(cont, VideoContent) and not cont.is_gif:
                return cont
        return None

    @video.setter
    def video(self, video: VideoContent | None):
        """放入视频内容。

        放在最前面，这样「视频当封面 hero」的直觉与 ``video`` 属性的取值一致。
        旧实现是「contents 非空就静默什么都不做」——调用方察觉不到，视频就这么丢了。
        """
        if video is not None:
            self.contents.insert(0, video)

    @property
    def video_contents(self) -> list[VideoContent]:
        return [cont for cont in self.contents if isinstance(cont, VideoContent)]

    @property
    def img_contents(self) -> list[ImageContent]:
        return [cont for cont in self.contents if isinstance(cont, ImageContent)]

    @property
    def audio_contents(self) -> list[AudioContent]:
        return [cont for cont in self.contents if isinstance(cont, AudioContent)]

    @property
    def all_grid_images(self):
        covers: list[PathTask] = []
        for cont in self.contents:
            if isinstance(cont, (VideoContent, AudioContent)):
                if cont.cover is not None:
                    covers.append(cont.cover)
            elif isinstance(cont, ImageContent):
                covers.append(cont.path_task)
        return covers

    @property
    def grid_medias(self):
        return [cont for cont in self.contents if isinstance(cont, (VideoContent, ImageContent))]

    @property
    def formartted_datetime(self) -> str | None:
        """发布时间格式化为本地时间字符串（无参数属性版）。"""
        if self.timestamp is None:
            return None
        return datetime.fromtimestamp(self.timestamp).strftime("%Y-%m-%d %H:%M:%S")

    def _iterate_download_coros(self, img_only: bool = False) -> Iterator[Awaitable[Path | None]]:
        if author := self.author:
            if author.avatar:
                yield author.avatar.get()
        for cont in self.contents:
            if not img_only or isinstance(cont, ImageContent):
                yield cont.path_task.get()
            if isinstance(cont, (VideoContent, AudioContent)) and cont.cover and (not img_only or not cont.cover.needs_media):
                yield cont.cover.get()
        for gra in self.graphics:
            if isinstance(gra, ImageContent):
                yield gra.path_task.get()
        if self.repost is not None:
            yield from self.repost._iterate_download_coros(img_only)

    async def ensure_downloads_complete(self, *, img_only: bool = False, suppress_errors: bool = True) -> None:
        await asyncio.gather(*self._iterate_download_coros(img_only), return_exceptions=suppress_errors)

    # 缺料提示里的量词：中文里「3 张图片」「1 个视频」比「3 图片」顺口
    _MEDIA_UNITS: ClassVar[dict[str, str]] = {
        "图片": "张", "封面": "张", "视频": "个", "音频": "个",
    }

    # 上一次审计写进 limit_warnings 的那条原文（存进 extra）。
    # 有了它才能做到「重复审计不叠加」——见 audit_missing_media。
    _AUDIT_WARNING_KEY: ClassVar[str] = "_missing_media_warning"

    # 同上，但记的是审计自己写的**体积超限提示**（可能不止一条，如视频+封面）。
    # 与缺料那条分开存：两者来源不同、条数不同，撤回时不能互相干扰。
    _SIZE_WARNING_KEY: ClassVar[str] = "_media_size_warnings"

    def _iter_media_tasks(self) -> Iterator[tuple[str, PathTask]]:
        """遍历所有需要落盘的媒体任务，附带用途标签（供缺料审计用）。

        **不含头像**：头像只是装饰，缺了不影响内容完整性；把它算进缺料，会让
        「头像恰好挂了一次」也弹一条「1 个头像下载失败，未包含在本次内容中」——
        措辞也不对，头像本来就不是「内容」。头像的失败由 ``safe_get`` 的告警日志留痕。
        """
        for cont in self.contents:
            if isinstance(cont, VideoContent):
                yield "视频", cont.path_task
                if cont.cover is not None:
                    yield "封面", cont.cover
            elif isinstance(cont, AudioContent):
                yield "音频", cont.path_task
                if cont.cover is not None:
                    yield "封面", cont.cover
            else:
                yield "图片", cont.path_task
        for gra in self.graphics:
            if isinstance(gra, ImageContent):
                yield "图片", gra.path_task
        if self.repost is not None:
            yield from self.repost._iter_media_tasks()

    def _iter_card_tasks(self) -> Iterator[tuple[str, PathTask]]:
        """仅卡片需要审计的内容图片：封面 / 图集 / 转发里的图。

        **刻意不含视频与音频本体**：卡片只用封面渲染，为一张卡片去下载整段
        视频（或音频）是纯粹的浪费。同理排除 ``needs_media`` 的派生封面
        （抽帧封面必须先有视频本体）。
        """
        for cont in self.contents:
            if isinstance(cont, (VideoContent, AudioContent)):
                if cont.cover is not None:
                    yield "封面", cont.cover
            elif isinstance(cont, ImageContent):
                yield "图片", cont.path_task
        for gra in self.graphics:
            if isinstance(gra, ImageContent):
                yield "图片", gra.path_task
        if self.repost is not None:
            yield from self.repost._iter_card_tasks()

    async def audit_missing_media(
        self, *, img_only: bool = False
    ) -> dict[str, int]:
        """结算媒体下载，将体积超限原因与真实下载失败写入 ``extra["limit_warnings"]``。

        **为什么要有这个方法**：下载失败原先只在日志里留痕，产出物本身却在撒谎
        —— 少了 3 张图的消息和图一张不缺的消息长得一模一样，用户没法判断该不该
        重试。这里在渲染/发送之前统一结算一次，把缺料写进既有的警告通道
        （``limit_warnings`` 已被卡片与聊天消息两处消费），让产物如实反映它缺了什么。

        **为什么要把 ``IgnoreException`` 排除在外**：那是本仓「**故意不下载**」的信号
        （视频时长超限、媒体体积超限、0 字节响应、m3u8 分片超限），不能算成下载失败。
        时长超限由解析器添加提示；体积超限通过 ``MediaSizeExceeded`` 单独补充原因。
        这里把 gather 的返回值与任务配对，按异常类型区分这两类提示与真实失败。

        只在确有失败时才追加，成功路径零开销（一次 gather，本来也要等这些任务）。

        ``img_only=True`` 用于「仅卡片」：只审计**卡片渲染真正会取的图**
        （封面 / 头像 / 图集），**不含视频与音频本体**，也不含 ``needs_media``
        的派生封面。审计里的 ``get()`` 就是触发下载的动作，所以清单里出现视频
        就等于把整段视频拉了下来 —— 这正是「仅卡片不下载视频」要防的。
        被排除的项**不计入 missing**：它们属于「本来就没打算要」，不是下载失败。

        **本方法是幂等的**：同一个 ``ParseResult`` 会被结算多次 —— 聊天链路
        （``main._deliver``）每次交付都调一次，而 ``_result_cache`` 命中的结果会被
        再交付一次（同一条链接在群里发两遍）；WebUI 的「按当前外观重渲染」也会对
        缓存里的**同一个对象**再调一次（``webui._preview_payload``）。不做处理的话
        「⚠️ 3 张图片下载失败」会一次一条越叠越多，而 warnings 还参与产物缓存键，
        于是每重发一次就重渲染一版卡片。所以这里先撤掉自己上一次留下的那条，
        再按本轮结果重新追加 —— **只动自己那条**，B站受限说明、时长超限这些
        别人写的警告一律不碰。

        Returns:
            各用途的失败数量，如 ``{"图片": 3, "视频": 1}``；全部成功时为空字典。
        """
        tasks = list(self._iter_card_tasks() if img_only else self._iter_media_tasks())
        if img_only:
            # 派生封面（抽帧）必须先有视频本体，仅卡片一律不碰
            tasks = [
                (kind, task) for kind, task in tasks
                if not getattr(task, "needs_media", False)
            ]
        warnings = self.extra.setdefault("limit_warnings", [])

        # 撤回上一次审计留下的那些（只撤回自己写的，别误删别的来源）
        previous = self.extra.pop(self._AUDIT_WARNING_KEY, None)
        for own in ([previous] if previous is not None else []) + list(
            self.extra.pop(self._SIZE_WARNING_KEY, None) or []
        ):
            try:
                warnings.remove(own)
            except ValueError:
                pass  # 被外部清理过，忽略

        if not tasks:
            return {}

        # 返回值不能丢：它是区分「真失败」与「按策略跳过」的唯一依据
        results = await asyncio.gather(
            *[task.get() for _, task in tasks], return_exceptions=True
        )

        missing: dict[str, int] = {}
        size_messages: list[str] = []
        for (kind, task), result in zip(tasks, results):
            if isinstance(result, MediaSizeExceeded):
                # **必须排在 IgnoreException 之前**：它是子类，写反了就是一条
                # 永远不执行的分支（且类型检查也看不出来）。体积超限是「按策略
                # 跳过」，所以**不计入 missing**，但要如实告诉用户为什么少了它。
                message = result.user_message
                if message not in size_messages:
                    size_messages.append(message)
                continue
            if isinstance(result, IgnoreException):
                # 其余策略跳过不计为下载失败；已有的时长等提示由原路径保留。
                continue
            # get() 失败时不会缓存 _path，所以 resolved 为 None 就等于「没落盘」
            if task.resolved is None:
                missing[kind] = missing.get(kind, 0) + 1

        # 体积超限的提示：写在缺料汇总**之前**，这样「为什么少了这个视频」紧挨着
        # 上下文（缺料汇总只统计真失败，两者几乎不会同时出现，但顺序固定下来
        # 比按插入顺序碰运气可靠）。记下原文，供下一次调用撤回（幂等的关键）。
        if size_messages:
            warnings.extend(size_messages)
            self.extra[self._SIZE_WARNING_KEY] = list(size_messages)

        if missing:
            parts = [
                f"{count} {self._MEDIA_UNITS.get(kind, '个')}{kind}"
                for kind, count in missing.items()
            ]
            message = f"⚠️ {'、'.join(parts)}下载失败，未包含在本次内容中"
            warnings.append(message)
            # 记下原文，供下一次调用撤回（幂等的关键）
            self.extra[self._AUDIT_WARNING_KEY] = message
        return missing

    @property
    def content_type(self) -> str:
        content_type = self.extra.get("content_type")
        if content_type is None:
            if self.video:
                return "视频"
            # 图片既可能放在 graphics（微博/NGA 那种混排），也可能放在 contents
            # （抖音/小红书/快手/微博图集）。只看 graphics 会让后者掉到「动态」——
            # 卡片头把一条图集标成「动态」，是另一种「产物在撒谎」。
            if self.img_contents or self.graphics:
                return "图文"
            if self.audio_contents:
                return "音频"
            return "动态"
        return content_type

    def __repr__(self) -> str:
        return (
            f"platform: {self.platform.display_name}, "
            f"timestamp: {self.timestamp}, "
            f"title: {self.title}, "
            f"text: {self.text}, "
            f"url: {self.url}, "
            f"author: {self.author}, "
            f"video: {self.video}, "
            f"contents: {self.contents}, "
            f"graphics: {self.graphics}, "
            f"extra: {self.extra}, "
            f"repost: [[{self.repost}]], "
            f"render_image: {self.render_image.name if self.render_image else 'None'}"
        )


class ParseResultKwargs(TypedDict, total=False):
    title: str | None
    text: str | None
    contents: list[MediaContent]
    graphics: list[str | ImageContent]
    timestamp: int | None
    url: str | None
    author: Author | None
    extra: dict[str, Any]
    repost: ParseResult | None
    card_info_loader: Any


#: 按**主机**分组的跟踪参数白名单：只对认得的平台删，且只删明确已知的键。
#:
#: **为什么不能全局黑名单**：``ts`` / ``timestamp`` / ``ref`` 看着像统计参数，
#: 却是很多站点签名（sign/timestamp/nonce 三件套）与自定义 ``ref`` 的组成部分，
#: 删掉等于把签名链接改坏 —— 用户点开是 403/失效，且症状极具迷惑性（看着
#: 只是「链接短了一点」）。所以规则**按 host 收敛**：不认识的主机一律**原样返回**。
_HOST_TRACKING_PARAMS: dict[str, frozenset[str]] = {
    "bilibili.com": frozenset({
        "spm_id_from", "vd_source", "unique_k", "from_source",
        "from_spmid", "share_source", "share_medium", "share_plat",
        "share_tag", "share_from", "share_times", "share_record",
    }),
    "xiaohongshu.com": frozenset({
        "xhsshare", "appuid", "apptime", "share_id", "shareredid",
        "share_from", "xsec_source",
    }),
    "douyin.com": frozenset({
        "previous_page", "share_token", "enter_from",
    }),
    "weibo.com": frozenset({"from", "weibo_id"}),
}


def _tracking_params_for(host: str) -> frozenset[str] | None:
    """取该主机适用的跟踪参数集合；**认不得的主机返回 None**（⇒ 原样不动）。"""
    host = host.lower()
    for domain, params in _HOST_TRACKING_PARAMS.items():
        if host == domain or host.endswith("." + domain):
            return params
    return None


def clean_url(url: str | None) -> str | None:
    """去掉**认得的平台**链接里的跟踪参数，其余一律原样返回。

    三条硬约束：

    1. **只对白名单主机生效**：B站去 ``vd_source``/``spm_id_from``，小红书去
       ``xhsshare``。未知站点**一个字都不动** —— 签名链接最怕被"顺手清理"。
    2. **必留参数**永不删除：B站 ``p``（分P）、小红书 ``xsec_token``（新链接
       没有它就 404）、以及任何 ``access_key`` / ``sign`` / ``token``。
    3. **不重新编码查询串**：删完参数后直接做**字符串级**切除，保留其余部分
       的原始字节。``urlencode`` 会把 ``%2F`` 变 ``%2F`` 之外的形态、重排顺序、
       改写空格，签名串一旦被改写就失效了。
    """
    if not url:
        return url
    try:
        from urllib.parse import urlsplit

        parts = urlsplit(url)
        if not parts.query:
            return url

        removable = _tracking_params_for(parts.hostname or "")
        if not removable:
            return url  # 认不出的主机：一律不动

        # 逐段扫描原始查询串，只切除「键名在白名单里」的整段。
        # 不 decode/re-encode：sign=%2B%3D 这类签名必须逐字节保持原样。
        kept: list[str] = []
        for segment in parts.query.split("&"):
            if not segment:
                continue
            key = segment.split("=", 1)[0]
            # 只认 ASCII 字母数字下划线开头的键；含百分号编码的键不敢动
            if key.lower() in removable and key.isascii() and key.replace("_", "").isalnum():
                continue
            kept.append(segment)

        if len(kept) == len([s for s in parts.query.split("&") if s]):
            return url  # 没删掉任何东西就不重组，省掉一切改写风险
        query = "&".join(kept)
        from urllib.parse import urlunsplit

        return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))
    except Exception:
        # 静默失败即保留原样：坏链接比长链接糟糕得多
        return url


def build_concise_text(result: ParseResult) -> list[str]:
    """构建精简文本的行列表（标题 / 干净地址 / 播放信息 / 必要提示）。

    **只发一条普通消息**，不含长简介、AI 总结或转发节点。字段缺失就省略，
    不为了凑格式联网取数。
    """
    lines: list[str] = []

    if result.title:
        lines.append(str(result.title).strip())

    url = clean_url(result.url)
    if url:
        lines.append(str(url))

    # 播放信息：优先结构化字段，缺省即省略。**刻意不读 extra["info"]** ——
    # 那是 B站 AI 总结（core/parsers/bilibili/parser.py 写入），正是要排除的长内容。
    info_parts: list[str] = []
    duration = result.extra.get("duration")
    if duration:
        info_parts.append(f"时长 {duration}")
    stats = result.extra.get("stats_line")
    if stats:
        info_parts.append(str(stats))
    online = result.extra.get("online")
    if online and not stats:
        info_parts.append(str(online))
    if info_parts:
        lines.append(" · ".join(info_parts))

    # 有意义的限制提示照旧保留（体积/时长超限、解析能力不足），
    # 否则用户只看到标题却不知道媒体为什么没发。
    for warning in result.extra.get("limit_warnings") or []:
        text = str(warning).strip()
        if text and text not in lines:
            lines.append(text)

    return lines
