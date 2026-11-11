# 本文件包含衍生自 astrbot_plugin_rika_share（MIT License）的代码，
# 上游项目：https://github.com/iris1598/astrbot_plugin_rika_share
# 本仓库对其做过修改；完整归属见项目根目录 README「许可与致谢」。

"""Bilibili 解析器 - 支持视频、动态、直播、专栏、收藏夹"""

import re
import json
import html
import time
import asyncio
import difflib
from dataclasses import dataclass
from typing import Any, ClassVar

from astrbot.api import logger
from bilibili_api import HEADERS, Credential, select_client, request_settings
from bilibili_api.opus import Opus
from bilibili_api.video import Video
from bilibili_api.login_v2 import QrCodeLogin, QrCodeLoginEvents
from msgspec import convert

from ...base_parser import BaseParser, PlatformEnum, ParseException, IgnoreException, DownloadException, handle
from ...exception import MediaProcessException
from ...download import validate_cached_media
from .access import (
    STATUS_BLOCKED,
    STATUS_FULL,
    analyze_play_access,
    expand_bili_short_link,
    video_dedup_identity,
)
from ...data import Platform, ImageContent, MediaContent, platform_of
from ...cookie_utils import ck2dict
from ...media_utils import ffmpeg_available, fmt_duration
from ...task import PathTask

try:
    select_client("curl_cffi")
    request_settings.set("impersonate", "chrome131")
except Exception:
    logger.warning("curl_cffi 未注册/未安装，B站解析器将使用默认 httpx 客户端")
    select_client("httpx")


# ---------------------------------------------------------------- 标题反查
#
# QQ 官方机器人把 B站小程序卡片渲染成纯文本时，只留 title / preview /
# source：没有 b23.tv，也没有 BV 号，唯一能落地的身份信息就是**视频标题**。
# 所以「卡片没链接」这条路上，只能拿标题去 B站搜一次，再把搜到的视频当成
# 用户分享的那条。
#
# 这里的取舍是「宁可不解析，也不发错视频」：只接受归一化之后**几乎完全一致**
# 的那一条（默认 0.86），命中不了就当作没找到。翻唱 / 二创 / 同名投稿的标题
# 往往只差几个字，阈值一放松就会把别人的视频发出去 —— 那比零反应更糟。
TITLE_MATCH_MIN_RATIO = 0.86

# 自动解析之上还有一档：给出候选、由用户点选（不自动发卡片）。
# 落在 [TITLE_CANDIDATE_MIN_RATIO, TITLE_MATCH_MIN_RATIO) 之间说明「像但不保准」，
# 静默丢弃等于让用户面对零反应，直接发又可能发错 —— 折中是列出来让用户挑。
TITLE_CANDIDATE_MIN_RATIO = 0.62

# 单次卡片反查最多允许的搜索请求数（每页算一次）。标题精确命中时 1 次就够，
# 只有标题带噪声时才需要多试一个关键词 + 第二页。
TITLE_SEARCH_MAX_REQUESTS = 3

# 命中到这个程度才提前收工，不去翻第二页：0.97 以上基本就是同一条视频，
# 留一点余量给「标题被改过几个字」的情况。
TITLE_EARLY_STOP_RATIO = 0.97

# 同一标题的搜索结果缓存时长（秒）。同一张卡片常被反复转发。
TITLE_SEARCH_CACHE_TTL = 1800
TITLE_SEARCH_CACHE_MAX = 200

# 搜索结果标题带 ``<em class="keyword">`` 高亮；归一化时要连标签一起剥掉，
# 否则同一条视频会因为它多出 30 个字符而算成「不相似」。
_TITLE_TAG_RE = re.compile(r"<[^>]+>")

# 归一化时抹掉的字符：空白、全角空格，以及标题两边常见的分隔符 / 标点。
# 保留汉字、字母、数字 —— 这些才承载「是哪条视频」。
_TITLE_NOISE_RE = re.compile(
    r"[\s\u3000|｜:：,，.。!！?？~～\-—_/\\()（）\[\]【】「」『』《》\"'“”‘’·]+"
)


def normalize_title(text: str) -> str:
    """标题归一化：剥高亮标签、还原实体、去掉空白与标点、统一大小写。"""
    cleaned = html.unescape(_TITLE_TAG_RE.sub("", str(text or "")))
    return _TITLE_NOISE_RE.sub("", cleaned).lower()


def title_similarity(left: str, right: str) -> float:
    """两个标题的相似度（0~1），已按 :func:`normalize_title` 归一化。"""
    a, b = normalize_title(left), normalize_title(right)
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


# 官方系列投稿的标题普遍是「主标题 | 副标题」（``《鸣潮》共鸣者战斗演示 | 莉莉``）。
# 同系列不同角色只差最后这一段，整串比对天然会给出 0.8~0.86 的假高分，
# 所以打分时把副标题单独拎出来加权。注意归一化会把这些分隔符全删掉，
# 这里必须对**原始标题**切分。
_TITLE_SUBTITLE_RE = re.compile(r"[|｜/\\:：\-—–_·、]+")


# 标题尾部 / 中部的平台噪声。QQ 卡片、各站分享口令经常给标题加料：
# ``《X》 | 莉莉 - 哔哩哔哩``、``【鸣潮】… #鸣潮# #游戏#``。这些词不承载
# 「是哪条视频」的信息，留着会把相似度拉低到阈值以下。
_PLATFORM_SUFFIX_RE = re.compile(
    r"\s*[-—–|｜]\s*(?:哔哩哔哩|bilibili|b站|抖音|快手|微博|小红书|acfun|youtube)\s*$",
    re.IGNORECASE,
)
_HASHTAG_RE = re.compile(r"[#＃][^#＃\s]{0,40}[#＃]?")


def clean_title(text: str) -> str:
    """去掉标题上的平台噪声（HTML 高亮、话题标签、``- 哔哩哔哩`` 后缀）。"""
    cleaned = html.unescape(_TITLE_TAG_RE.sub("", str(text or ""))).strip()
    cleaned = _HASHTAG_RE.sub(" ", cleaned)
    # 每轮剥掉一段平台后缀、文本严格变短，剥不动即为收敛点。终止条件写在
    # 循环头（而不是 while True + break），有界性一眼可见，也避免静态检查
    # 把它当成「无让出点的死循环」。
    while (stripped := _PLATFORM_SUFFIX_RE.sub("", cleaned).strip()) != cleaned:
        cleaned = stripped
    return re.sub(r"\s+", " ", cleaned).strip(" -—–|｜")


