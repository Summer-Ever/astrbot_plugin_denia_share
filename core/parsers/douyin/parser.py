# 本文件包含衍生自 astrbot_plugin_rika_share（MIT License）的代码，
# 上游项目：https://github.com/iris1598/astrbot_plugin_rika_share
# 本仓库对其做过修改；完整归属见项目根目录 README「许可与致谢」。

"""抖音解析器。

双路径取数，任一成功即可：

1. **轻量 HTML 路径**：抓取 iesdouyin / m.douyin 分享页的
   ``window._ROUTER_DATA``。零签名成本，是首选路径。抖音只在请求带有效
   ``ttwid`` 时才把作品数据放进 SSR，所以抓取必须走 :mod:`.session` 里的会话
   （保持 cookie + 重试），不能每次新建 client 只发一次请求。
2. **签名 Web API 路径**：``/aweme/v1/web/aweme/detail/`` 配合
   a_bogus 签名 + ttwid 会话，稳定性更好，是 HTML 路径失败后的兜底。
"""

import re
from pathlib import Path
from typing import Any, ClassVar

import aiohttp
from httpx import AsyncClient
from astrbot.api import logger

from ...base_parser import BaseParser, PlatformEnum, ParseException, handle
from ...data import Platform, platform_of
from ...task import PathTask
from .models.video import unwrap_audio_url
from ...media_utils import fmt_duration


class DouyinParser(BaseParser):
    platform: ClassVar[Platform] = platform_of(PlatformEnum.DOUYIN)

    def __init__(self, downloader):
        super().__init__(downloader)
        self._web_client = None

    # ------------------------------------------------------------------ #
    # 大视频压缩（DOUYIN_COMPRESS_SIZE_MB）
    # ------------------------------------------------------------------ #

    def create_video(self, url_or_task, cover_url=None, duration=None, is_gif=False):
        """重写基类：抖音视频在下载完成后按阈值压缩到 720p。

        抖音大视频直发 QQ 常因文件过大被协议端拒绝，压缩能显著提高送达率；
        阈值由配置 ``DOUYIN_COMPRESS_SIZE_MB`` 控制（0 = 关闭）。

        只有「URL 直传」的路径需要包压缩工厂 —— 传 Task / PathTask 的调用方
        已经自己管下载，不该被二次包装。压缩阈值在惰性工厂**内部**读取：
        仅文本/仅卡片模式下不会有人 get 这个任务，构造时连配置都不用碰，
        改完配置也无需任何热更新钩子即可生效。
        """
        if isinstance(url_or_task, str):
            return super().create_video(
                PathTask.lazy(lambda url=url_or_task: self._download_and_compress(url)),
                cover_url=cover_url,
                duration=duration,
                is_gif=is_gif,
            )
        return super().create_video(
            url_or_task, cover_url=cover_url, duration=duration, is_gif=is_gif
        )

    async def _download_and_compress(self, url: str) -> Path:
        """下载抖音视频，然后按当前配置决定是否压缩。

        配置读取包在 try 里：解析器其余部分不依赖任何配置项，这里同样不能让
        「配置不可用」把整条视频下载炸掉 —— 读不到就当关闭压缩。
        """
        path = await self.downloader.download_video(url, ext_headers=self.headers)
        try:
            from ...config import get_config
            from ...media_utils import compress_video_if_needed

            threshold = get_config().DOUYIN_COMPRESS_SIZE_MB
        except Exception:
            logger.debug("[douyin] 读取压缩阈值失败，跳过压缩", exc_info=True)
            return path
        if threshold <= 0:
            return path
        return await compress_video_if_needed(path, threshold)

    # ------------------------------------------------------------------ #
    # URL 注册
    # ------------------------------------------------------------------ #

    @handle("v.douyin", r"v\.douyin\.com/[a-zA-Z0-9_\-]+")
    @handle("jx.douyin", r"jx\.douyin\.com/[a-zA-Z0-9_\-]+")
    async def _parse_short_link(self, searched: re.Match[str]):
        url = f"https://{searched.group(0)}"
        return await self.parse_with_redirect(url)

    @handle("douyin", r"douyin\.com/(?P<ty>video|note|slides)/(?P<vid>\d+)")
    @handle("iesdouyin", r"iesdouyin\.com/share/(?P<ty>slides|video|note)/(?P<vid>\d+)")
    @handle("m.douyin", r"m\.douyin\.com/share/(?P<ty>slides|video|note)/(?P<vid>\d+)")
    @handle("jingxuan.douyin", r"jingxuan\.douyin\.com/m/(?P<ty>slides|video|note)/(?P<vid>\d+)")
    async def _parse_douyin(self, searched: re.Match[str]):
        ty, vid = searched.group("ty"), searched.group("vid")
        # canonical **按参数传下去**，不挂在 self 上：解析器实例跨消息复用，
        # 存实例属性会让并发请求互相覆盖又互相清空（图文请求可能因此拿到
        # 上一条视频请求留下的 /video/ 链接）。
        result = await self._fetch_douyin(ty, vid)
        # 补规范链接：下面三条路径返回的结果都可能没有 url，而卡片页脚左侧画的就是它。
        # 这里用 handler 抓到的作品号拼一条 —— 用户常发的是 v.douyin.com 短链或带口令的
        # 分享文案，两者显示出来只是一串无意义短码；展开成 /video/<作品号> 才是用户
        # 认得的那个「号码」。只在为空时补，解析器自己给出的更优先。
        if not result.url:
            result.url = f"https://www.douyin.com/{ty}/{vid}"
        return result

    async def _fetch_douyin(self, ty: str, vid: str):
        # 重置本条作品的请求预算。**必须在这里、只重置一次**：预算要跨下面所有
        # URL 变体共享 —— 4 个 URL 各自重试一轮就是最坏 48 次 GET，
        # 正是最容易把出口 IP 打进限流页的形状（见 session.MAX_REQUESTS_PER_WORK）。
        from .session import get_share_session

        get_share_session(type(self)).start_work()

        # 规范链接跟着请求走（不存实例状态），图文才不会退化成 /video/。
        canonical = f"https://www.douyin.com/{ty}/{vid}"

        # 1) HTML _ROUTER_DATA 路径。零签名成本，配会话 cookie 后最稳。
        #    slides 类型也试 /share/note/<id>/ —— 抖音两种写法都有，
        #    而 /share/slides/<id>/ 现在不返回 _ROUTER_DATA。
        html_types = (ty, "note") if ty == "slides" else (ty,)
        for html_type in html_types:
            for url in (
                self._build_m_douyin_url(html_type, vid),
                self._build_iesdouyin_url(html_type, vid),
            ):
                try:
                    return await self.parse_video(url)
                except ParseException as e:
                    logger.debug(f"[douyin] HTML 路径失败 {url}: {e}")
                    continue

        # 2) 图文页再试 slidesinfo 接口。
        #    注意：该接口 2026-09 起对绝大多数作品返回 aweme_details=null、
        #    filter_list reason=8，实际已失效，只作为兜底保留。
        if ty in ("slides", "note"):
            try:
                return await self.parse_slides(vid)
            except Exception as e:
                logger.debug(f"[douyin] slidesinfo 路径失败，转签名接口: {e}")

        # 3) 兜底：签名 Web API
        return await self.parse_by_web_api(vid, canonical_url=canonical)

    @staticmethod
    def _build_iesdouyin_url(ty: str, vid: str) -> str:
        return f"https://www.iesdouyin.com/share/{ty}/{vid}"

    @staticmethod
    def _build_m_douyin_url(ty: str, vid: str) -> str:
        return f"https://m.douyin.com/share/{ty}/{vid}"

    # ------------------------------------------------------------------ #
    # 音频（图文配乐 / 原生纯音频）
    # ------------------------------------------------------------------ #

    @staticmethod
    def _background_music_enabled() -> bool:
        """图文配乐开关。**读不到配置时按关闭处理**，不能让一个装饰性开关
        把整条解析炸掉 —— 解析器的其余部分本来不依赖任何配置项。"""
        try:
            from ...config import get_config

            return bool(getattr(get_config(), "SEND_BACKGROUND_MUSIC", False))
        except Exception:
            return False

    @classmethod
    def _extract_audio(cls, item: dict) -> tuple[str, float | None] | None:
        """从签名 API 的原始 dict 里取出作品音频（URL + 秒级时长）。

        与模型层的 :func:`unwrap_audio_url` 共用同一套判定，所以三条取数路径
        （HTML / slidesinfo / 签名 API）对「什么算音频」不会分叉。

        **不探测、不下载**：这里只做纯 URL 形状判断。任何 HEAD/GET 都会让
        「仅文本」输出也去碰网络，惰性就废了。
        """
        if not isinstance(item, dict):
            return None

        music = item.get("music") or {}
        seconds = None
        if isinstance(music, dict):
            duration = music.get("duration")
            # music.duration 单位是**秒**；非数字/非正数就当没有，别猜。
            if isinstance(duration, (int, float)) and not isinstance(duration, bool) and duration > 0:
                seconds = float(duration)
            play_url = music.get("play_url") or music.get("playUrl") or {}
            if found := cls._first_url(play_url):
                if real := unwrap_audio_url(found):
                    return real, seconds

        # 样本形状：真正的音频直链藏在 video.play_addr 的包装参数里。
        # 时长仍取 music.duration —— 包装地址本身不带时长信息。
        video = item.get("video") or {}
        if isinstance(video, dict):
            for key in ("play_addr", "playAddr", "PlayAddrStruct"):
                found = cls._first_url(video.get(key))
                if found and (real := unwrap_audio_url(found)):
                    return real, seconds
        return None

    def _attach_audio(self, result, audio: tuple[str, float | None] | None, *, has_images: bool):
        """按既定策略把音频挂到结果上，返回是否真的挂了。

        三条规则（顺序即优先级）：

        1. **原生纯音频**（既无图也无视频）：无条件产出 AudioContent。
           这是作品本身就是一段音乐，不属于「背景音乐」，不该被开关拦掉。
        2. **图文配乐**（有图）：只在 ``SEND_BACKGROUND_MUSIC`` 打开时附带，
           否则维持现状（只发图），避免更新后所有图文突然多发一段音频。
        3. **普通视频**：调用方根本不进来 —— 视频自带声音，再挂一段配乐
           既重复又可能是被裁过的片段。
        """
        if audio is None:
            return False
        url, duration = audio
        if has_images and not self._background_music_enabled():
            return False
        # 时长上限检查放在建任务**之前**：超限时用户需要知道「为什么没有音频」，
        # 而不是拿到一个安静的空结果。这里的音频和视频限制都以秒计。
        if duration:
            self._add_limit_warning(result, duration, media_type="音频")
            # 纯音频作品没有视频时长可显示，把音频秒数放进 extra，
            # 好让卡片/文本能带上「66 秒」这类信息；已有真实视频时长则不覆盖。
            if result.video is None and not result.extra.get("duration"):
                result.extra["duration"] = fmt_duration(duration)
        result.contents.append(self.create_audio(url, duration or 0.0))
        return True

    # ------------------------------------------------------------------ #
    # 路径一：HTML _ROUTER_DATA
    # ------------------------------------------------------------------ #

    async def parse_video(self, url: str):
        from .models.video import decoder as video_decoder
        from .session import get_share_session, looks_like_challenge

        text = await get_share_session(type(self)).fetch(url, self.ios_headers)

        if looks_like_challenge(text):
            raise ParseException("抖音风控挑战未通过，稍后再试")

        pattern = re.compile(pattern=r"window\._ROUTER_DATA\s*=\s*(.*?)</script>", flags=re.DOTALL)
        matched = pattern.search(text)
        if not matched or not matched.group(1):
            raise ParseException("can't find _ROUTER_DATA in html")

        try:
            video_data = video_decoder.decode(matched.group(1).strip()).video_data
        except ParseException:
            raise
        except Exception as e:
            # msgspec 的 ValidationError 也走这里：SSR 结构变了就落到下一条路径，
            # 而不是抛一个非 ParseException 把整条解析链路炸掉
            raise ParseException(f"分享页数据结构无法识别({type(e).__name__})") from e

        author = self.create_author(video_data.author.nickname, video_data.avatar_url)
        result = self.result(title=video_data.desc, author=author, timestamp=video_data.create_time)

        audio = self._audio_from_data(video_data)
        if image_urls := video_data.image_urls:
            result.contents.extend(self.create_images(image_urls))
            self._attach_audio(result, audio, has_images=True)
        elif video_url := video_data.video_url:
            self._add_limit_warning(result, video_data.duration)
            result.video = self.create_video(video_url, video_data.cover_url, video_data.duration)
        elif audio is not None:
            # 无图无视频但有真实音频：作品本身就是一段音乐，不受配乐开关限制。
            self._attach_audio(result, audio, has_images=False)
        else:
            # 既没有图片也没有视频：多半是 SSR 结构变了、或命中了另一种页面变体。
            # 必须抛出去让 _parse_douyin 继续走后面的路径与兜底接口 —— 返回一个
            # 「只有标题、零媒体、零警告」的空结果，用户会收到一张空卡片却不知道为什么。
            raise ParseException("分享页里既没有图片也没有视频")
        return result

    @staticmethod
    def _audio_from_data(video_data) -> tuple[str, float | None] | None:
        """HTML/slides 两条 msgspec 路径共用的音频提取。"""
        url = video_data.audio_url
        if not url:
            return None
        return url, video_data.audio_duration

    async def parse_slides(self, video_id: str):
        from .models.slides import decoder as slides_decoder

        url = "https://www.iesdouyin.com/web/api/v2/aweme/slidesinfo/"
        params = {"aweme_ids": f"[{video_id}]", "request_source": "200"}
        async with AsyncClient(
            **self.client_kwargs(headers=self.android_headers)
        ) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()

        aweme_details = slides_decoder.decode(response.content).aweme_details
        if not aweme_details:
            # 裸下标 [] 会抛 IndexError —— 虽然这一条被 _parse_douyin 的裸
            # except Exception 接得住（会继续走签名 API），但日志只剩
            # "list index out of range"，看不出是「接口没给作品」。
            raise ParseException("图文接口没有返回任何作品")
        slides_data = aweme_details[0]
        author = self.create_author(slides_data.name, slides_data.avatar_url)
        result = self.result(title=slides_data.desc, author=author, timestamp=slides_data.create_time)

        # **静态图优先。** 实拍图集（slides）里每张图都同时带一份静态图和一段内嵌视频，
        # 原先是「有内嵌视频就用内嵌视频」，于是整条图集被当成一串视频发出去 ——
        # 用户看到的就是「解析图集变成了视频」，而静态图全被丢掉。
        # 内嵌视频只在**没有任何静态图**时才用，那种作品本质就是视频。
        audio = self._audio_from_data(slides_data)
        if image_urls := slides_data.image_urls:
            result.contents.extend(self.create_images(image_urls))
            self._attach_audio(result, audio, has_images=True)
        elif dynamic_urls := slides_data.dynamic_urls:
            for dynamic_url in dynamic_urls:
                result.contents.append(self.create_video(dynamic_url))
        elif audio is not None:
            self._attach_audio(result, audio, has_images=False)
        else:
            # 静态图与内嵌视频段都没有：这条接口没给媒体。抛出去让 _parse_douyin
            # 接着走签名 Web API 兜底，而不是返回一个零媒体的空结果。
            raise ParseException("图文接口里既没有图片也没有视频段")
        return result

    # ------------------------------------------------------------------ #
    # 路径二：签名 Web API
    # ------------------------------------------------------------------ #

    def _get_web_client(self):
        if self._web_client is None:
            from .web import DouyinWebClient

            self._web_client = DouyinWebClient()
        return self._web_client

    async def parse_by_web_api(self, item_id: str, canonical_url: str | None = None):
        """canonical_url 可选：直接调用本方法（不经 handler）时不会传，
        此时退回 /video/<id>，保持旧行为。"""
        client = self._get_web_client()
        async with aiohttp.ClientSession() as session:
            data = await client.fetch_detail(session, item_id)

        if not data:
            # 让 web.py 把真实原因带出来（403 风控 / 网络失败 / 内容确实没了）。
            # 一律报"分享已删除"会把排查方向带偏 —— 实测本机就是被 403 风控拦的。
            raise ParseException(
                getattr(client, "last_error", "")
                or "分享已删除或资源直链提取失败, 请稍后再试"
            )

        item = self._pick_item(data, item_id)
        if item is None:
            raise ParseException("响应中未包含目标作品")

        return self._build_result_from_item(item, canonical_url=canonical_url)

    @staticmethod
    def _pick_item(data: dict, item_id: str) -> dict | None:
        candidates: list[dict] = []
        detail = data.get("aweme_detail")
        if isinstance(detail, dict):
            candidates.append(detail)
        for key in ("aweme_details", "aweme_list", "item_list"):
            value = data.get(key)
            if isinstance(value, list):
                candidates.extend(i for i in value if isinstance(i, dict))
        for item in candidates:
            if str(item.get("aweme_id") or item.get("id") or "") == str(item_id):
                return item
        # 匹配不上就返回 None，让调用方报错。
        # 旧实现在这里兜底返回 candidates[0]，会把「响应里没有目标作品」伪装成成功，
        # 静默解析出一条**完全不相干的作品**（实测会把另一个视频当成本次结果发出去）。
        return None

    # ---- 媒体 URL 提取 ---- #

    @staticmethod
    def _first_url(value: Any) -> str | None:
        """从 url_list / 字符串 / 嵌套结构中取出第一个 http(s) 链接。"""
        if isinstance(value, str):
            return value if value.startswith(("http://", "https://")) else None
        if isinstance(value, list):
            for item in value:
                if found := DouyinParser._first_url(item):
                    return found
            return None
        if isinstance(value, dict):
            for key in ("url_list", "urlList", "download_url_list", "url"):
                if found := DouyinParser._first_url(value.get(key)):
                    return found
        return None

    @staticmethod
    def _no_watermark(url: str) -> str:
        """抖音直链去水印：playwm → play。"""
        return url.replace("/playwm/", "/play/").replace("playwm?", "play?")

    @classmethod
    def _video_urls(cls, item: dict) -> list[str]:
        """返回按优先级排序的备用视频直链（已去水印）。

        **必须排除「音频包装地址」**：图文作品会把音频直链接在 play_addr 的
        video_id 参数里（样本 note/7697524967416115136）。不排除的话，纯音频作品
        会被当成一条 0 字节的假视频发出去 —— 实测那条 playwm 地址是 404，
        改写成 play 之后是 200 + text/plain + 0 字节。
        """
        urls: list[str] = []
        video = item.get("video") or {}
        for key in ("play_addr", "playAddr", "PlayAddrStruct", "download_addr", "play_addr_h264"):
            if found := cls._first_url(video.get(key)):
                if unwrap_audio_url(found):
                    continue  # 这其实是音频，交给 _extract_audio
                urls.append(cls._no_watermark(found))
        for bitrate in video.get("bit_rate") or []:
            if isinstance(bitrate, dict) and (found := cls._first_url(bitrate.get("play_addr"))):
                if unwrap_audio_url(found):
                    continue
                urls.append(cls._no_watermark(found))
        # 去重保序
        return list(dict.fromkeys(urls))

    @classmethod
    def _image_urls(cls, item: dict) -> list[str]:
        """返回作品里的**静态图**直链。

        不要把 ``images[].video.play_addr`` 混进来：实拍图集里每张图都带一段内嵌
        视频，混进图片列表后会被当图片下载与渲染，实际落地的是 mp4 ——
        表现就是「图集里冒出视频」，卡片上则是打不开的空图。
        内嵌视频段另有用途，见 ``_slide_video_urls``。
        """
        urls: list[str] = []
        for image in item.get("images") or []:
            if found := cls._first_url(image.get("url_list") or image):
                urls.append(found)
        return list(dict.fromkeys(urls))

    @classmethod
    def _slide_video_urls(cls, item: dict) -> list[str]:
        """图文帖里内嵌的实拍视频段（已去水印）。

        只在**没有静态图**时才该拿来用 —— 见 ``parse_slides`` 的说明。
        """
        urls: list[str] = []
        for image in item.get("images") or []:
            if not isinstance(image, dict):
                continue
            if found := cls._first_url((image.get("video") or {}).get("play_addr")):
                urls.append(cls._no_watermark(found))
        return list(dict.fromkeys(urls))

    @staticmethod
    def _cover_url(item: dict) -> str | None:
        video = item.get("video") or {}
        for key in ("cover", "origin_cover", "dynamic_cover"):
            if found := DouyinParser._first_url(video.get(key)):
                return found
        return None

    @staticmethod
    def _duration(item: dict) -> float:
        video = item.get("video") or {}
        for key in ("duration", "video_duration"):
            raw = video.get(key) or item.get(key)
            if raw:
                value = float(raw)
                # 抖音 Web 接口返回的 duration 单位是毫秒
                return value / 1000 if value > 1000 else value
        return 0.0

    def _build_result_from_item(self, item: dict, canonical_url: str | None = None):
        author_info = item.get("author") or {}
        avatar = self._first_url(author_info.get("avatar_larger") or author_info.get("avatar_thumb"))
        author = self.create_author(author_info.get("nickname") or "抖音用户", avatar)

        # 规范链接**交给 handler 决定**（`_parse_douyin` 用 URL 里的 ty+vid 拼
        # /note/ 或 /slides/）。这里原先硬编码 /video/，图文作品会被写成
        # https://www.douyin.com/video/<note_id> —— 那条链接打不开对应作品。
        # 只有调用方没给（直接调本方法）时才退回 /video/。
        url = canonical_url
        if not url and item.get("aweme_id"):
            url = f"https://www.douyin.com/video/{item.get('aweme_id')}"

        result = self.result(
            title=item.get("desc") or item.get("item_title") or "",
            author=author,
            timestamp=int(item.get("create_time") or 0) or None,
            url=url,
        )

        image_urls = self._image_urls(item)
        video_urls = self._video_urls(item)
        audio = self._extract_audio(item)

        # **图片优先，与 parse_video 的判定保持一致。**
        # 图文帖（note / slides）的 item 里也有顶层 video —— 抖音会把音频塞进
        # 它的 play_addr。先判视频会把整条图文作品当成视频，图片全被丢掉。
        if image_urls:
            result.contents.extend(self.create_images(image_urls))
            self._attach_audio(result, audio, has_images=True)
        elif video_urls:
            duration = self._duration(item)
            self._add_limit_warning(result, duration)
            result.video = self.create_video(video_urls[0], self._cover_url(item), duration)
        elif slide_videos := self._slide_video_urls(item):
            # 没有任何静态图、只有内嵌视频段：这种作品本质就是视频
            for slide_url in slide_videos:
                result.contents.append(self.create_video(slide_url))
        elif audio is not None:
            # 原生纯音频：不依赖配乐开关。
            self._attach_audio(result, audio, has_images=False)
        else:
            raise ParseException("未从作品中提取到媒体内容")

        return result