@dataclass(frozen=True)
class VideoCandidate:
    """一条候选视频。``score`` 是 0~1 的标题匹配分。"""

    bvid: str
    title: str
    author: str
    score: float

    @property
    def url(self) -> str:
        return f"https://www.bilibili.com/video/{self.bvid}"

    def display(self) -> str:
        author = f"（{self.author}）" if self.author else ""
        return f"{self.title}{author}"


class BilibiliParser(BaseParser):
    platform: ClassVar[Platform] = platform_of(PlatformEnum.BILIBILI)

    @staticmethod
    def _codec_order(codecs_enum: type, preference: str) -> list:
        """按配置项决定 ``detect_best_streams`` 的 codec 优先顺序。

        为什么要这个开关：同一个视频 B站会同时提供多种压缩格式，而它们的
        兼容性差距极大。AV1 体积最小，但老客户端（尤其电脑版 QQ）硬解不了，
        发过去就是花屏；H.264 任何客户端都能播，代价是同画质下体积大一些。

        1.2.x 把 AV1 排在第一位，那是为了省流量，无意间把「能下下来」和
        「放得出来」混成了一件事。这里把顺序交还给用户。

        注意这里只决定**同清晰度下**挑哪个编码，不影响清晰度本身：
        4K/8K 常常只有 H.265/AV1，配置成 H.264 优先时只是拿不到该档的
        H.264 版本，``detect_best_streams`` 会按列表顺序退到 H.265/AV1，
        不会因此下载失败。
        """
        order = {
            "H.264 优先": ("AVC", "HEV", "AV1"),
            "H.265 优先": ("HEV", "AVC", "AV1"),
            "AV1 优先": ("AV1", "AVC", "HEV"),
        }.get(str(preference or "").strip(), ("AVC", "HEV", "AV1"))
        return [getattr(codecs_enum, name) for name in order]

    @staticmethod
    def _is_transient_api_error(error: Exception) -> bool:
        """判断 B站接口错误是否适合重试。

        -504 是 B站上游服务调用超时，通常是一过性的；网络层超时同样按此处理。
        """
        if isinstance(error, (asyncio.TimeoutError, TimeoutError)):
            return True

        code = getattr(error, "code", None)
        if code is None:
            code = getattr(error, "retcode", None)
        try:
            if int(code) == -504:
                return True
        except (TypeError, ValueError):
            pass

        message = str(error).lower()
        return (
            "-504" in message
            or "服务调用超时" in message
            or "timeout" in message
            or "timed out" in message
        )

    async def _call_bili_api_with_retry(self, call, *, operation: str, retries: int = 2):
        """对 B站临时超时做少量退避重试，避免瞬时故障直接导致解析失败。"""
        for attempt in range(retries + 1):
            try:
                return await call()
            except Exception as error:
                if not self._is_transient_api_error(error) or attempt >= retries:
                    raise

                delay = 0.8 * (attempt + 1)
                logger.warning(
                    f"B站{operation}接口临时超时，{delay:.1f}秒后重试 "
                    f"({attempt + 1}/{retries})：{error}"
                )
                await asyncio.sleep(delay)

    # ---------------- 分享卡片：按标题反查视频 ----------------

    @classmethod
    def _title_parts(cls, text: str) -> list[str]:
        """按主 / 副标题切开，去掉空白段。

        先剥 HTML 标签再切：搜索结果的副标题也会被高亮包裹
        （``| <em class="keyword">莉莉</em>``），直接切会在 ``</em>`` 上劈开，
        把副标题切丢、尾部剩一个 ``em>`` 残片。
        """
        cleaned = html.unescape(_TITLE_TAG_RE.sub("", str(text or "")))
        return [part.strip() for part in _TITLE_SUBTITLE_RE.split(cleaned) if part.strip()]

    @classmethod
    def title_tail(cls, text: str) -> str:
        """标题最后一段（通常是角色名 / 集数），已归一化。"""
        for part in reversed(cls._title_parts(text)):
            cleaned = normalize_title(part)
            if cleaned:
                return cleaned
        return ""

    @classmethod
    def build_title_queries(cls, title: str) -> list[str]:
        """同一张卡片可以试的搜索词，越靠前越精确，最多两个。

        卡片标题经常被平台加工过（尾部缀 ``- 哔哩哔哩``、带一堆 ``#话题#``），
        原样搜会连一条都对不上；这时退一步只用主标题再搜一次。多花一次请求，
        换取「标题被加工过」这一类漏识别。
        """
        raw = clean_title(str(title or "").strip())
        queries: list[str] = [raw] if raw else []
        parts = cls._title_parts(raw)
        if len(parts) >= 2:
            cut = raw.rfind(parts[-1])
            head = raw[:cut].strip(" \t|｜-—–_·、") if cut > 0 else ""
            if head and head not in queries:
                queries.append(head)
        return queries[:2]

    @classmethod
    def score_video(cls, title: str, candidate: str) -> float:
        """标题匹配打分（0~1），越高越可能是同一条视频。

        两段式：整串相似度占 0.75，副标题（``|`` 之后那段）相似度占 0.25。
        带分隔符的标题才启用副标题权重 —— 单段标题没有「副标题」可言，
        硬切会把普通视频标题的最后几个字当成副标题，反而降低准确度。
        """
        title = clean_title(title)
        candidate = clean_title(candidate)
        full = title_similarity(title, candidate)
        if len(cls._title_parts(title)) < 2:
            return full
        q_tail, c_tail = cls.title_tail(title), cls.title_tail(candidate)
        if not q_tail or not c_tail:
            return full
        tail = difflib.SequenceMatcher(None, q_tail, c_tail).ratio()
        return 0.75 * full + 0.25 * tail

    @classmethod
    def rank_videos(cls, items: Any, title: str) -> list[VideoCandidate]:
        """把搜索结果整理成按匹配分排序的候选列表（纯函数，可离线测）。"""
        candidates: list[VideoCandidate] = []
        seen: set[str] = set()
        for item in items or []:
            if not isinstance(item, dict):
                continue
            bvid = str(item.get("bvid") or "").strip()
            if not bvid or bvid in seen:
                continue
            seen.add(bvid)
            raw_title = str(item.get("title") or "")
            candidates.append(
                VideoCandidate(
                    bvid=bvid,
                    title=html.unescape(_TITLE_TAG_RE.sub("", raw_title)).strip(),
                    author=str(item.get("author") or "").strip(),
                    score=cls.score_video(title, raw_title),
                )
            )
        candidates.sort(key=lambda item: item.score, reverse=True)
        return candidates

    @classmethod
    def pick_best_video(cls, results: Any, title: str) -> str | None:
        """从搜索结果里挑出与标题最接近的一条，返回 ``bvid``；认不出返回 ``None``。"""
        ranked = cls.rank_videos(results, title)
        if ranked and ranked[0].score >= TITLE_MATCH_MIN_RATIO:
            return ranked[0].bvid
        return None

    # ---------------------------------------------------------- 缓存

    def _title_cache_get(self, keyword: str) -> list[VideoCandidate] | None:
        entry = self._title_cache.get(keyword)
        if not entry:
            return None
        stamp, value = entry
        if time.monotonic() - stamp > TITLE_SEARCH_CACHE_TTL:
            self._title_cache.pop(keyword, None)
            return None
        return value

    def _title_cache_put(self, keyword: str, value: list[VideoCandidate]) -> None:
        if len(self._title_cache) >= TITLE_SEARCH_CACHE_MAX:
            # 简单淘汰：清掉最早写入的那批，够用且不引入额外数据结构。
            for key in list(self._title_cache)[: TITLE_SEARCH_CACHE_MAX // 4]:
                self._title_cache.pop(key, None)
        self._title_cache[keyword] = (time.monotonic(), value)

    # ---------------------------------------------------- 对外入口

    async def search_video_candidates(self, title: str, *, limit: int = 3) -> list[VideoCandidate]:
        """按标题反查候选视频，按匹配分从高到低返回（可能为空）。

        策略：先用完整标题搜第一页；命中不到「几乎确定」的程度就再翻第二页、
        换主标题再搜一次，最多 ``TITLE_SEARCH_MAX_REQUESTS`` 次请求。
        结果按标题缓存 30 分钟，同一张卡片被反复转发时不重复打接口。

        失败一律降级成空列表：搜索只是「附加能力」，不该让整条消息变报错。
        """
        keyword = str(title or "").strip()
        # 太短的标题（「预告」「第一集」）搜出来全是噪声，直接放弃。
        if len(normalize_title(keyword)) < 4:
            return []

        cached = self._title_cache_get(keyword)
        if cached is not None:
            return cached[:limit]

        from bilibili_api import search

        collected: list[Any] = []
        requests = 0
        ranked: list[VideoCandidate] = []
        for query in self.build_title_queries(keyword):
            pages = (1, 2) if query == keyword else (1,)
            for page in pages:
                if requests >= TITLE_SEARCH_MAX_REQUESTS:
                    break
                requests += 1
                try:
                    payload = await self._call_bili_api_with_retry(
                        lambda q=query, p=page: search.search_by_type(
                            q,
                            search_type=search.SearchObjectType.VIDEO,
                            page=p,
                            page_size=30,
                        ),
                        operation="标题搜索",
                    )
                except Exception as error:  # noqa: BLE001 - 搜索失败一律降级
                    logger.warning(f"B站按标题反查失败（{query} 第{page}页）：{error}")
                    break
                items = payload.get("result") if isinstance(payload, dict) else None
                collected.extend(items or [])
                ranked = self.rank_videos(collected, keyword)
                if ranked and ranked[0].score >= TITLE_EARLY_STOP_RATIO:
                    # 已经基本确定是同一条，不再浪费请求。
                    self._title_cache_put(keyword, ranked[:limit])
                    return ranked[:limit]
            if ranked and ranked[0].score >= TITLE_EARLY_STOP_RATIO:
                break

        ranked = [
            item for item in self.rank_videos(collected, keyword)
            if item.score >= TITLE_CANDIDATE_MIN_RATIO
        ][:limit]
        self._title_cache_put(keyword, ranked)
        return ranked

    async def search_video_url(self, title: str) -> str | None:
        """按标题反查唯一的视频链接；认不出就返回 ``None``。

        只返回「够确定」的那条（``TITLE_MATCH_MIN_RATIO`` 以上），
        供只需要一个结果的调用方使用；需要给用户挑候选的场合用
        :meth:`search_video_candidates`。
        """
        candidates = await self.search_video_candidates(title, limit=1)
        if candidates and candidates[0].score >= TITLE_MATCH_MIN_RATIO:
            return candidates[0].url
        return None

    def __init__(self, downloader, bili_ck: str | None = None, config_dir=None):
        super().__init__(downloader)
        self.headers = HEADERS.copy()
        self._credential: Credential | None = None
        self._bili_ck = bili_ck
        self._cookies_file = (config_dir / "bilibili_cookies.json") if config_dir else None
        # 标题 → 候选列表。同一张卡片常被同一群里多个人转发，缓存能省掉重复搜索。
        self._title_cache: dict[str, tuple[float, list[VideoCandidate]]] = {}

    @handle("b23.tv", r"b23\.tv/[0-9a-zA-Z._?%&+-=/#]+")
    @handle("bili2233", r"bili2233\.cn/[0-9a-zA-Z._?%&+-=/#]+")
    async def _parse_short_link(self, searched: re.Match[str]):
        url = f"https://{searched.group(0)}"
        return await self.parse_with_redirect(url)

    @handle("BV", r"^(?P<bvid>BV[0-9a-zA-Z]{10})(?:\s)?(?P<page_num>\d{1,3})?$")
    @handle("/BV", r"bilibili\.com(?:/video)?/(?P<bvid>BV[0-9A-Za-z]{10})(?:.*?[?&]p=(?P<page_num>\d{1,3}))?")
    async def _parse_bv(self, searched: re.Match[str]):
        bvid = str(searched.group("bvid"))
        page_num = int(searched.group("page_num") or 1)
        return await self.parse_video(bvid=bvid, page_num=page_num)

    @handle("av", r"^av(?P<avid>\d{6,})(?:\s)?(?P<page_num>\d{1,3})?$")
    @handle("/av", r"bilibili\.com(?:/video)?/av(?P<avid>\d{6,})(?:.*?[?&]p=(?P<page_num>\d{1,3}))?")
    async def _parse_av(self, searched: re.Match[str]):
        avid = int(searched.group("avid"))
        page_num = int(searched.group("page_num") or 1)
        return await self.parse_video(avid=avid, page_num=page_num)

    @classmethod
    def dedup_identity(cls, searched: re.Match[str]) -> str | None:
        """B站视频的同群去重身份（见 :func:`access.video_dedup_identity`）。

        让「分享卡片里的带参长链」与「后来者直接发的裸 BV」认出是同一个视频。
        短链（``b23.tv`` / ``bili2233.cn``）在这里认不出（不联网），由
        :meth:`dedup_expand` 先跳转展开后再取身份。
        """
        return video_dedup_identity(searched)

    async def dedup_expand(self, keyword: str, searched: re.Match[str]):
        """短链先跳一次展开（见 :func:`access.expand_bili_short_link`）。

        展开结果由 ``main._process_url`` 直接交给 ``parse``，所以这次跳转就是
        原来 ``_parse_short_link`` 要跳的那次，不会重复跳、也不会多解析一次。
        """
        return await expand_bili_short_link(self, keyword, searched)

    @handle("/dynamic/", r"bilibili\.com/dynamic/(?P<dynamic_id>\d+)")
    @handle("/opus/", r"bilibili\.com/opus/(?P<dynamic_id>\d+)")
    @handle("t.bili", r"t\.bilibili\.com/(?P<dynamic_id>\d+)")
    async def _parse_dynamic(self, searched: re.Match[str]):
        dynamic_id = int(searched.group("dynamic_id"))
        return await self.parse_dynamic_or_opus(dynamic_id)

    @handle("live.bili", r"live\.bilibili\.com/(?P<room_id>\d+)")
    async def _parse_live(self, searched: re.Match[str]):
        room_id = int(searched.group("room_id"))
        return await self.parse_live(room_id)

    @handle("/favlist", r"favlist\?fid=(?P<fav_id>\d+)")
    async def _parse_favlist(self, searched: re.Match[str]):
        fav_id = int(searched.group("fav_id"))
        return await self.parse_favlist(fav_id)

    @handle("/read/", r"bilibili\.com/read/cv(?P<read_id>\d+)")
    async def _parse_read(self, searched: re.Match[str]):
        from bilibili_api.article import Article
        read_id = int(searched.group("read_id"))
        article = Article(read_id)
        opus = await article.turn_to_opus()
        return await self._parse_bilibli_api_opus(opus)

    async def parse_video(self, *, bvid: str | None = None, avid: int | None = None, page_num: int = 1):
        from .models.video import VideoInfo, AIConclusion

        credential = await self.credential
        video = Video(bvid=bvid, aid=avid, credential=credential)
        video_info = convert(
            await self._call_bili_api_with_retry(video.get_info, operation="视频信息"),
            VideoInfo,
        )
        author = self.create_author(video_info.owner.name, video_info.owner.face)
        page_info = video_info.extract_info_with_page(page_num)

        from ...config import get_config
        pconfig = get_config()

        cid = page_info.cid
        # AI 总结与在线人数都**不在这里**请求：它们是卡片的附加信息，只在真正
        # 渲染卡片时通过 result.card_info_loader 惰性加载，见下面的 fetch_* 闭包。

        url = f"https://bilibili.com/{video_info.bvid}"
        if page_info.index > 0:
            url += f"?p={page_info.index + 1}"

        # 格式化时长
        duration_str = fmt_duration(page_info.duration)

        # 格式化统计数据
        s = video_info.stat
        stats_map = {
            "👍": s.like, "🪙": s.coin, "⭐": s.favorite,
            "↩️": s.share, "💬": s.reply, "👀": s.view, "💭": s.danmaku,
        }

        def fmt_num(n: int) -> str:
            return f"{n / 10000:.1f}万" if n >= 10000 else str(n)

        stats_line = " ".join(f"{k} {fmt_num(v)}" for k, v in stats_map.items() if v > 0)

        # 在线人数同样惰性获取，主解析阶段不再 await。
        if cid is None:
            logger.debug("cid 为 None，跳过在线人数获取")

        # 时长限制提示（大小限制在下载时动态检查）
        limit_warnings = []
        if page_info.duration > pconfig.VIDEO_DURATION_MAXIMUM:
            limit_warnings.append(f"⚠️ 视频时长({duration_str})超过限制({fmt_duration(pconfig.VIDEO_DURATION_MAXIMUM)})，不会下载视频")

        extra = {
            "stats_line": stats_line,
            "duration": duration_str,
            # 由惰性加载器填充；未加载（纯文本/富媒体输出）时保持空串。
            "online": "",
            "content_type": "视频",
            "limit_warnings": limit_warnings,
        }

        async def fetch_online() -> str:
            if cid is None:
                return ""
            online_data = await video.get_online(cid=cid)
            total = int(online_data.get("total", 0))
            count = int(online_data.get("count", 0))
            if total > 0:
                return f"🏄‍♂️ {total} 人正在观看，{count} 人在网页端观看"
            return ""

        async def fetch_ai() -> tuple[str, str]:
            # 只抽有效的 model_result.summary 正文。AIConclusion.summary 在
            # 没有 model_result 时会返回占位错误串，并给正文加「AI总结:」前缀，
            # 这些都不该画到卡片上。
            if not credential:
                return "", "no_credential"
            if cid is None:
                return "", "unavailable"
            ai_result = await self._call_bili_api_with_retry(
                lambda: video.get_ai_conclusion(cid=cid), operation="AI总结",
            )
            ai_conclusion = convert(ai_result, AIConclusion)
            model_result = getattr(ai_conclusion, "model_result", None)
            summary_text = (getattr(model_result, "summary", "") or "").strip()
            if not summary_text:
                return "", "unavailable"
            return summary_text, "ok"

        async def download_video():
            if page_info.duration > pconfig.VIDEO_DURATION_MAXIMUM:
                raise IgnoreException
            output_path = pconfig.cache_dir / f"{video_info.bvid}-{page_info.index + 1}.mp4"
            if output_path.exists():
                return validate_cached_media(output_path, pconfig.VIDEO_SIZE_MAXIMUM_MB)
            # 把缺料通道的 list 直接捕获进来，而不是闭包引用 result ——
            # 后者要靠「create_task 到 result 赋值之间没有 await」这个时序假设，
            # 而 extra["limit_warnings"] 在闭包定义之前就已经存在了。
            v_url, v_backups, a_url, a_backups = await self.extract_download_urls(
                video=video, page_index=page_info.index,
                warnings=limit_warnings, rights=video_info.rights,
                # 自建取流备用路径的输入。用 video_info.bvid 而不是入参 bvid：
                # 用户给的是 avid 时入参为空，而 video_info 里一定有。
                bvid=video_info.bvid, cid=page_info.cid,
            )
            if page_info.duration > pconfig.VIDEO_DURATION_MAXIMUM:
                raise IgnoreException

            url_pairs = [(v_url, a_url)]
            for i, v_bu in enumerate(v_backups):
                a_bu = a_backups[i] if i < len(a_backups) else a_url
                url_pairs.append((v_bu, a_bu))

            last_error = None
            for idx, (v_try, a_try) in enumerate(url_pairs):
                try:
                    if idx > 0:
                        logger.info(f"B站 CDN 重试 ({idx+1}/{len(url_pairs)})")
                    if a_try is not None:
                        return await self.downloader.download_av_and_merge(
                            v_try, a_try, output_path=output_path, ext_headers=self.headers,
                        )
                    else:
                        # 这是**视频**，必须占媒体池：不传 slots 会落到 _image_slots，
                        # 双池设计要防的「小图排队等大视频」就在这条路径上原样发生。
                        return await self.downloader._download_file(
                            v_try, file_name=output_path.name, ext_headers=self.headers,
                            slots=self.downloader._media_slots,
                        )
                except (IgnoreException, MediaProcessException):
                    # 这两类错误换备用地址都不会变好，直接打断、不进下一轮重试：
                    #  - IgnoreException：「体积超限 / 分片超限」是策略跳过，逐个重试只是把
                    #    同一个大文件重下若干遍，最后还报「已尝试所有CDN」（缺料审计会误判）；
                    #  - MediaProcessException：本机缺 ffmpeg 之类的环境问题 —— 备用 CDN 不会
                    #    让本机长出一个 ffmpeg，重试只会把整段视频再下一遍。
                    raise
                except Exception as e:
                    if idx > 0:
                        logger.warning(f"B站 CDN 重试 ({idx+1}/{len(url_pairs)}) 失败: {e}")
                    last_error = e
                    continue

            raise DownloadException("视频下载失败，已尝试所有CDN") from last_error

        video_content = self.create_video(
            # **传工厂而不是 create_task(...)**：下载要等到真正需要这份视频时
            # 才开始。只发文本 / 卡片时不下载整段视频是输出模式的前提，
            # 而这里用 create_task 会在 parse 返回的瞬间就把下载排上。
            PathTask(lambda: download_video()),
            page_info.cover, page_info.duration,
        )

        result = self.result(
            url=url, title=page_info.title, timestamp=page_info.timestamp,
            text=video_info.desc, author=author, contents=[video_content],
            extra=extra,
        )
        from ...card_extras import CardInfoLoader

        result.card_info_loader = CardInfoLoader(
            result, fetch_online=fetch_online, fetch_ai=fetch_ai,
        )
        return result

    async def parse_dynamic_or_opus(self, dynamic_id: int):
        from bilibili_api.dynamic import Dynamic
        from .models.dynamic import DynamicWrapper

        dynamic = Dynamic(dynamic_id, await self.credential)
        if await dynamic.is_article():
            return await self._parse_bilibli_api_opus(dynamic.turn_to_opus())

        dynamic_info = convert(await dynamic.get_info(), DynamicWrapper).item
        return await self._parse_dynamic_info(dynamic_info)

    async def _parse_dynamic_info(self, dynamic_info):
        from .models.dynamic import DynamicInfo

        if dynamic_info.is_video():
            if (major := dynamic_info.modules.major) and (archive := major.archive):
                result = await self.parse_video(bvid=archive.bvid)
                result.text = dynamic_info.text
                result.extra["content_type"] = "动态"
                return result

        author = self.create_author(dynamic_info.name, dynamic_info.avatar)
        contents: list[MediaContent] = []
        contents.extend(self.create_images(dynamic_info.image_urls))

        repost = None
        if dynamic_info.type == "DYNAMIC_TYPE_FORWARD" and dynamic_info.orig is not None:
            repost = await self._parse_dynamic_info(dynamic_info.orig)

        return self.result(
            title=dynamic_info.title, text=dynamic_info.text,
            timestamp=dynamic_info.timestamp, author=author,
            contents=contents, repost=repost, extra={"content_type": "动态"},
        )

    async def parse_opus_by_id(self, opus_id: int):
        opus = Opus(opus_id, await self.credential)
        return await self._parse_bilibli_api_opus(opus)

    async def _parse_bilibli_api_opus(self, bili_opus: Opus):
        from .models.opus import OpusItem

        opus_info = await bili_opus.get_info()
        if not isinstance(opus_info, dict):
            raise ParseException("获取图文动态信息失败")

        opus_data = convert(opus_info, OpusItem)
        author = self.create_author(*opus_data.name_avatar)

        result = self.result(author=author, title=opus_data.title, timestamp=opus_data.timestamp)
        for node in opus_data.extract_nodes():
            if isinstance(node, str):
                result.graphics.append(node)
            else:
                result.graphics.append(self.create_image(node.url, alt=node.alt))
        return result

    async def parse_live(self, room_id: int):
        from bilibili_api.live import LiveRoom
        from .models.live import RoomData

        room = LiveRoom(room_display_id=room_id, credential=await self.credential)
        info_dict = await room.get_room_info()
        room_data = convert(info_dict, RoomData)
        contents: list[MediaContent] = []
        if cover := room_data.cover:
            contents.append(self.create_image(self.downloader.download_img(cover, ext_headers=self.headers)))
        if keyframe := room_data.keyframe:
            contents.append(self.create_image(self.downloader.download_img(keyframe, ext_headers=self.headers)))
        author = self.create_author(room_data.name, room_data.avatar)
        url = f"https://www.bilibili.com/blackboard/live/live-activity-player.html?enterTheRoom=0&cid={room_id}"
        return self.result(url=url, title=room_data.title, text=room_data.detail,
                          contents=contents, author=author, extra={"content_type": "直播"})

    async def parse_favlist(self, fav_id: int):
        from bilibili_api.favorite_list import get_video_favorite_list_content
        from .models.favlist import FavData

        fav_dict = await get_video_favorite_list_content(fav_id)
        if fav_dict["medias"] is None:
            raise ParseException("收藏夹内容为空, 或被风控")
        favdata = convert(fav_dict, FavData)
        author = self.create_author(favdata.info.upper.name, favdata.info.upper.face)
        graphics: list[str | ImageContent] = []
        for fav in favdata.medias:
            graphics.append(self.create_image(fav.cover, alt=fav.desc))
            graphics.append(fav.desc)
        return self.result(title=favdata.title, timestamp=favdata.timestamp,
                          author=author, graphics=graphics, extra={"content_type": "收藏夹"})

    @staticmethod
    def _append_access_warning(warnings: list[str] | None, message: str) -> None:
        """把可访问性结论写进缺料通道（同一条只记一次）。

        ``warnings`` 为 None 时什么都不做 —— ``tools/probe_bili_e2e.py`` 之类的
        探测脚本只关心 URL，不该被这一步绊住。
        """
        if not message or warnings is None:
            return
        if message not in warnings:
            warnings.append(message)

    async def _fallback_stream_urls(
        self,
        bvid: str,
        cid: int,
        target_quality,
        *,
        cookie_header: str = "",
        warnings: list[str] | None = None,
    ):
        """自建取流备用路径。拿不到就返回 ``None``，让调用方按原逻辑走。

        **什么时候才会走到这里**：``bilibili-api-python`` 调不动了 —— 接口返回形状
        变了、版本不兼容、或者压根装不上。这条路不依赖那个库，只用 httpx 加
        一次 WBI 签名（见 :mod:`.fallback`）。

        **什么时候不该走**：「没权限」。大会员专享、充电专属这类视频换个客户端
        去请求也拿不到流，白白多打一次请求还会把日志搅浑，所以调用方要先用
        ``restriction_type`` 判断。

        :returns: ``(v_url, v_backups, a_url, a_backups)``，取不到返回 ``None``。
        """
        from astrbot.api import logger

        from .fallback import fetch_streams
        from .fallback.wbi import WbiKeyProvider
        from .fallback.fetch import fetch_single_file
        # get_config 在本文件里一律**方法内 import**（顶层不引，见 parse_video /
        # extract_download_urls）。这里漏掉过一次：本函数自己的
        # ``except Exception`` 会把随之而来的 NameError 吞成 ``return None``，
        # 外部完全看不出异常 —— 整条「主力报错 → 自建接管」路径静默失效。
        from ...config import get_config

        try:
            max_qn = int(getattr(target_quality, "value", 0) or 0)
        except (TypeError, ValueError):
            max_qn = 0

        try:
            if getattr(self, "_fallback_key_provider", None) is None:
                self._fallback_key_provider = WbiKeyProvider(lambda: self.new_client())
            video, audio, _raw = await fetch_streams(
                lambda: self.new_client(),
                bvid,
                cid,
                headers=self.headers,
                cookie_header=cookie_header,
                codec_preference=get_config().BILI_CODEC,
                max_qn=max_qn,
                key_provider=self._fallback_key_provider,
            )
        except Exception as error:
            logger.warning(f"[bili] 自建取流备用路径也失败: {error}")
            return None

        if not video:
            return None

        access = analyze_play_access(_raw, has_cookie=bool(cookie_header))
        self._append_access_warning(warnings, access["message"])
        if access["status"] == STATUS_BLOCKED:
            raise IgnoreException(access["message"] or "无法获取视频流")

        if audio and not ffmpeg_available():
            try:
                pair, raw = await fetch_single_file(
                    lambda: self.new_client(), bvid, cid,
                    headers=self.headers, cookie_header=cookie_header,
                )
            except Exception:
                pair, raw = None, {}
                logger.warning("[bili] 自建单文件取流失败", exc_info=True)
            if pair:
                access = analyze_play_access(raw, has_cookie=bool(cookie_header))
                if access["status"] == STATUS_FULL:
                    return pair[0], pair[1], None, []
            message = "未安装 ffmpeg，无法获取可直接发送的视频，已跳过下载"
            self._append_access_warning(warnings, message)
            raise IgnoreException(message)

        logger.info(
            f"[bili] 已切到自建取流备用路径（bilibili-api-python 未生效）: {bvid} p{cid}"
        )
        v_url, v_backups = video
        if audio:
            a_url, a_backups = audio
            return v_url, v_backups, a_url, a_backups
        return v_url, v_backups, None, []

    async def extract_download_urls(self, video: Video | None = None, *, bvid: str | None = None,
                                     avid: int | None = None, page_index: int = 0,
                                     warnings: list[str] | None = None,
                                     rights: dict | None = None,
                                     cid: int | None = None):
        """取视频/音频流地址。

        ``bvid`` + ``cid`` 是**自建取流备用路径**的输入。缺了它们，备用路径不可用
        （见 :meth:`_fallback_stream_urls`）——主力路径不受影响。
        """
        from bilibili_api.video import (
            AudioStreamDownloadURL, VideoStreamDownloadURL, FLVStreamDownloadURL,
            MP4StreamDownloadURL, VideoDownloadURLDataDetecter, VideoQuality, VideoCodecs,
        )
        from ...config import get_config

        # 清晰度字符串 → VideoQuality 枚举映射
        QUALITY_MAP = {
            "360P": VideoQuality._360P,
            "480P": VideoQuality._480P,
            "720P": VideoQuality._720P,
            "1080P": VideoQuality._1080P,
            "1080P+": VideoQuality._1080P_PLUS,
            "4K": VideoQuality._4K,
            "8K": VideoQuality._8K,
        }

        # 从配置读取用户设置的清晰度，不区分大小写
        pconfig = get_config()
        raw_quality = pconfig.BILI_QUALITY.strip().upper().replace("＋", "+")
        target_quality = QUALITY_MAP.get(raw_quality, VideoQuality._1080P)

        credential = await self.credential
        if video is None:
            video = Video(bvid=bvid, aid=avid, credential=credential)

        try:
            download_url_data = await self._call_bili_api_with_retry(
                lambda: video.get_download_url(page_index=page_index),
                operation="视频流地址",
            )
        except IgnoreException:
            raise
        except Exception as error:
            # **受限视频在这里是抛异常**（`ResponseCodeException`，如 code=-10403
            # 大会员专享）。不接住的话会被 download_video 的 CDN 重试循环当成网络故障，
            # 逐个备用地址重试一遍，最后报「视频下载失败，已尝试所有CDN」——
            # 把「没权限」说成「网络问题」，用户不知道该去登录还是该重试。
            # 这里分析成一句人话原因，再抛 IgnoreException（策略跳过），
            # 缺料审计就不会再加一条「下载失败」的自相矛盾警告。
            access = analyze_play_access(
                error=error, rights=rights, has_cookie=credential is not None,
            )
            # **接口层面坏了**（B站改了返回形状 / bilibili_api 版本不兼容）时，
            # 换自建路径再试一次。**「没权限」不走这条路** —— 换个客户端去请求
            # 一样拿不到流，只会把日志搅浑。
            if not access["restriction_type"] and bvid and cid is not None:
                fb = await self._fallback_stream_urls(
                    bvid, cid, target_quality, cookie_header=self._bili_ck or "", warnings=warnings,
                )
                if fb:
                    return fb
            self._append_access_warning(warnings, access["message"])
            raise IgnoreException(access["message"] or "无法获取视频流") from error

        access = analyze_play_access(
            download_url_data, rights=rights, has_cookie=credential is not None,
        )
        self._append_access_warning(warnings, access["message"])
        if access["status"] == STATUS_BLOCKED:
            # 有响应但拿不到可播放流：同样是「知道原因的跳过」，不是 CDN 故障
            raise IgnoreException(access["message"] or "无法获取视频流")

        detecter = VideoDownloadURLDataDetecter(download_url_data)
        streams = detecter.detect_best_streams(
            video_max_quality=target_quality,
            codecs=self._codec_order(VideoCodecs, get_config().BILI_CODEC),
            no_dolby_video=True, no_hdr=True,
        )

        # 筛选视频流和音频流
        video_stream = next(
            (s for s in streams if isinstance(s, (VideoStreamDownloadURL, FLVStreamDownloadURL, MP4StreamDownloadURL))),
            None,
        )
        audio_stream = next(
            (s for s in streams if isinstance(s, AudioStreamDownloadURL)), None,
        )

        # DASH 拿不到流、或拿到的流低于目标清晰度时（典型：未登录），
        # 回退 html5 单文件 MP4。
        # 实测（2026-09-17，匿名）：DASH 只给到 480P，html5 durl 却是 720P——
        # 同一个 5 分钟视频 35.3MB，而 DASH 360P 只有 11.2MB。
        #
        # **还有第三种触发条件：本机没有 ffmpeg**。DASH 是音视频分离流，必须
        # 合并才能播放；缺 ffmpeg 时硬着头皮下载，等于把整段视频（往往上百 MB）
        # 下完再发现合并不了。所以在这一刻就要改拿单文件。
        need_merge = audio_stream is not None
        ffmpeg_missing = need_merge and not ffmpeg_available()
        need_mp4_fallback = (
            video_stream is None
            or self._below_target(video_stream, target_quality)
            or ffmpeg_missing
        )
        if need_mp4_fallback:
            dash_qid = getattr(getattr(video_stream, "video_quality", None), "value", None)
            # 缺 ffmpeg 时**故意不传 better_than**：宁可降画质也要给一个带声音的
            # 完整文件。按档位比较会让「目标 4K、DASH 给 1080P、html5 只给 720P」
            # 直接放弃回退，然后仍然因为没有 ffmpeg 而失败 —— 白绕一圈。
            mp4_url, mp4_backups = await self._try_html5_mp4(
                video, page_index, target_quality,
                better_than=None if ffmpeg_missing else dash_qid,
            )
            if mp4_url:
                # html5 durl 是音视频合并好的单文件，没有独立音轨
                return mp4_url, mp4_backups, None, []

        if video_stream is None:
            # 主路径调用成功但没解析出视频流：同样是 bilibili_api 侧失灵的表现，
            # 交给自建路径再试一次。
            if bvid and cid is not None:
                fb = await self._fallback_stream_urls(
                    bvid, cid, target_quality, cookie_header=self._bili_ck or "", warnings=warnings,
                )
                if fb:
                    return fb
            raise DownloadException("未找到可下载的视频流")

        if ffmpeg_missing:
            # 到这里说明「必须合并」且「合并不了」，且单文件回退也没拿到。
            # 在**开始下载之前**就停掉：继续走就是下完再失败。
            # 抛 IgnoreException（按策略跳过）而不是 DownloadException，缺料审计
            # 不会把它算成下载失败；同时把原因写进既有提示通道，用户在已选的
            # 文本 / 卡片里能看到一句人话。
            message = (
                "本机未安装 ffmpeg，无法合并B站音视频分离流，"
                "且未取到可用的单文件视频，已跳过下载"
            )
            self._append_access_warning(warnings, message)
            raise IgnoreException(message)

        v_backups = video_stream.backup_url if isinstance(video_stream, VideoStreamDownloadURL) else []
        a_backups = audio_stream.backup_url if audio_stream and isinstance(audio_stream, AudioStreamDownloadURL) else []

        if audio_stream is None:
            return video_stream.url, v_backups, None, []

        return video_stream.url, v_backups, audio_stream.url, a_backups

    # 清晰度档位 → 期望高度（用于判断 DASH 实际给到的是否够用）
    _QUALITY_HEIGHT = {
        "360P": 360, "480P": 480, "720P": 720,
        "1080P": 1080, "1080P+": 1080, "4K": 2160, "8K": 4320,
    }

    def _below_target(self, video_stream, target_quality) -> bool:
        """DASH 实际流高度是否明显低于目标档位。"""
        try:
            height = int(getattr(video_stream, "scale", (0, 0))[1] or 0)
        except (TypeError, ValueError, IndexError):
            height = 0
        if not height:
            return False
        target_height = self._QUALITY_HEIGHT.get(
            str(getattr(target_quality, "name", "")).replace("_PLUS", "").replace("_", "").upper(),
            self._QUALITY_HEIGHT.get(str(target_quality).upper()),
        )
        if not target_height:
            return False
        return height < target_height

    async def _try_html5_mp4(self, video, page_index: int, target_quality, better_than: int | None = None):
        """尝试 html5 接口拿音视频合并的单文件 MP4，返回 (url, backups)。

        ``better_than`` 为 DASH 已拿到的清晰度 id；若 html5 档位不高于它则放弃，
        避免「目标设 4K、DASH 给了 1080P、html5 只给 720P」时反而被降级。

        **只接受 ``durl`` 恰好一项的情况**：多项 durl 是同一视频的**顺序分段**
        （对照本平台 ``access.py`` 用 ``durl[].length`` 求和来识别试看片段），
        不是同一文件的 CDN 备用地址。把第 2 段当备用地址，换 CDN 时就会拿到
        视频中段 —— 表现为「发出去的只有中间一截且可能无声」。真正的备用地址
        是**该项自带的** ``backup_url``。
        """
        from astrbot.api import logger

        try:
            data = await video.get_download_url(page_index=page_index, html5=True)
        except Exception as e:
            logger.debug(f"[bili] html5 MP4 回退不可用: {e}")
            return None, []

        served = data.get("quality")
        if better_than is not None and isinstance(served, int) and served <= better_than:
            logger.debug(
                f"[bili] html5 档位 {served} 不高于 DASH 的 {better_than}，放弃回退"
            )
            return None, []

        durl = list(data.get("durl") or [])
        # **按原始长度判定**。先滤掉「没有 url 的条目」再数，等于把分段流
        # 悄悄洗成单文件：`[{"url": 第1段}, {}]` 这种畸形第二段被丢掉后，
        # 第 1 段就被当成完整视频发出去 —— 用户收到一段没有声音的残片。
        if len(durl) != 1:
            logger.debug(f"[bili] html5 返回 {len(durl)} 段 durl，不是单文件，回退不可用")
            return None, []
        item = durl[0]
        if not isinstance(item, dict) or not item.get("url"):
            return None, []

        # **必须分析 html5 自己这份响应**，不能沿用原始 DASH 的结论：那是在
        # html5 请求之前算的。html5 很可能给的是试看片段（is_preview=1），
        # 拿它当「完整文件」发出去，就是把截断内容当正常结果。
        access = analyze_play_access(payload=data, has_cookie=bool(self._bili_ck))
        if access.get("status") != STATUS_FULL:
            logger.debug(
                f"[bili] html5 返回的不是完整文件"
                f"（status={access.get('status')}），放弃回退"
            )
            return None, []

        backups = item.get("backup_url") or item.get("backupUrl") or []
        if isinstance(backups, str):
            backups = [backups]
        url = item["url"]
        logger.info(
            f"[bili] 使用 html5 单文件 MP4 回退（服务端 quality={served}，"
            f"目标 {getattr(target_quality, 'name', target_quality)}）"
        )
        return url, [u for u in backups if u]

    def _save_credential(self):
        if self._credential is None or self._cookies_file is None:
            return
        self._cookies_file.write_text(json.dumps(self._credential.get_cookies()))

    def _load_credential(self):
        if self._cookies_file is None or not self._cookies_file.exists():
            return
        try:
            self._credential = Credential.from_cookies(json.loads(self._cookies_file.read_text()))
        except Exception as e:
            logger.error(f"加载已保存的凭证失败: {e}")

    def _save_cookie_str(self, cookie_str: str):
        """将cookie字符串持久化保存，供下次启动时自动加载"""
        if self._cookies_file is None:
            return
        try:
            ck_dict = ck2dict(cookie_str)
            self._cookies_file.parent.mkdir(parents=True, exist_ok=True)
            self._cookies_file.write_text(json.dumps(ck_dict))
            logger.info("B站 Cookie 已持久化保存")
        except Exception as e:
            logger.error(f"保存 Cookie 失败: {e}")

    async def _init_credential(self):
        # **配置项优先**（与 main.apply_runtime_config 的取值顺序保持一致）：
        # BILI_CK 是用户显式填的，持久化文件只是扫码登录的暂存。
        # 反过来的话，网页上改了 BILI_CK 会被旧登录态压住 —— 接口报成功、实际不生效。
        if self._bili_ck:
            credential = Credential.from_cookies(ck2dict(self._bili_ck))
            if await credential.check_valid():
                logger.info(f"B站配置中的 Cookie 有效, 已持久化保存")
                self._credential = credential
                self._save_credential()
                self._save_cookie_str(self._bili_ck)
                return
            logger.info("B站配置中的 Cookie 已过期")

        # 其次才是扫码登录持久化下来的文件
        self._load_credential()
        if self._credential is not None:
            if await self._credential.check_valid():
                logger.info("从持久化文件加载的B站 Cookie 有效")
                return
            logger.info("持久化文件中的 Cookie 已过期")

    def update_cookie(self, cookie_str: str):
        """运行时更新B站Cookie，立即生效"""
        if not cookie_str:
            return
        self._bili_ck = cookie_str
        self._credential = None
        # 持久化保存，重启后自动生效
        self._save_cookie_str(cookie_str)
        logger.info("B站 Cookie 已更新，将在下次请求时重新初始化凭证")

    def clear_cookie(self):
        """清除运行时与持久化的 Cookie。

        ``update_cookie("")`` 是空值早退的，清不掉内存里的凭据，
        所以「清除 Cookie」必须走这个方法：连同自己持久化的那份一起删。
        """
        self._bili_ck = None
        self._credential = None
        if self._cookies_file is not None:
            try:
                self._cookies_file.unlink(missing_ok=True)
            except OSError:
                logger.warning("删除 B站 Cookie 文件失败", exc_info=True)
                return False
        logger.info("B站 Cookie 已清除")
        return True

    @property
    async def credential(self) -> Credential | None:
        if self._credential is None:
            await self._init_credential()
            if self._credential is None:
                return None
            return self._credential

        # 已过期时尝试重新初始化
        if not await self._credential.check_valid():
            logger.warning("哔哩哔哩凭证已过期, 尝试重新初始化")
            self._credential = None
            await self._init_credential()
            if self._credential is None:
                return None

        # 尝试刷新
        if self._credential and await self._credential.check_refresh():
            logger.info("哔哩哔哩凭证需要刷新")
            if self._credential.has_ac_time_value() and self._credential.has_bili_jct():
                await self._credential.refresh()
                self._save_credential()

        return self._credential
