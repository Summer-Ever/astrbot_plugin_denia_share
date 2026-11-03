"""
希望解析器 - 链接分享自动解析插件

支持 B站 | 抖音 | 快手 | 微博 | 小红书 | Twitter | AcFun | NGA | GitHub | Pixiv | Steam
另有网页截图（thum.io / Cloudflare 双后端）与 Pixiv 关键词搜索。
Steam 历史最低价需配置 ITAD_API_KEY（免费申请），未配置时只显示当前价与折扣。

代码来源（涉及第三方许可的都标在各自源文件里）：
- astrbot_plugin_rika_share（MIT）：解析框架 / 卡片渲染 / B站 / 微博 / 小红书 / AcFun / NGA
- Johnserf-Seed/f2（Apache-2.0）：抖音 a_bogus 签名，见 core/parsers/douyin/sign.py

有意不实现的能力：LLM 文本翻译、视频仅发送封面。
"""

import os
import re
import io
import json
import time
import asyncio
import hashlib
from contextvars import ContextVar
from pathlib import Path
from typing import Any, AsyncGenerator, Dict

_DELIVERY_UNCERTAIN = ContextVar("denia_delivery_uncertain", default=False)

import aiohttp
import httpx
import qrcode
from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult, MessageChain
import astrbot.api.message_components as Comp
from astrbot.api.star import Context, Star, register, StarTools

from .core.media_utils import (
    CACHE_MARKER_NAME,
    cleanup_cache_dir,
    ensure_cache_marker,
    is_docker_environment,
)
from .core.config import init_config, get_config, verify_schema_alignment
from .core.download import StreamDownloader
from .core.link_dedup import HINT, PROCEED, SILENT, LinkDedup
from .core.data import (
    ParseResult, ImageContent, VideoContent, AudioContent, Author, platform_of,
    build_concise_text,
)
from .core.history import HistoryStore, ParseRecord
from .core.permissions import is_allowed
from .core.relay import register_file
from .core.constants import (
    PLATFORM_DISPLAY_NAMES, PlatformEnum, is_custom_platform, platform_meta,
)
# 版本号只认 __init__.py 那一处：register 装饰器直接读它，
# 不用再在装饰器里手写一遍版本字符串（以前改版本要同时改三处，漏一处就版本不一致）
from . import __version__
from .core.exception import (
    ParseException, IgnoreException, SilentException,
)
from .core.card_renderer import ShareCardRenderer
from .core.screenshot import (
    ScreenshotService,
    describe_capture,
    is_probably_screenshotable,
)
from .core.parsers import (
    NeteaseMusicParser, QQMusicParser,
    BilibiliParser, DouyinParser, KuaiShouParser, WeiBoParser,
    XiaoHongShuParser, TwitterParser, NGAParser, AcfunParser,
    GitHubParser, PixivParser, SteamParser, XiaoheiheParser,
)
from .core.custom_parsers import (
    SUBDIR_NAME as CUSTOM_PARSERS_SUBDIR,
    CustomParserLoader,
    LoadResult as CustomParserLoadResult,
)

PLUGIN_NAME = "astrbot_plugin_denia_share"

# ========== B站扫码登录 API ==========
BILI_QR_GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
BILI_QR_POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
QR_CODE_SCANNED = 86090
QR_CODE_EXPIRED = 86038
QR_CODE_SUCCESS = 0
QR_CODE_EXPIRE_TIME = 180
POLL_INTERVAL = 5

# 登录状态在任务结束后还要留一会儿：WebUI 是**轮询**读取的，任务一结束就删，
# 最后一轮会拿到「登录任务不存在」而不是「登录成功」。
LOGIN_STATE_TTL = 300
# 状态字典的条数上限。WebUI 每点一次「获取二维码」都是一个随机 task_id，
# 原先只从 _bili_login_tasks 里 pop，状态永远留着 —— 点得越多字典越长。
LOGIN_STATE_MAX = 32

# 解析结果内存缓存的条数上限。`_result_cache` 原先只在「清理循环 / 切换缓存目录 /
# 手动清空缓存」三处 clear，而清理循环在 CACHE_TTL_HOURS=0 时根本不启动 ——
# 也就是说那种部署下它永不回收。加个条数上限兜底，与 TTL 无关。
MAX_RESULT_CACHE_ENTRIES = 128

# 渲染索引（cache_key -> 卡片路径）的条数上限。
#
# `_result_cache` 有 TTL、有条数上限，而 `_render_cache` 原先**两者都没有**：
# 磁盘卡片按 CACHE_TTL_HOURS 清理时，渲染索引不会被回收（CACHE_TTL_HOURS=0
# 时清理循环根本不启动），于是处理过的每个不同链接都会在索引里留一条，
# 无界增长。它只是「哪张卡片图在磁盘哪个位置」的记忆，条数少了的代价只是
# 重新渲染一次，不会丢内容 —— 与磁盘保留策略分开管。
MAX_RENDER_CACHE_ENTRIES = 128


def _get_plugin_data_dir() -> Path:
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path

    return Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME


#: 音频文件名里需要剔除的字符（路径分隔符 + 各家文件系统的保留字符）。
#: Windows 那几个（<>:"/\|?*）在聊天软件里同样不该出现 —— 收件人一存盘就报错。
_UNSAFE_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

#: 兜底文件名：标题被清洗成空串（全是表情、全是标点）时用它。
_FALLBACK_AUDIO_NAME = "音频"


def _audio_display_name(result: ParseResult, path: Path) -> str:
    """给「音频文件」模式起一个人类可读的名字，**后缀按真实文件走**。

    下载侧的缓存名是 URL 哈希（``<md5>.m4s``），直接当附件名发出去，用户在
    QQ 里看到的就是一串十六进制。这里换成「标题 + 真实后缀」，但只换**主名**：
    后缀必须来自磁盘上的真实文件，不能拿标题去拼 —— 那是编造格式。
    """
    suffix = path.suffix  # 含点；认不出就是空串，那就不加后缀，绝不瞎补 .mp3
    title = (getattr(result, "title", "") or "").strip()
    cleaned = _UNSAFE_FILENAME_CHARS.sub("_", title).strip(" .")
    if not cleaned:
        cleaned = _FALLBACK_AUDIO_NAME
    # 留出后缀的位置，避免标题太长时把扩展名挤掉。
    max_stem = max(1, 120 - len(suffix))
    return f"{cleaned[:max_stem]}{suffix}"


# ========== URL 匹配模式 ==========
BILIBILI_PATTERN = re.compile(r"(bilibili\.com|b23\.tv|bili2233\.cn|BV[1-9a-zA-Z]{10}|av\d{6,})")
DOUYIN_PATTERN = re.compile(r"(v\.douyin\.com|douyin\.com|iesdouyin\.com|m\.douyin\.com|jx\.douyin\.com|jingxuan\.douyin\.com)")
KUAISHOU_PATTERN = re.compile(r"(v\.kuaishou\.com|kuaishou\.com|chenzhongtech\.com)")
WEIBO_PATTERN = re.compile(r"(weibo\.com|weibo\.cn|m\.weibo\.cn|video\.weibo\.com|mapp\.api\.weibo\.cn)")
XHS_PATTERN = re.compile(r"(xhslink\.com|xhslink\.cn|xiaohongshu\.com)")
TWITTER_PATTERN = re.compile(r"(?:^|[\s./@])x\.com(?:[/:\s]|$)|twitter\.com")
NGA_PATTERN = re.compile(r"(nga\.178\.com|ngabbs\.com|bbs\.nga\.cn)")
ACFUN_PATTERN = re.compile(r"acfun\.cn")
GITHUB_PATTERN = re.compile(r"github\.com/[\w.\-]+/[\w.\-]+")
PIXIV_PATTERN = re.compile(r"pixiv\.net/(?:artworks|i/|member_illust)")
STEAM_PATTERN = re.compile(r"store\.steampowered\.com")
XIAOHEIHE_PATTERN = re.compile(r"https?://(?:(?:www|api)\.)?xiaoheihe\.cn/", re.I)

URL_PATTERN = re.compile(r"https?://[^\s'\"<>]+")


class _EventUrlWrapper:
    """把任意 URL 伪装成一个只含该 URL 的事件，供解析流程复用。"""

    def __init__(self, event: AstrMessageEvent, url: str):
        self._event = event
        self.message_str = url

    def __getattr__(self, name):
        return getattr(self._event, name)


@register(
    "希望解析器",
    "xiaoxi2760",
    # 平台列表也从 PLATFORMS 派生：这是第 5 处会写出平台名的地方，
    # 手写一份就等于又埋一个「改了这里忘了那里」
    "链接分享自动解析，支持 " + "|".join(PLATFORM_DISPLAY_NAMES.values()),
    __version__,
)
class DeniaSharePlugin(Star):

    @staticmethod
    def _is_onebot(event: AstrMessageEvent) -> bool:
        """合并转发（Comp.Nodes）是 OneBot v11 独有特性。"""
        try:
            return "aiocqhttp" in event.get_platform_name().lower()
        except Exception:
            return False

    @staticmethod
    def _fallback_cache_dir(data_dir: Path) -> tuple[Path, str]:
        """回退到插件数据目录，并保证它带缓存哨兵。"""
        default_dir = data_dir / "cache"
        default_dir.mkdir(parents=True, exist_ok=True)
        ensure_cache_marker(default_dir, allow_nonempty=True)
        return default_dir, "fallback"

    @staticmethod
    def _resolve_cache_dir(data_dir: Path, configured: str) -> tuple[Path, str]:
        """决定媒体缓存目录。

        留空用插件数据目录；填了「共享缓存目录」就用它，让协议端容器也能按
        同一个容器内路径读到下载下来的视频。

        共享目录不可用时回退到插件数据目录，避免下载因路径不可写而中断。

        **安全约束**：缓存清理是递归删除（``clear_cache_dir`` / ``cleanup_cache_dir``），
        所以这里必须先把危险路径挡掉，而不是等清理时再补救。三类被拒：
        文件系统根目录、把插件数据目录包在里面的祖先目录、
        已有内容却没有缓存哨兵的目录。三者在**建目录之前**判定。

        Returns:
            (缓存目录, 来源说明)，来源取 ``default`` / ``configured`` / ``fallback``。
        """
        if not configured:
            default_dir = data_dir / "cache"
            default_dir.mkdir(parents=True, exist_ok=True)
            ensure_cache_marker(default_dir, allow_nonempty=True)
            return default_dir, "default"

        candidate = Path(configured).expanduser()
        # resolve() 不要求路径存在（strict=False），所以危险路径可以在建目录**之前**
        # 就判掉。之前是先 mkdir(parents=True) 再校验，虽然删除链已经被哨兵挡住，
        # 但「按一个自由文本配置项在任意路径建出目录树」这个原语还在 —— 顺序调过来
        # 就顺手消掉了，代价为零。
        resolved = candidate.resolve()

        if resolved == Path(resolved.anchor):
            logger.warning(
                f"[denia_share] 拒绝把文件系统根目录 {resolved} 当作缓存目录"
                f"（清理会递归删掉整块盘），已回退到插件数据目录"
            )
            return DeniaSharePlugin._fallback_cache_dir(data_dir)

        try:
            data_dir.resolve().relative_to(resolved)
        except ValueError:
            pass
        else:
            logger.warning(
                f"[denia_share] 缓存目录 {resolved} 把插件数据目录包在里面，"
                f"清理时会连配置与解析记录一起删掉，已回退到插件数据目录"
            )
            return DeniaSharePlugin._fallback_cache_dir(data_dir)

        try:
            resolved.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning(
                f"[denia_share] 共享缓存目录不可用（{configured}）：{exc}；"
                f"已回退到插件数据目录"
            )
            return DeniaSharePlugin._fallback_cache_dir(data_dir)

        if not os.access(resolved, os.W_OK):
            logger.warning(
                f"[denia_share] 共享缓存目录不可写（{resolved}），已回退到插件数据目录"
            )
            return DeniaSharePlugin._fallback_cache_dir(data_dir)

        if not ensure_cache_marker(resolved, allow_nonempty=False):
            logger.warning(
                f"[denia_share] 共享缓存目录 {resolved} 里已有内容且没有 "
                f"{CACHE_MARKER_NAME} 哨兵，无法确认它就该被当作缓存目录，"
                f"已回退到插件数据目录。确认要用它，请先在该目录下建一个空的 "
                f"{CACHE_MARKER_NAME} 文件"
            )
            return DeniaSharePlugin._fallback_cache_dir(data_dir)

        return resolved, "configured"

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        data_dir = _get_plugin_data_dir()
        data_dir.mkdir(parents=True, exist_ok=True)
        self._data_dir = data_dir
        self.config_dir = data_dir / "config"
        self.config_dir.mkdir(parents=True, exist_ok=True)

        pconfig = init_config(config, data_dir / "cache", self.config_dir)

        # 缓存目录可被「共享缓存目录」配置项接管：Docker 分容器部署时，
        # 协议端要按同一个容器内路径才能读到下载下来的视频
        self.cache_dir, self.cache_dir_source = self._resolve_cache_dir(
            data_dir, pconfig.CACHE_DIR
        )
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        pconfig.cache_dir = self.cache_dir

        try:
            from .core.config import (
                migrate_grouped_config,
                migrate_music_commands,
                migrate_output_modes,
                migrate_proxy_switches,
            )

            # 顺序要紧：分代理开关迁移**必须先跑**。
            # migrate_grouped_config 的规则是「分组值 == 默认值且扁平值 ≠ 默认值
            # → 用扁平值」，而开关键默认值就是 False —— 老配置里残留的扁平
            # PROXY_*: true 会被它顶进分组，盖掉用户在新版里显式关掉的同一个开关，
            # 症状是「我明明关了，重启后又走代理」。migrate_proxy_switches 会先把这
            # 些扁平位并入分组再删掉，于是通用迁移再也看不到它们。
            changed = migrate_proxy_switches(config)
            # 输出模式迁移要在通用分组迁移**之前**：它靠「组里没有这三个键」
            # 判别新装还是升级，而 migrate_grouped_config 会按默认值把它们补进
            # 分组（OUTPUT_* 的默认值与迁移写入值可能不同，补完就分不出来了）。
            changed = migrate_output_modes(config) or changed
            changed = migrate_music_commands(config) or changed
            changed = migrate_grouped_config(config) or changed
            if changed:
                save = getattr(config, "save_config", None)
                if callable(save):
                    save()
        except Exception:
            logger.warning("旧版配置迁移失败，将使用兼容回退读取", exc_info=True)

        # 直连下载器：常驻。代理下载器按需懒建（见 _downloader_for）——
        # 代理绑定在 httpx 连接池上没法按请求切换，分解析器代理靠两个实例实现
        self.downloader = StreamDownloader(
            self.cache_dir,
            proxies=None,
            max_size_mb=pconfig.VIDEO_SIZE_MAXIMUM_MB,
            verify_ssl=pconfig.HTTP_VERIFY_SSL,
        )
        self._proxy_downloader: StreamDownloader | None = None
        self.disabled_platforms = pconfig.DISABLED_PLATFORMS
        self._send_errors = pconfig.SEND_ERROR_MESSAGES

        self.parsers: dict[str, Any] = {}
        # 用户自定义解析器的加载器。延迟到 _init_parsers 里建（那时 downloader 已就绪）
        self._custom_loader: CustomParserLoader | None = None
        self._custom_result: CustomParserLoadResult | None = None
        # 上一轮装进 self.parsers 的自定义平台键。重扫时靠它把**已经不存在**的
        # 平台（文件被删/改名，或新版本加载失败）摘掉 —— 见 _init_custom_parsers。
        self._custom_parser_keys: set[str] = set()
        self._init_parsers()
        # 存 (写入时刻, 结果) —— 时刻是给「重复解析间隔」用的，见 _get_cached_result
        self._result_cache: dict[str, tuple[float, ParseResult]] = {}
        self._render_cache: dict[str, Path] = {}
        #: 本次媒体投递是否落在「送达未知」（合并转发回执超时/断连）。
        #: 见 _process_url：未知时保留去重窗口但不记成功历史。
        # 同群重复链接去重：只解析第一个，后来者不解析也不收卡片（见 core/link_dedup）
        self._link_dedup = LinkDedup(lambda: self._dedup_window())
        self._cache_cleanup_task: asyncio.Task | None = None

        self._renderer = ShareCardRenderer(self.cache_dir, **pconfig.renderer_options())

        # ========== B站 Cookie ==========
        self._bili_cookie: str = ""
        # 上一次**生效过**的配置项 BILI_CK。用来判断「配置项到底变没变」：
        # apply_runtime_config 是「保存配置 / 恢复默认 / 保存外观」三个接口都会走的，
        # 无条件重新应用 cookie 会误伤（详见那里的注释）。
        self._bili_ck_applied: str = ""
        self._bili_http_session: aiohttp.ClientSession | None = None
        self._bili_login_tasks: Dict[str, asyncio.Task] = {}
        self._bili_login_states: Dict[str, dict[str, Any]] = {}
        # 二维码临时文件的延迟清理任务，terminate() 时一并取消
        self._delayed_cleanup_tasks: list[asyncio.Task] = []
        self._bili_data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self._bili_data_dir.mkdir(parents=True, exist_ok=True)
        self._bili_cookie_file = self._bili_data_dir / "bili_cookie.json"

        # ========== 解析记录（WebUI「缓存」页的数据源） ==========
        self.history = HistoryStore(data_dir / "history.jsonl")

        self._check_schema_alignment()
        self._register_webui()

        logger.info(
            f"[denia_share] 已启用平台: {', '.join(self.parsers.keys()) or '无'}；"
            f"卡片渲染: {'开' if self._renderer.enabled else '关'}；"
            f"媒体缓存: {self.cache_dir}"
            f"{'（共享目录）' if self.cache_dir_source == 'configured' else ''}"
            f"{'（配置的共享目录不可用，已回退）' if self.cache_dir_source == 'fallback' else ''}；"
            f"媒体中转: {'开' if pconfig.MEDIA_RELAY_ENABLED else '关'}"
            f"{'（容器内）' if is_docker_environment() else ''}"
        )

    def _check_schema_alignment(self):
        """自检 _conf_schema.json 与 CONFIG_META 是否一致（不一致会导致配置静默丢失）。"""
        try:
            schema_path = Path(__file__).with_name("_conf_schema.json")
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("[denia_share] 无法读取 _conf_schema.json，跳过一致性自检", exc_info=True)
            return
        problems = verify_schema_alignment(schema)
        for problem in problems:
            logger.warning(f"[denia_share] 配置 schema 不一致：{problem}")

    def _register_webui(self):
        """注册插件 WebUI 的后端接口。

        AstrBot < 4.24.2 没有 register_web_api，此时静默跳过——
        聊天命令与解析功能都不受影响，只是没有网页界面。

        4.24.2 ~ 4.26.x 有 register_web_api 但没有 astrbot.api.web，
        由 core/web_compat 自动回退到 quart，两条路径行为一致。
        """
        if not hasattr(self.context, "register_web_api"):
            logger.info("[denia_share] 当前 AstrBot 不支持插件页面，跳过 WebUI 注册")
            self._webui_ready = False
            return
        try:
            from .core.web_compat import HAS_WEB_API
            from .core.webui import WebUIApi

            self._webui = WebUIApi(self)
            self._webui.register()
            self._webui_ready = True
            logger.info(
                "[denia_share] WebUI 接口已注册（插件页面 pages/denia，响应后端=%s）",
                "astrbot.api.web" if HAS_WEB_API else "quart 兼容层",
            )
        except Exception:
            self._webui_ready = False
            logger.warning("[denia_share] WebUI 接口注册失败", exc_info=True)

    def _downloader_for(self, platform_key: str) -> StreamDownloader:
        """按平台的「走代理」开关挑下载器：开了走代理池，没开走直连池。

        StreamDownloader 的代理绑定在连接池上（httpx client 构造参数），
        没法按请求切换，所以「分解析器代理」靠两个下载器实例实现：
        共享同一个缓存目录；直连池常驻，代理池只在有平台开关打开时才建。
        与解析器接口请求同源：都问 ``pconfig.proxy_for(platform_key)``。
        """
        pconfig = get_config()
        if pconfig.proxy_for(platform_key) is None:
            return self.downloader

        if self._proxy_downloader is None:
            self._proxy_downloader = StreamDownloader(
                self.cache_dir,
                proxies=pconfig.PROXY or None,
                max_size_mb=pconfig.VIDEO_SIZE_MAXIMUM_MB,
                verify_ssl=pconfig.HTTP_VERIFY_SSL,
            )
            logger.info(
                f"[denia_share] 已创建代理下载器（{pconfig.PROXY}），"
                "供打开了「走代理」开关的解析器下载媒体使用"
            )
        return self._proxy_downloader

    def _init_parsers(self):
        pconfig = get_config()
        disabled = self.disabled_platforms
        if "bilibili" not in disabled:
            # bilibili_api 缺失时会换上自建替补解析器（B站视频仍可用）；
            # 连替补都起不来才是 None，此时跳过注册而不是拖垮整个插件。
            if BilibiliParser is None:
                from .core.parsers import BILIBILI_FALLBACK_ERROR, BILIBILI_IMPORT_ERROR

                logger.error(
                    f"[denia_share] B站解析器未启用：bilibili-api-python 导入失败"
                    f"（{BILIBILI_IMPORT_ERROR}），自建替补也不可用"
                    f"（{BILIBILI_FALLBACK_ERROR}）。其余平台不受影响；"
                    f"需要 B站请执行 pip install 'bilibili-api-python>=16.0.0'"
                )
            else:
                from .core.parsers import BILIBILI_IS_FALLBACK

                self.parsers["bilibili"] = BilibiliParser(
                    self._downloader_for("bilibili"), bili_ck=pconfig.BILI_CK, config_dir=self.config_dir
                )
                if BILIBILI_IS_FALLBACK:
                    logger.warning(
                        "[denia_share] bilibili-api-python 不可用，B站已切到自建备用解析："
                        "视频可解析下载，动态 / 直播 / 收藏夹 / 专栏暂不可用"
                    )
        if "douyin" not in disabled:
            self.parsers["douyin"] = DouyinParser(self._downloader_for("douyin"))
        if "kuaishou" not in disabled:
            self.parsers["kuaishou"] = KuaiShouParser(self._downloader_for("kuaishou"))
        if "weibo" not in disabled:
            self.parsers["weibo"] = WeiBoParser(self._downloader_for("weibo"))
        if "xiaohongshu" not in disabled:
            self.parsers["xiaohongshu"] = XiaoHongShuParser(
                self._downloader_for("xiaohongshu"), xhs_ck=pconfig.XHS_CK
            )
        if "twitter" not in disabled:
            self.parsers["twitter"] = TwitterParser(self._downloader_for("twitter"))
        if "nga" not in disabled:
            self.parsers["nga"] = NGAParser(self._downloader_for("nga"))
        if "acfun" not in disabled:
            self.parsers["acfun"] = AcfunParser(self._downloader_for("acfun"))
        if "github" not in disabled:
            self.parsers["github"] = GitHubParser(
                self._downloader_for("github"), token=pconfig.GITHUB_TOKEN
            )
        if "pixiv" not in disabled:
            self.parsers["pixiv"] = PixivParser(
                self._downloader_for("pixiv"), cookie=pconfig.PIXIV_CK
            )
        if "steam" not in disabled:
            self.parsers["steam"] = SteamParser(
                self._downloader_for("steam"),
                itad_key=pconfig.ITAD_API_KEY,
                region=pconfig.STEAM_REGION,
            )
        if "netease" not in disabled:
            self.parsers["netease"] = NeteaseMusicParser(self._downloader_for("netease"))
        if "xiaoheihe" not in disabled:
            self.parsers["xiaoheihe"] = XiaoheiheParser(
                self._downloader_for("xiaoheihe"), cookie=pconfig.XIAOHEIHE_CK
            )
        if "qqmusic" not in disabled:
            self.parsers["qqmusic"] = QQMusicParser(self._downloader_for("qqmusic"))
        for key in ("netease", "qqmusic"):
            if key in self.parsers:
                self.parsers[key].before_play = self._refresh_music_account

        self._init_custom_parsers()
        self._build_screenshot_service()

    def _init_custom_parsers(self) -> None:
        """加载用户自定义解析器（目录扫描 + 失败隔离）。

        每次都会重新扫描目录：这个方法在插件初始化与 ``apply_runtime_config``
        里都会被调到，所以「保存配置 / 切换平台 / 点重新加载」都能让新写的文件
        生效，不必重载插件。加载器自身保证反复调用是幂等的（先反注册再注册）。
        """
        if self._custom_loader is None:
            self._custom_loader = CustomParserLoader(
                self._data_dir / CUSTOM_PARSERS_SUBDIR
            )
        # 自定义解析器一律用直连下载器：它们没有对应的「走代理」配置项
        # （AstrBot 会剔除 schema 之外的键），代理池只服务于内置平台。
        result = self._custom_loader.reload(
            self.downloader, skip_keys=set(self.disabled_platforms)
        )
        self._custom_result = result
        # 先把上一轮装进去的自定义平台摘掉，再装这一轮的。
        #
        # **不能只 update**：dict.update 只覆盖同名键，删掉（或改了平台键）的文件
        # 会把自己的旧实例留在 self.parsers 里 —— 平台列表里已经没有它了
        # （加载器的 _purge 反注册了展示名），但实例还在：
        #   · WebUI 手动解析照旧用**旧代码**解析（_match_parser 找得到它）
        #   · toggle_platform 会对它回「未知平台」（展示名已不在 PLATFORM_DISPLAY_NAMES）
        # 也就是「看起来删了、其实还在跑」，本仓最贵的那类 bug。
        # 用自己记的键来摘，而不是 is_custom_platform(key)：后者查的是 PLATFORMS，
        # 而 _purge 已经把旧键从那里删掉了，问它只会得到「这不是自定义平台」。
        for stale_key in self._custom_parser_keys:
            self.parsers.pop(stale_key, None)
        self._custom_parser_keys = set(result.parsers)
        self.parsers.update(result.parsers)

    def reload_custom_parsers(self) -> CustomParserLoadResult:
        """供 WebUI 的「重新加载自定义解析器」按钮调用：重扫 + 立刻生效。"""
        self._init_custom_parsers()
        return self._custom_result or CustomParserLoadResult()

    def _build_screenshot_service(self) -> None:
        """按当前配置（重）建截图服务。

        抽成独立方法是因为热更新也要调它：截图后端的代理与证书校验都是构造时
        读死的，只在初始化时建一次的话，网页上改这两项要重载插件才生效。
        """
        pconfig = get_config()
        self.screenshot = ScreenshotService(
            self.cache_dir,
            backend=pconfig.SCREENSHOT_BACKEND,
            cf_account_id=pconfig.CF_ACCOUNT_ID,
            cf_api_token=pconfig.CF_API_TOKEN,
            proxy=pconfig.SCREENSHOT_PROXY,
            verify_ssl=pconfig.HTTP_VERIFY_SSL,
        )
        self._screenshot_fallback = pconfig.SCREENSHOT_FALLBACK

    async def _cache_cleanup_loop(self):
        """缓存清理循环。

        **每一轮重新读配置**，而不是把 TTL / 间隔烤进闭包 —— 配置页的「维护」
        分组写着「改动后立即生效」，而 apply_runtime_config 并不重建这个任务，
        烤进闭包就等于「网页上改了 CACHE_TTL_HOURS 也不会生效」。
        """
        while True:
            pconfig = get_config()
            ttl = pconfig.CACHE_TTL_HOURS
            interval = max(pconfig.CACHE_CLEANUP_INTERVAL_MINUTES, 1) * 60
            try:
                if ttl > 0:
                    await cleanup_cache_dir(self.cache_dir, ttl_hours=ttl)
                    self._result_cache.clear()
                    self._render_cache.clear()
                    # 去重窗口也一起清：窗口里的「只解析第一个」是基于上一次解析
                    # 建立的，缓存都清空了还锁着，后来者会被无故静默掉
                    self._link_dedup.clear()
            except asyncio.CancelledError:
                raise
            except Exception:
                # 一次清理失败（如磁盘满/文件占用）不应杀死整个循环
                logger.warning("[denia_share] 缓存清理失败，下轮重试", exc_info=True)
            await asyncio.sleep(interval)

    async def initialize(self):
        # 循环常驻：TTL=0 时它自己跳过清理，这样「把 TTL 从 0 改成非 0」也能生效
        self._cache_cleanup_task = asyncio.create_task(self._cache_cleanup_loop())

        await self._bili_load_cookie()
        self._bili_http_session = aiohttp.ClientSession()
        if self._bili_cookie:
            self._bili_apply_cookie_to_parser(self._bili_cookie)
        from .core.credentials.sync import service_for
        service_for(self).start()

    # ==================== 平台处理器 ====================

    def _access_denied(self, event: AstrMessageEvent) -> bool:
        """白名单 / 黑名单判定：True 表示这次请求该被**静默**拦下。

        管理员由 AstrBot 判定（``event.is_admin()``），与 ``/denia_status`` 等命令的
        ADMIN 门一致，不单独配管理员 ID。判定顺序见 ``core/permissions.py``。

        拦下时只写一条 debug 日志：黑名单群里不该因为有人发链接就刷出提示。

        判定本身抛异常时**按放行处理**并留一条 warning —— 名单配错最多是「没拦住」，
        而在这里静默拦下全部请求会让插件看起来彻底坏了（用户只会看到「零反应」）。
        """
        try:
            allowed = is_allowed(
                get_config(),
                is_admin=event.is_admin(),
                is_private=event.is_private_chat(),
                sender_id=event.get_sender_id(),
                group_id=event.get_group_id(),
            )
        except Exception:
            logger.warning("[denia_share] 权限判定失败，本次按放行处理", exc_info=True)
            return False
        if not allowed:
            logger.debug(
                "[denia_share] 被权限名单拦下：user=%s group=%s",
                event.get_sender_id(),
                event.get_group_id(),
            )
        return not allowed

    async def _dispatch(self, event: AstrMessageEvent, name: str):
        if self._has_json_component(event):
            return
        parser = self.parsers.get(name)
        if not parser:
            return
        async for r in self._process_url(event, parser):
            yield r

    # 内置平台的链接处理器。每条 handler 的 docstring 会**原样显示在插件详情页的
    # 「指令」列表里**（AstrBot 取 handler.__doc__ 作为描述），所以一句话说清解析什么。
    @filter.regex(BILIBILI_PATTERN)
    async def bilibili_handler(self, event: AstrMessageEvent, matched: re.Match | None = None):
        """B站链接解析（视频 / 动态 / 直播 / 专栏 / 收藏夹）"""
        async for r in self._dispatch(event, "bilibili"):
            yield r

    @filter.regex(DOUYIN_PATTERN)
    async def douyin_handler(self, event: AstrMessageEvent, matched: re.Match | None = None):
        """抖音链接解析（视频 / 图集）"""
        async for r in self._dispatch(event, "douyin"):
            yield r

    @filter.regex(KUAISHOU_PATTERN)
    async def kuaishou_handler(self, event: AstrMessageEvent, matched: re.Match | None = None):
        """快手链接解析（视频 / 图集）"""
        async for r in self._dispatch(event, "kuaishou"):
            yield r

    @filter.regex(WEIBO_PATTERN)
    async def weibo_handler(self, event: AstrMessageEvent, matched: re.Match | None = None):
        """微博链接解析（正文 / 视频 / 长文）"""
        async for r in self._dispatch(event, "weibo"):
            yield r

    @filter.regex(XHS_PATTERN)
    async def xiaohongshu_handler(self, event: AstrMessageEvent, matched: re.Match | None = None):
        """小红书链接解析（图文 / 视频，图片改写为无水印原图）"""
        async for r in self._dispatch(event, "xiaohongshu"):
            yield r

    @filter.regex(TWITTER_PATTERN)
    async def twitter_handler(self, event: AstrMessageEvent, matched: re.Match | None = None):
        """Twitter / X 链接解析（图片 / 视频）"""
        async for r in self._dispatch(event, "twitter"):
            yield r

    @filter.regex(NGA_PATTERN)
    async def nga_handler(self, event: AstrMessageEvent, matched: re.Match | None = None):
        """NGA 帖子解析"""
        async for r in self._dispatch(event, "nga"):
            yield r

    @filter.regex(ACFUN_PATTERN)
    async def acfun_handler(self, event: AstrMessageEvent, matched: re.Match | None = None):
        """AcFun 视频解析"""
        async for r in self._dispatch(event, "acfun"):
            yield r

    @filter.regex(GITHUB_PATTERN)
    async def github_handler(self, event: AstrMessageEvent, matched: re.Match | None = None):
        """GitHub 仓库卡片（星标 / 分支 / issue / 主语言 / 最近提交）"""
        async for r in self._dispatch(event, "github"):
            yield r

    @filter.regex(PIXIV_PATTERN)
    async def pixiv_handler(self, event: AstrMessageEvent, matched: re.Match | None = None):
        """Pixiv 作品解析（非全年龄内容一律过滤）"""
        async for r in self._dispatch(event, "pixiv"):
            yield r

    @filter.regex(STEAM_PATTERN)
    async def steam_handler(self, event: AstrMessageEvent, matched: re.Match | None = None):
        """Steam 商店页卡片（价格 / 折扣 / 类型，可含历史最低价）"""
        async for r in self._dispatch(event, "steam"):
            yield r

    @filter.regex(XIAOHEIHE_PATTERN)
    async def xiaoheihe_handler(self, event: AstrMessageEvent, matched: re.Match | None = None):
        """小黑盒帖子与游戏分享链接解析。"""
        async for result in self._dispatch(event, "xiaoheihe"):
            yield result

    @filter.regex(r"https?://(?:[A-Za-z0-9-]+\.)?(?:music\.163\.com|163cn\.tv)/")
    async def netease_handler(self, event: AstrMessageEvent, matched: re.Match | None = None):
        """网易云音乐单曲分享链接解析。"""
        async for result in self._dispatch(event, "netease"):
            yield result

    @filter.regex(r"https?://(?:[A-Za-z0-9-]+\.)?y\.qq\.com/")
    async def qqmusic_handler(self, event: AstrMessageEvent, matched: re.Match | None = None):
        """QQ音乐单曲分享链接解析。"""
        async for result in self._dispatch(event, "qqmusic"):
            yield result

    # 自定义解析器的聊天入口。内置平台各有自己的 ``@filter.regex(<域名>)`` 处理器，
    # 而自定义平台是**运行时**才知道的，没法为它生成静态过滤器 —— 所以用这个通用 URL
    # 处理器兜住它们。少了这一条，自定义解析器只能在网页上手动解析：群里发链接**零反应**
    # （加载成功、平台开关里能看到它、自检也过，但聊天链路根本没有入口）。
    #
    # 两条让路规则：
    # - 消息带 JSON 组件（QQ 小程序卡片）时直接返回：``json_card_handler`` 已经会遍历
    #   全部解析器（含自定义），不返回就会把同一链接解析两遍。
    # - URL 归一个**已启用**的内置平台时跳过：那是内置处理器的活，重复处理会发两条。
    #   内置平台被禁用时不算命中，此时自定义解析器可以接过去。
    #
    # 上面这段原本是 handler 的 docstring，会整段显示在插件详情页里，太长，改成注释。
    @filter.regex(URL_PATTERN)
    async def custom_parser_handler(self, event: AstrMessageEvent, matched: re.Match | None = None):
        """自定义解析器的聊天入口（通用 URL 匹配）"""
        if self._has_json_component(event):
            return
        for link in dict.fromkeys(URL_PATTERN.findall(event.message_str or "")):
            if self._match_builtin_parser(link) is not None:
                continue
            parser = self._match_custom_parser(link)
            if parser is None:
                continue
            async for r in self._process_url(_EventUrlWrapper(event, link), parser):
                yield r
            return

    # ==================== 网页截图 ====================

    @filter.command("shot")
    async def shot_command(self, event: AstrMessageEvent):
        """对链接截图 /shot <网址>"""
        url = self._first_url(event)
        if not url:
            yield event.plain_result("用法: /shot <网址>")
            return
        async for r in self._do_screenshot(event, url):
            yield r

    @filter.regex(URL_PATTERN)
    async def screenshot_fallback_handler(self, event: AstrMessageEvent, matched: re.Match | None = None):
        """无可解析平台时按需截图兜底（默认关闭）。"""
        if not self._screenshot_fallback or self._has_json_component(event):
            return
        url = self._first_url(event)
        if not url or self._match_parser(url) is not None:
            return
        async for r in self._do_screenshot(event, url):
            yield r

    @filter.command("pixiv")
    async def pixiv_search_command(self, event: AstrMessageEvent):
        """Pixiv 关键词搜索 /pixiv <关键词>"""
        if self._access_denied(event):
            return
        keyword = " ".join((event.message_str or "").split()[1:]).strip()
        if not keyword:
            yield event.plain_result("用法: /pixiv <关键词>\n结果强制过滤非全年龄内容")
            return

        parser = self.parsers.get("pixiv")
        if parser is None:
            yield event.plain_result("Pixiv 平台已被禁用")
            return

        # 三路全关：连搜索 API 都不该打，更不该占结果缓存 / 写历史。
        # 位置在 parser.search 之前 —— 否则「什么都不发」却仍消耗一次联网请求。
        if not get_config().any_output_enabled():
            logger.info("[denia_share] 三路输出全关，跳过 Pixiv 搜索")
            return

        try:
            cache_key = f"pixiv:{keyword}"
            result = self._get_cached_result(cache_key)
            if result is None:
                result = await parser.search(keyword)
                self._remember_result(cache_key, result)
            async for r in self._deliver(event, result, cache_key):
                yield r
            await self._record_history(
                result, self._render_cache.get(cache_key), via="command"
            )
        except IgnoreException as e:
            logger.warning(f"[denia_share] Pixiv 搜索被忽略: {e.message}")
            yield event.plain_result(f"ℹ️ {e.message}")
        except ParseException as e:
            logger.warning(f"[denia_share] Pixiv 搜索失败: {e.message}")
            yield event.plain_result(f"❌ 搜索失败: {e.message}")
        except Exception as e:
            logger.exception("Pixiv 搜索异常")
            # 同样不回显异常原文，避免把 URL / 容器内路径带进群里
            yield event.plain_result(
                f"❌ 搜索出错（{type(e).__name__}），详情见 AstrBot 日志"
            )

    # ==================== JSON 卡片（QQ 小程序分享） ====================

    def _get_music_controller(self):
        controller = getattr(self, "_music_controller", None)
        if controller is None:
            from .core.music.controller import MusicController
            controller = MusicController(self)
            self._music_controller = controller
            self._music_login_manager = controller.login
        return controller

    async def _refresh_music_account(self, platform):
        await self._get_music_controller()._refresh(platform)

    @filter.regex(r"(?s)^.*$")
    async def music_commands_handler(self, event: AstrMessageEvent, matched: re.Match | None = None):
        """点歌 歌名显示本插件搜歌卡片，听1 按编号发送音频。"""
        from .core.music.controller import music_command
        selected = music_command(event.message_str)
        if selected is None or self._has_json_component(event):
            return
        async for output in self._get_music_controller().handle(event, *selected):
            yield output

    @filter.regex(r".*")
    async def json_card_handler(self, event: AstrMessageEvent, matched: re.Match | None = None):
        """QQ 小程序分享卡片（Json 消息段）里的链接解析"""
        if not self._has_json_component(event):
            return
        links = self._prioritize_card_links(self._extract_links_from_event(event))
        if not links:
            return
        for link in links:
            for parser in self.parsers.values():
                try:
                    parser.search_url(link)
                    async for r in self._process_url(_EventUrlWrapper(event, link), parser):
                        yield r
                    return
                except (SilentException, ParseException):
                    continue

    def _prioritize_card_links(self, links: list[str]) -> list[str]:
        """卡片里的链接排序：**能在本地认定视频身份的排前面**。

        分享卡片经常同时带短链（``meta.detail_1.qqdocurl``，如 ``b23.tv/…``）和
        原长链（``detail_1.url``）。原来的顺序是 ``qqdocurl`` 在前，于是永远先跳
        一次短链：短链失效时整张卡片直接失败，明明旁边那条长链是好的、不需要跳转。

        平台解析器可以通过 ``dedup_identity``（可选 hook）在本地认领资源身份。
        这里就按「第一个认领它的解析器有没有给出身份」稳定排序 —— 有的优先，
        需要额外跳转的靠后；认不出 / 没有 hook 的解析器保持原顺序（``sorted`` 稳定）。
        """
        unique = list(dict.fromkeys(links))

        def priority(link: str) -> int:
            for parser in self.parsers.values():
                try:
                    _keyword, searched = parser.search_url(link)
                except Exception:
                    continue
                resolver = getattr(parser, "dedup_identity", None)
                if callable(resolver):
                    try:
                        if resolver(searched):
                            return 0
                    except Exception:
                        pass
                return 1
            return 2

        return sorted(unique, key=priority)

    # ==================== 核心流程 ====================

    @staticmethod
    def result_cache_key(url: str) -> str:
        """解析结果缓存键：全量 URL 的短哈希。

        直接截断 URL 前 64 字符会让不同链接撞成同一个 key（小红书 / 微博的
        分享链常带长参数），撞上就会把 A 链接的解析结果当成 B 链接的返回。
        WebUI 的手动解析也必须用同一个键，否则同一链接在两条链路上各缓存一份。
        """
        return hashlib.md5(url.encode()).hexdigest()[:16]

    async def _process_url(
        self, event: AstrMessageEvent, parser: Any
    ) -> AsyncGenerator[MessageEventResult, None]:
        _DELIVERY_UNCERTAIN.set(False)
        from .core.diagnostics import store_for
        diagnostic = store_for(self).start('chat', self._error_platform(parser))
        diagnostic.stage('match', 'running')
        phase = 'match'
        # 白名单 / 黑名单的唯一收口点：内置平台处理器、自定义解析器、
        # JSON 卡片三条路径最后都走这里（/shot 截图与 /pixiv 搜索各自另有入口）。
        if self._access_denied(event):
            diagnostic.stage('match', 'skipped', 'blocked')
            diagnostic.finish('skipped')
            return
        # 这两个必须**在 try 之前**预置：下面的失败分支都要用它们撤销
        # 去重占位，而它们是在 try 里逐个赋值的。若 try 的第一行
        # ``result_cache_key(url)`` 就抛了（url 含孤立代理字符时
        # ``url.encode()`` 会抛 UnicodeEncodeError），except 里引用未赋值的
        # ``scope`` 会再抛 UnboundLocalError —— 真实错误被掩盖，forget 不执行、
        # 「处理出错」提示也发不出去。
        cache_key: str | None = None
        scope: str | None = None
        # 去重键与结果缓存键**分开**：缓存按「匹配到的原文」算，去重按平台给出的
        # 规范身份算（见 _dedup_key）。撤销占位必须用登记时的那个键，所以也预置。
        dedup_key: str | None = None
        # 已经交付出去了就别再撤销占位：_record_history 之类**交付之后**的收尾
        # 步骤失败时撤销占位，会让下一个发同链接的人重新解析并**再发一张卡片** ——
        # 那正好是这个功能要消灭的事。
        delivered = False
        try:
            raw_message = event.message_str.strip()
            # **键必须来自「解析器实际匹配到的那一段」，不能自己抽 URL。**
            #
            # 踩过两次坑：
            #   1. 键用整条消息 → 11 个内置平台处理器传的是整条事件，
            #      「【】看看这个 <url>」和「<url>」成了两个键，不去重也不共用缓存。
            #   2. 改用 `URL_PATTERN.search()` 抽「第一个 URL」→ 那是**独立**的抽取，
            #      和解析器自己的 pattern 可以是**不同的链接**：
            #        · 消息里两个不同平台的链接 → 第二个平台的处理器拿到第一个链接的键，
            #          抖音解析器一次没被调用，却发出一张 B站卡片；
            #        · 前导一个非本平台链接 → 两个不同视频共用一个键，用户收到的是
            #          **第一个视频的卡片**；
            #        · `URL_PATTERN` 的 `[^\s'\"<>]+` 不排除中文与标点，
            #          「…/xxx，挺有意思」会把 8 个中文字一起吞进键。
            #
            # 所以顺序必须是：**先让解析器认领，再按它认领的那段算键**。
            keyword, searched = parser.search_url(raw_message)
            diagnostic.stage('match', 'ok')
            matched = searched.group(0) or raw_message
            cache_key = self.result_cache_key(matched)

            # ---- 三路输出全关：这里就短路 ----
            # 位置很关键：必须在 search_url **之后**（还得知道是哪条链接，便于日志）
            # 但要在**去重占位与解析之前**。否则用户把三项都关掉后，每条链接
            # 仍然会去解析、下载全部媒体，最后什么也不发 —— 既白花流量，
            # 又会把去重窗口占掉让群里其他人也收不到东西。
            if not get_config().any_output_enabled():
                diagnostic.stage('send', 'skipped', 'output_off')
                diagnostic.finish('skipped')
                logger.info(
                    f"[denia_share] 文本/卡片/富媒体三项全关，本次不解析也不响应: "
                    f"url={matched[:60]}"
                )
                return

            # ---- 同群重复链接去重 ----
            # 必须排在缓存查询**之前**：命中结果缓存时照旧要发卡片，而去重要的
            # 是「不再响应」。先查缓存的话第二个人的卡片就照发出来了，等于没去重。
            if self._dedup_applies(event):
                scope = self._event_scope(event)
                # 短链（bv 藏在跳转后）先展开成实际匹配；展开结果后面直接拿去
                # parse，所以这一跳就是 parse_short 本来要跳的那一次，不重复。
                keyword, searched = await self._prepare_dedup_target(parser, keyword, searched)
                dedup_key = self._dedup_key(parser, searched, cache_key)
                decision = self._link_dedup.check(scope, dedup_key)
                if decision == HINT:
                    diagnostic.stage('parse', 'skipped', 'duplicate')
                    diagnostic.finish('skipped')
                    uncertain = self._link_dedup.is_uncertain(scope, dedup_key)
                    yield event.plain_result(self._UNCERTAIN_HINT if uncertain else self._DEDUP_HINT)
                    return
                if decision == SILENT:
                    diagnostic.stage('parse', 'skipped', 'duplicate')
                    diagnostic.finish('skipped')
                    logger.info(
                        f"[denia_share] 同会话重复链接已静默: scope={scope} "
                        f"url={matched[:60]}"
                    )
                    return

            phase = 'parse'
            diagnostic.stage('parse', 'running')
            result = self._get_cached_result(cache_key)
            if result is None:
                result = await parser.parse(keyword, searched)
                self._remember_result(cache_key, result)
                diagnostic.stage('parse', 'ok')
            else:
                diagnostic.stage('parse', 'ok', 'cached')

            def mark_delivered():
                nonlocal delivered
                delivered = True

            phase = 'send'
            queued = False
            async for r in self._deliver(event, result, cache_key, on_delivered=mark_delivered, diagnostic=diagnostic):
                delivered = True
                queued = True
                yield r

            if _DELIVERY_UNCERTAIN.get():
                diagnostic.stage('send', 'unknown', 'receipt_unknown')
                diagnostic.finish('unknown')
                if scope is not None and dedup_key is not None:
                    self._link_dedup.mark_uncertain(scope, dedup_key)
                logger.info("[denia_share] 合并转发送达未知，保留本次去重窗口但不记成功历史")
                return
            if not delivered:
                diagnostic.stage('send', 'error', 'send_failed')
                diagnostic.finish('error')
                self._forget_dedup(scope, dedup_key, False)
                return

            # 交付完成后再记录：此时媒体都已落盘，记录里能带上真实文件名
            await self._record_history(result, self._render_cache.get(cache_key))
            has_error = any(s['state'] == 'error' for s in diagnostic.to_dict()['stages'])
            if not any(s['name'] == 'send' and s['state'] == 'error' for s in diagnostic.to_dict()['stages']):
                diagnostic.stage('send', 'unknown' if queued else 'ok', 'queued' if queued else 'accepted')
            diagnostic.finish('error' if has_error else 'unknown' if queued else 'ok')

        except SilentException as e:
            diagnostic.stage(phase, 'skipped', 'skipped')
            diagnostic.finish('skipped')
            # 「这条链接不归我管」是每条普通消息都会走到的正常流量，只在调试级留痕
            logger.debug(f"[denia_share] 静默跳过: {e.message}")
            self._forget_dedup(scope, dedup_key, delivered)
        except ParseException as e:
            diagnostic.stage(phase, 'error', 'parse_failed')
            diagnostic.finish('error')
            # 解析失败必须无条件留痕：Cookie 全失效、接口改版、被风控都走这条，
            # 而 SEND_ERROR_MESSAGES 默认是关的 —— 只由它决定「发不发群」，
            # 不能连日志一起决定，否则群里不回复、日志也一片空白，用户和运维同时失明。
            logger.warning(f"[denia_share] 解析未完成: {e.message}")
            # 同样要撤销：第一次解析挂了就该让下一个人重试，而不是废掉整个窗口
            self._forget_dedup(scope, dedup_key, delivered)
            # 已经交付出去了就别再报「处理出错」：卡片已经发到群里了，
            # 收尾步骤（记解析记录之类）失败不影响用户拿到了内容，
            # 再补一句报错只会让人以为这条链接没解析成功。
            if not delivered and e.notify_prefix and self._send_errors:
                yield event.plain_result(f"{e.notify_prefix} {e.message}")
        except httpx.RequestError as e:
            diagnostic.stage(phase, 'error', 'network')
            diagnostic.finish('error')
            # 网络层故障（短链展开的 ConnectError / TimeoutException 等）单独归一类：
            # 它不是「解析器不认识这条链接」，也不是代码 bug，报错文案要能区分开。
            # 日志**只记平台与异常类型**：异常原文常带完整 URL（短链里可能含 token），
            # 不能落进日志，也不打 traceback。
            logger.warning(
                f"[denia_share] 网络请求失败: platform={self._error_platform(parser)} "
                f"type={type(e).__name__}"
            )
            # 失败就撤占位（见 _forget_dedup：未交付才撤），下一个人能重试。
            self._forget_dedup(scope, dedup_key, delivered)
            if not delivered and self._send_errors:
                # 不回显异常原文 / URL：只说明「这次没拿到内容、是网络问题」
                yield event.plain_result("❌ 网络请求失败，请检查网络或稍后重试")
        except Exception as e:
            diagnostic.stage(phase, 'error', 'parse_failed' if phase == 'parse' else 'send_failed')
            diagnostic.finish('error')
            logger.exception("解析异常")
            self._forget_dedup(scope, dedup_key, delivered)
            if not delivered and self._send_errors:
                # 不回显异常原文：里面常带完整 URL 与容器内本地路径
                yield event.plain_result(
                    f"❌ 处理出错（{type(e).__name__}），详情见 AstrBot 日志"
                )
        finally:
            if diagnostic.status == 'running':
                diagnostic.finish('unknown')

    # ---- 同群重复链接去重的辅助 ----

    #: 窗口内后来者收到的提示。只发一次（见 core/link_dedup 的说明）。
    _DEDUP_HINT = "这个链接刚刚已经解析过了哦"
    _UNCERTAIN_HINT = "上一条合并转发的发送结果还没确认，请先查看聊天记录；若未收到，等去重窗口结束后再发。"

    @staticmethod
    def _dedup_applies(event: AstrMessageEvent) -> bool:
        """这次要不要走去重。

        **只在群聊里去重**：需求原文是「同一个群多个链接时触发」。私聊里
        一个人连发两次同一链接，第二次多半是「没看到回复再发一遍」，这时候
        拦下来只会让人以为插件坏了 —— 私聊本来就不存在「刷屏」问题。

        判断不出是不是私聊时（``is_private_chat()`` 本身抛异常）按**不去重**
        处理：宁可多发一次，也不要在正常路径上误伤。
        """
        try:
            return not bool(event.is_private_chat())
        except Exception:
            logger.debug("[denia_share] 私聊判定失败，本次不去重", exc_info=True)
            return False

    @staticmethod
    def _error_platform(parser: Any) -> str:
        """日志用的平台字段。**不是** URL —— 只用来区分「哪个平台网络挂了」。"""
        platform = getattr(parser, "platform", None)
        name = getattr(platform, "name", None)
        if name:
            return str(name)
        return type(parser).__name__

    def _forget_dedup(
        self, scope: str | None, dedup_key: str | None, delivered: bool
    ) -> None:
        """撤销去重占位。

        三个条件都要满足才撤：登记过（scope / dedup_key 都有值）、还没交付
        出去、确实走了去重分支。缺一个都会出问题：

        - 没登记过就撤：``None`` 拼出来的键不存在，无害，但会掩盖「忘记登记」
        - **已经交付还撤**：下一个人会重新解析并**再发一张卡片** —— 那正是这个
          功能要消灭的事。``_record_history`` 这类交付后的收尾步骤失败时最容易
          踩到。
        - 传的必须是**登记时的去重键**（``_dedup_key`` 算出来的那个），不是结果
          缓存键：平台做了跨形式归一（如 B站带参长链与裸 BV）时两者不同，传错
          就撤不掉占位，失败一次废掉整个窗口。
        """
        if not delivered and scope and dedup_key:
            self._link_dedup.forget(scope, dedup_key)

    def _dedup_key(self, parser: Any, searched: Any, cache_key: str) -> str:
        """算这次链接的**同群去重键**：优先用平台给出的规范身份。

        平台的规范身份由 :meth:`core.base_parser.BaseParser.dedup_identity` 提供，
        它能把「同一条资源的不同输入形式」归一（B站：带参分享长链 / 裸 BV /
        ``bilibili.com/BV…``），解决「第一人发卡片、第二人发 BV 直链仍再次解析」。

        三条要求：

        - **解析器没有该方法、返回 None / 空串、或实现抛异常时**，一律回落到
          ``cache_key``（= 匹配到的原文哈希），行为与旧版逐字节一致 —— 这个
          hook 是可选的，不能让第三方自定义解析器因为缺它而挂掉；
        - 身份只用于**去重**，不参与结果缓存：缓存仍按用户实际发的链接算，
          去重窗口过期后「换种发法」仍会正常解析（窗口过期行为保持不变）；
        - 加 ``dedup:`` 前缀再哈希，避免规范身份与「原文哈希」落在同一命名空间里
          相互撞键（两个 md5 输入不同，碰撞概率可忽略）。
        """
        resolver = getattr(parser, "dedup_identity", None)
        if not callable(resolver):
            return cache_key
        try:
            identity = resolver(searched)
            if isinstance(identity, str) and identity.strip():
                return self.result_cache_key("dedup:" + identity.strip())
        except Exception:
            # 归一实现抛异常（第三方自定义解析器、脏字符串编码失败）不能拖垮解析：
            # 去重少一次，也不能让这条链接永远解析不了。
            logger.debug("[denia_share] 去重身份归一失败，回落原文键", exc_info=True)
        return cache_key

    async def _prepare_dedup_target(
        self, parser: Any, keyword: str, searched: Any
    ) -> tuple[str, Any]:
        """去重前的**异步**预处理：短链先展开成解析器真正认领的匹配。

        平台解析器可覆写 :meth:`core.base_parser.BaseParser.dedup_expand` 做这件事
        （B站 ``b23.tv`` / ``bili2233.cn``：BV 藏在跳转后面，不跳就按不了视频身份
        去重）。返回的匹配会被调用方**直接拿去 ``parse``**，所以这次跳转就是原来
        ``_parse_short_link`` 本来要跳的那一次 —— 不会为拿身份多跳一次，也不会
        多解析一次。

        三条要求：

        - **没实现该 hook 的解析器**（含所有第三方自定义解析器、以及测试里的替身）
          原样返回，行为与旧版一致；
        - **异常照原样抛出**：展开失败要走现有解析失败策略（撤销占位、让下一个人
          重试），不能在这里吞成一个「看起来解析过了」的静默分支；
        - 返回 ``None`` / 拿不到匹配时也原样返回，由调用方按原文键去重。
        """
        expander = getattr(parser, "dedup_expand", None)
        if not callable(expander):
            return keyword, searched
        expanded = await expander(keyword, searched)
        if not expanded:
            return keyword, searched
        new_keyword, new_searched = expanded
        if new_searched is None:
            return keyword, searched
        return new_keyword, new_searched

    @staticmethod
    def _event_scope(event: AstrMessageEvent) -> str:
        """取会话标识（``unified_msg_origin`` = 平台:类型:会话ID）。

        取不到时退回发送者 id：宁可把不同群当成同一个（少了去重机会），
        也不要反过来把不同会话混成一条 —— 混成一条会误伤正常解析。
        """
        origin = getattr(event, "unified_msg_origin", "") or ""
        if origin:
            return str(origin)
        try:
            return f"user:{event.get_sender_id()}"
        except Exception:
            return "unknown"

    def _dedup_window(self) -> int:
        """读去重窗口秒数；脏值回落到默认并留一条日志。

        不能让 ``int()`` 裸抛：异常会一路冒到 _process_url 的 except Exception，
        变成「处理出错」而这条链接永远解析不了。
        """
        try:
            return int(get_config().DUPLICATE_LINK_WINDOW_SECONDS)
        except (TypeError, ValueError):
            logger.warning(
                "[denia_share] 同群重复链接去重窗口配置不是整数，按 60 秒处理"
            )
            return 60

    async def _deliver(
        self, event: AstrMessageEvent, result: ParseResult, cache_key: str, *, on_delivered=None, diagnostic=None
    ) -> AsyncGenerator[MessageEventResult, None]:
        """把已得到的 ParseResult 按用户选中的输出模式发出去。

        三个独立分支（文本 / 卡片 / 原始媒体），互不排斥、互不代偿：
        某一支失败**只会让那一支没有产物**，绝不自动补发用户没勾的那几路。

        链接解析与 /pixiv 搜索共用这条输出链路。
        """
        pconfig = get_config()
        modes = pconfig.output_modes()
        text_on = bool(modes["text"])
        card_on = bool(modes["card_effective"])
        media_on = bool(modes["media"])
        def stage(name, state, reason=''):
            if diagnostic is not None:
                diagnostic.stage(name, state, reason)

        # 全关时本就不会走到这里（入口已短路），留一道兜底防止将来新增调用方
        if not (text_on or card_on or media_on):
            logger.info("[denia_share] 三路输出全关，本次不发送任何内容")
            return

        # 缺料审计只覆盖**用户真的要的**媒体：媒体一路开着才审计全部内容，
        # 仅卡片时只审计渲染真正会去取的图（封面/头像/图集，且跳过派生任务）。
        # 审计会 get() 这些任务，也就是**这一步才是触发下载的地方** —— 全关时
        # 一次都不会被调用，一个字节都不下载。
        if media_on or card_on:
            stage('media', 'running')
            try:
                missing = await self._audit_for_modes(result, media_on=media_on)
                limited = bool(result.extra.get('limit_warnings'))
                stage('media', 'error' if missing else 'skipped' if limited else 'ok',
                    'media_missing' if missing else 'limit' if limited else '')
            except Exception:
                stage('media', 'error', 'media_missing')
                missing = {}
                logger.warning("[denia_share] 媒体下载结算失败", exc_info=True)
            if missing:
                logger.warning(
                    f"[denia_share] 本次解析有媒体未落盘: "
                    f"{'、'.join(f'{k}×{v}' for k, v in missing.items())}"
                )
        else:
            stage('media', 'skipped', 'skipped')

        # ---- 卡片 ----
        # 渲染/发送都可能抛（Pillow 缺字体、协议端拒绝、磁盘满）。它们**只能**
        # 让卡片这一路没有产物，绝不能把已经选中的文本与媒体一起带崩。
        # 同样地，这里也**不会**因为卡片失败就改发用户没勾的那几路。
        render_path: Path | None = None
        if card_on:
            stage('render', 'running')
            try:
                render_path = await self._renderer.render(
                    result, cache_key=cache_key,
                    existing=self._render_cache.get(cache_key),
                )
            except Exception:
                render_path = None
                logger.warning("[denia_share] 卡片渲染失败，继续发送已勾选的其他输出", exc_info=True)
            if render_path is not None:
                self._remember_render(cache_key, render_path)
            stage('render', 'ok' if render_path is not None else 'error', '' if render_path is not None else 'render_failed')
        else:
            stage('render', 'skipped', 'skipped')

        # ---- 文本 ----
        # 渲染失败**不**回退成文本：文本只在 text_on 时才发。反过来也一样。
        if text_on:
            async for r in self._send_text(event, result):
                yield r

        if render_path is not None:
            # 卡片发不出去（协议端拒绝/找不到会话）也不能连累已勾选的媒体
            try:
                if await self._send_image(event, render_path):
                    if on_delivered is not None:
                        on_delivered()
                else:
                    stage('send', 'error', 'send_failed')
            except Exception:
                stage('send', 'error', 'send_failed')
                logger.warning("[denia_share] 卡片发送失败，继续发送已勾选的其他输出", exc_info=True)

        # ---- 原始媒体（图片/视频/动图/音频） ----
        if media_on:
            async for r in self._try_send_media(event, result, on_delivered=on_delivered):
                yield r

    async def _audit_for_modes(
        self, result: ParseResult, *, media_on: bool
    ) -> dict[str, int]:
        """按输出模式结算媒体下载，返回失败统计。

        - **媒体一路开着**：审计全部内容（与旧行为一致），提示会进
          ``extra["limit_warnings"]``，由已选中的文本/卡片如实展示。
        - **仅卡片**：只审计渲染会去取的图，且**跳过 ``needs_media`` 的派生任务**
          （用 ffmpeg 抽帧那种）。否则为了给卡片补一张封面就得先下整段视频，
          与「仅卡片只要封面和头像」的意图相反。

        这里走 :meth:`ParseResult.audit_missing_media` 之外的精简路径时，
        仍遵守它「幂等、只撤自己写的那条」的所有权约定。
        """
        if media_on:
            return await result.audit_missing_media()

        # 仅卡片：把派生任务（needs_media）从审计范围里摘掉后再走同一条通道
        return await result.audit_missing_media(img_only=True)

    def _remember_result(self, cache_key: str, result: ParseResult) -> None:
        """写入解析结果内存缓存，并维持条数上限（见 MAX_RESULT_CACHE_ENTRIES）。"""
        cache = self._result_cache
        cache[cache_key] = (time.monotonic(), result)
        # dict 保持插入序：超限就丢最早写入的（FIFO 够用，这里只是防无界增长）
        while len(cache) > MAX_RESULT_CACHE_ENTRIES:
            evicted = next(iter(cache))
            cache.pop(evicted, None)
            # 结果被淘汰时**连带摘掉渲染索引**：留着的话，
            # ``renderer.render`` 会拿 existing 直接返回旧图，而那条图对应的
            # 结果已经不存在了，索引就成了无主条目。
            self._render_cache.pop(evicted, None)

    def _remember_render(self, cache_key: str, path: Path) -> None:
        """登记渲染产物并维持索引条数上限（见 MAX_RENDER_CACHE_ENTRIES）。

        WebUI 的独立预览也走这里：那条入口**不经** ``_remember_result``，
        曾经绕过上限直接写 ``_render_cache``（只受结果缓存淘汰牵动）。
        """
        self._render_cache[cache_key] = path
        while len(self._render_cache) > MAX_RENDER_CACHE_ENTRIES:
            self._render_cache.pop(next(iter(self._render_cache)), None)

    @staticmethod
    def _cache_media_gone(result: ParseResult) -> bool:
        """缓存里的结果，其媒体文件是否**已经被磁盘清理掉了**。

        为什么必须查：``PathTask`` 首次下载后把路径存进 ``_path``，之后再取
        一律直接返回，**不检查文件还在不在**；发送侧也不一定查
        ``path.exists()``。于是只要解析结果还在内存里、而磁盘文件已按
        ``CACHE_TTL_HOURS`` 被清掉，就会拿着一个不存在的路径去发媒体 ——
        卡片正常发出去、**视频/图片/封面静默消失**，日志里什么都没有。

        覆盖范围用 ``result._iter_media_tasks()``（视频 / 封面 / 音频 / 图片 /
        graphics / 转发），那是 ``core/data.py`` 里已有的媒体清单，收口在一处
        免得以后加了新内容类型这里漏掉。头像另算 —— 它只是装饰，但它同样会
        画在卡片上，文件没了会出现裂图。

        只查已解析出路径的（``resolved`` 非 None）：还在下载中的、或下载失败
        的不据此判定，那两种情况另有分支管。
        """
        tasks = list(result._iter_media_tasks())
        author = getattr(result, "author", None)
        avatar = getattr(author, "avatar", None) if author is not None else None
        if avatar is not None:
            tasks.append(("头像", avatar))

        for _label, task in tasks:
            path = task.resolved
            if path is not None and not path.exists():
                return True
        return False

    def _get_cached_result(self, cache_key: str) -> ParseResult | None:
        """取可用的缓存结果；不可用（过期 / 媒体已清理）时顺手删掉并返回 None。

        两种失效都**删掉条目**而不是留着等下次 —— 留着等于每次都白查一遍
        磁盘，而且会让「清空缓存」之外的路径继续返回一份发不出媒体的结果。
        """
        entry = self._result_cache.get(cache_key)
        if entry is None:
            return None
        stored_at, result = entry

        # 读侧兑底：手改过 JSON / 配置项是脏值时不能让整条链路跟着报错。
        # 用 int() 包一层 —— 裸 int("abc") 抛 ValueError，会被 _process_url 的
        # except Exception 吞成「处理出错」，而且异常发生在 pop 之前，
        # 坏条目永远摘不掉，那一个 key 从此每次都报错、无法自愈。
        try:
            ttl = int(get_config().RESULT_CACHE_TTL_SECONDS)
        except (TypeError, ValueError):
            ttl = 600
            logger.warning(
                "[denia_share] 重复解析间隔配置不是整数，按 600 秒处理",
            )
        if ttl <= 0:
            # 0 = 关掉这个优化，每次都重新解析。**不能写成「跳过过期检查」**：
            # 那样 TTL=0 反而变成永不过期，与配置项文案正好相反。
            self._drop_cache_entry(cache_key)
            return None
        if (time.monotonic() - stored_at) >= ttl:
            self._drop_cache_entry(cache_key)
            logger.debug(
                f"[denia_share] 解析结果已过重复解析间隔（{ttl}s），重新解析"
            )
            return None

        if self._cache_media_gone(result):
            self._drop_cache_entry(cache_key)
            logger.info(
                "[denia_share] 缓存里的媒体文件已被清理，重新解析一次（只是白费一次请求）"
            )
            return None

        # 轻量测试桩（只实现几个方法的替身）没有这个方法，getattr 兜一层，
        # 免得缓存读取在桩上直接 AttributeError。
        failed_check = getattr(self, "_cache_media_failed", None)
        if failed_check is not None and failed_check(result):
            # 缓存里这份结果已经**确定拿不到**媒体：留着它等于让后续每一次
            # 同链接都命中一份缺料的结果，而且它自己永远不会自愈。摘掉，
            # 让下一次真正重新解析（PathTask 侧只对临时失败有限重试，
            # 这里处理的是重试也没救回来的那部分）。
            self._drop_cache_entry(cache_key)
            logger.info(
                "[denia_share] 缓存里的媒体下载已失败，淘汰后重新解析一次"
            )
            return None

        return result

    @staticmethod
    def _cache_media_failed(result: ParseResult) -> bool:
        """缓存结果里是否已有**确定失败**的媒体任务（见 PathTask.failed）。"""
        for _label, task in result._iter_media_tasks():
            if task.failed:
                return True
        return False

    def _drop_cache_entry(self, cache_key: str) -> None:
        """摘掉一条解析结果，并连带摘掉它的渲染卡片。

        两者必须**同步**摘：卡片是从这份结果画出来的，结果都重解析了，卡片还
        留着的话，``renderer.render`` 见到 ``existing.exists()`` 就直接返回旧图，
        用户看到的标题/点赞数是上一次的。只做一半会出现「视频是新的、卡片是旧的」。
        """
        self._result_cache.pop(cache_key, None)
        self._render_cache.pop(cache_key, None)

    # ==================== 解析记录 ====================

    async def _record_history(
        self,
        result: ParseResult,
        render_path: Path | None,
        *,
        via: str = "auto",
        elapsed_ms: int | None = None,
    ) -> ParseRecord | None:
        """把一次成功的解析写入历史，供 WebUI 查看与管理。

        这是纯旁路：任何异常都只记日志，绝不影响正常的解析与发送。
        """
        try:
            media_files: list[str] = []
            for cont in result.contents:
                path = cont.path_task.resolved
                if path is not None:
                    media_files.append(path.name)

            record = ParseRecord.create(
                url=result.url or "",
                platform=result.platform.name,
                # 记录页是管理视图，用**正式名**：页面上的平台筛选与统计都是正式名，
                # 行内再显示「猴山」这种卡片叫法就会出现同一平台两个名字并排
                platform_display=platform_meta(result.platform.name).display_name,
                content_type=result.content_type,
                title=result.title or "",
                author=result.author.name if result.author else "",
                via=via,
                card_file=render_path.name if render_path else None,
                media_files=media_files,
                detail=self._history_detail(result),
                elapsed_ms=elapsed_ms,
            )
            if not await asyncio.to_thread(self.history.add, record):
                # 写盘失败（Windows 上 os.replace 会撞开着的文件）：记录没能落盘，
                # 不能假装成功 —— 至少要在日志里说清楚，而不是静默少一条。
                logger.warning(
                    "[denia_share] 解析记录未能落盘（记录页可能正被占用），本条记录已丢失"
                )
            return record
        except Exception:
            logger.warning("[denia_share] 写入解析记录失败", exc_info=True)
            return None

    @staticmethod
    def _history_detail(result: ParseResult) -> dict[str, Any]:
        """挑出适合展示在记录详情里的字段（不含媒体本体）。"""
        extra = result.extra or {}
        return {
            "stats_line": extra.get("stats_line") or "",
            "duration": extra.get("duration") or "",
            "online": extra.get("online") or "",
            "info": extra.get("info") or "",
            "text": (result.text or "")[:300],
            "image_count": len(result.img_contents),
            "video_count": len(result.video_contents),
            "audio_count": len(result.audio_contents),
            "warnings": list(extra.get("limit_warnings") or []),
        }

    # ==================== 配置热生效 ====================

    async def apply_runtime_config(self) -> dict[str, Any]:
        """按最新配置重建运行时组件。

        ``ParserConfig`` 直接读取 AstrBotConfig，所以配置值本身是实时的；
        这里负责重建那些「构造时把配置读死」的对象（解析器表、渲染器、截图服务、
        下载器参数），让网页端保存后不需要重载插件。
        """
        pconfig = get_config()
        summary: dict[str, Any] = {"rebuilt": False}

        self.disabled_platforms = pconfig.DISABLED_PLATFORMS
        self._send_errors = pconfig.SEND_ERROR_MESSAGES

        # 共享缓存目录可能被改了：换目录后旧的内存缓存全部失效
        new_cache_dir, source = self._resolve_cache_dir(
            self._data_dir, pconfig.CACHE_DIR
        )
        self.cache_dir_source = source
        if new_cache_dir != self.cache_dir:
            self.cache_dir = new_cache_dir
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            pconfig.cache_dir = self.cache_dir
            self._result_cache.clear()
            self._render_cache.clear()
            self._link_dedup.clear()
            logger.info(f"[denia_share] 媒体缓存目录已切换到 {self.cache_dir}")
        summary["cache_dir"] = str(self.cache_dir)
        summary["cache_dir_source"] = self.cache_dir_source

        try:
            await self.downloader.reconfigure(
                proxies=None,
                max_size_mb=pconfig.VIDEO_SIZE_MAXIMUM_MB,
                cache_dir=self.cache_dir,
                verify_ssl=pconfig.HTTP_VERIFY_SSL,
            )
        except Exception:
            logger.warning("[denia_share] 更新下载器参数失败", exc_info=True)

        # 代理下载器跟随同一份缓存目录 / 体积上限 / 证书开关，代理地址取配置值。
        # 地址被清空时 reconfigure 会把它的池切成直连 —— 池子留着不关，
        # 省得下次有平台开开关又要重建。
        if self._proxy_downloader is not None:
            try:
                await self._proxy_downloader.reconfigure(
                    proxies=pconfig.PROXY or None,
                    max_size_mb=pconfig.VIDEO_SIZE_MAXIMUM_MB,
                    cache_dir=self.cache_dir,
                    verify_ssl=pconfig.HTTP_VERIFY_SSL,
                )
            except Exception:
                logger.warning("[denia_share] 更新代理下载器参数失败", exc_info=True)

        self.parsers = {}
        self._init_parsers()

        music_controller = getattr(self, "_music_controller", None)
        if music_controller is not None:
            music_controller.reconfigure()

        # B站 Cookie **只在配置项真的变了的时候**才动。
        #
        # 为什么不能无条件重新应用：这个方法是「保存配置 / 恢复默认 / 保存外观」
        # 三个接口都会走的（webui.py:433/463/497）。无条件应用会有两种误伤 ——
        #   · 原先「登录态优先」：改 BILI_CK 不生效，接口却回 changed + runtime_ok
        #   · 改成「配置项优先」后：配置项非空时，改任何别的配置（代理/外观）都会
        #     把配置项里的 cookie 再应用一次，覆盖掉期间的扫码登录结果
        # 两种都是「用户没动 cookie，cookie 却被改了」。所以按「变化」触发。
        current_ck = str(pconfig.BILI_CK or "")
        if current_ck != self._bili_ck_applied:
            self._bili_ck_applied = current_ck
            if current_ck:
                self._bili_cookie = current_ck
                self._bili_apply_cookie_to_parser(current_ck)
            else:
                # 配置项被清空 = 用户明确要清掉（「恢复默认」的文案承诺了「包括 Cookie」）。
                # 走 bili_logout 而不是只清配置项：它会把主模块内存、解析器的持久化文件
                # 与 bili_cookie.json 一起清掉，否则「看起来清了、实际还在用」。
                await self.bili_logout()

        self._renderer = ShareCardRenderer(self.cache_dir, **pconfig.renderer_options())
        # 渲染参数进了产物文件名，配置变了就得让旧缓存失效
        self._render_cache.clear()

        # 解析结果缓存**同样要清**：B站编码 / 清晰度 / 代理 / 限长这些配置项
        # 都影响解析结果。不清的话，用户在配置页把编码从 AV1 换成 H.264，
        # 再发同一条链接，拿到的还是旧结果 —— 配置改了却看不出效果，
        # 正是最容易被当成「插件没生效」的一类问题。
        self._result_cache.clear()
        # 去重窗口同理：窗口长度本身就是配置项之一，热更新后必须立即按新值判定，
        # 否则用户把窗口从 60 改成 0 以为关掉了，实际还要等旧窗口过期
        self._link_dedup.clear()

        # 截图后端的代理 / 证书校验同样是构造时读死的，热更新要一起重建
        self._build_screenshot_service()

        summary["rebuilt"] = True
        summary["platforms"] = sorted(self.parsers)
        summary["render_enabled"] = self._renderer.enabled
        return summary

    # ==================== 网页截图 ====================

    @staticmethod
    def _first_url(event: AstrMessageEvent) -> str | None:
        urls = URL_PATTERN.findall(event.message_str or "")
        return urls[0] if urls else None

    def _match_parser(self, url: str) -> str | None:
        """找出能处理该 URL 的解析器名，没有则返回 None。"""
        for name, parser in self.parsers.items():
            try:
                parser.search_url(url)
                return name
            except Exception:
                continue
        return None

    def _match_builtin_parser(self, url: str) -> str | None:
        """该 URL 是否归一个**当前启用**的内置平台；是则返回平台键。

        只在内置平台启用时才算命中：内置平台被禁用后它的处理器会直接 return
        （``_dispatch`` 里 ``self.parsers.get(name)`` 拿不到），这时应当让自定义
        解析器接过去 —— README 承诺过自定义解析器可以「改掉某个内置平台的行为」。
        """
        for name, parser in self.parsers.items():
            if is_custom_platform(name):
                continue
            try:
                parser.search_url(url)
                return name
            except Exception:
                continue
        return None

    def _match_custom_parser(self, url: str):
        """找出能处理该 URL 的自定义解析器实例；没有则返回 None。"""
        for name, parser in self.parsers.items():
            if not is_custom_platform(name):
                continue
            try:
                parser.search_url(url)
                return parser
            except Exception:
                continue
        return None

    # ==================== 示例卡片（外观页离线预览） ==================== #

    def _sample_image(self, kind: str) -> Path:
        """合成/复用一张离线示例图（渐变 + 几何图形），不联网。"""
        from PIL import Image as _Image, ImageDraw as _Draw

        path = self.cache_dir / f"_sample_{kind}.png"
        if path.is_file():
            return path
        if kind == "hero":
            w, h = 1280, 720
            img = _Image.new("RGB", (w, h))
            top, bottom = (58, 76, 128), (16, 20, 32)
            for yy in range(h):
                t = yy / max(h - 1, 1)
                img.paste(tuple(round(top[i] + (bottom[i] - top[i]) * t) for i in range(3)),
                          (0, yy, w, yy + 1))
            dr = _Draw.Draw(img, "RGBA")
            dr.ellipse((int(w * 0.62), -h // 3, int(w * 1.25), h // 2), fill=(139, 124, 246, 90))
            dr.ellipse((-w // 5, int(h * 0.55), w // 3, int(h * 1.4)), fill=(46, 196, 182, 70))
            dr.rounded_rectangle((int(w * 0.08), int(h * 0.6), int(w * 0.5), int(h * 0.78)),
                                 radius=28, fill=(255, 255, 255, 26))
        else:  # avatar
            size = 256
            img = _Image.new("RGB", (size, size), (36, 43, 63))
            dr = _Draw.Draw(img, "RGBA")
            # 椭圆要的是 (x0, y0, x1, y1) 四个坐标，少一个会被 Pillow 判为参数错误
            dr.ellipse((28, 28, size - 28, size - 28), fill=(251, 114, 153, 255))
            dr.ellipse((88, 96, 168, 176), fill=(255, 255, 255, 235))
            dr.arc((64, 150, 192, 236), start=15, end=165, fill=(255, 255, 255, 235), width=14)
        try:
            img.save(path, "PNG")
        except OSError:
            logger.warning("[denia_share] 示例图写入失败", exc_info=True)
        return path

    def build_sample_result(self) -> ParseResult:
        """构造离线示例解析结果（视频卡片：封面 + 头像 + 统计），供外观页实时预览。"""
        from .core.task import PathTask

        async def _static(path: Path) -> Path:
            return path

        hero_path = self._sample_image("hero")
        avatar_path = self._sample_image("avatar")
        video = VideoContent(
            path_task=PathTask(_static(hero_path)),
            cover=PathTask(_static(hero_path)),
            duration=309,
        )
        return ParseResult(
            platform=platform_of(PlatformEnum.BILIBILI),
            author=Author(
                name="希望示例频道",
                avatar=PathTask(_static(avatar_path)),
                description="本卡片为离线示例，用于预览外观效果",
            ),
            title="示例：全新外观设计器效果预览",
            text="这是一条用于预览的示例简介。调整左侧的主题、布局、强调色、水印等参数，"
                 "卡片会立刻按新外观重新渲染，无需发送任何真实链接。",
            timestamp=1760000000,
            url="https://www.bilibili.com/video/BV1uth56uEz3",
            contents=[video],
            extra={
                "content_type": "视频",
                "stats_line": "👍 12.3万 🪙 3.4万 ⭐ 5.6万 👀 210.5万",
                "duration": "05:09",
                "ai_summary": "这条离线示例展示了封面、作者和播放信息的排列。此摘要仅用于预览开关效果，不代表真实视频内容。",
                "ai_summary_status": "ok",
            },
        )

    async def _do_screenshot(self, event: AstrMessageEvent, url: str):
        # /shot 与「匹配不到平台就截图」两个入口都汇到这里
        if self._access_denied(event):
            return
        if not is_probably_screenshotable(url):
            yield event.plain_result("这个地址不支持截图")
            return
        if not self.screenshot.is_configured:
            yield event.plain_result(
                "截图后端未配置：使用 Cloudflare 时请填写 Account ID 与 API Token"
            )
            return

        # 刻意**不套卡片模板**：整页长图塞进 16:9 的封面槽会被裁到只剩约 15%
        # （等于废掉），开「封面不裁切」又会撑出一张两米高的卡片 —— 两种都
        # 没法在聊天里看。截图就是单独发出去的原图，用文字把「这是什么」讲清楚。
        full_page = get_config().SCREENSHOT_FULL_PAGE
        path = await self.screenshot.capture(url, full_page=full_page)
        if path is None:
            detail = self.screenshot.last_error or "未知原因"
            yield event.plain_result(f"截图失败：{detail}\n{url}")
            return

        caption = await self._screenshot_caption(path, url, full_page=full_page)
        await self._send_image(event, path)
        yield event.plain_result(caption)

    async def _screenshot_caption(
        self, path: Path, url: str, *, full_page: bool
    ) -> str:
        """给截图配一句能认出这张图的说明。

        只有 URL 的话，图是匿名的 —— 群里翻上去只能看到一张长图。所以尽量
        补上页面标题、尺寸、体积，让人一眼知道是什么、为什么这么大。

        标题是尽力而为的装饰（见 :meth:`ScreenshotService.page_title`），
        拿不到就少一行，不影响其他信息。
        """
        parts: list[str] = []

        title = await self.screenshot.page_title(url)
        parts.append(f"网页截图：{title}" if title else "网页截图")

        notes: list[str] = []
        size_bytes = 0
        try:
            from PIL import Image as _Image

            with _Image.open(path) as im:
                width, height = im.size
            size_bytes = path.stat().st_size
            parts.append(f"{width}×{height}，{max(1, size_bytes // 1024)} KB")
            notes = describe_capture(
                height, full_page=full_page, backend=self.screenshot.backend
            )
        except Exception:  # noqa: BLE001 —— 读不出尺寸就不写，别为装饰抛异常
            pass

        parts.append(url)
        parts.extend(notes)

        # 超大图提醒一句：群里客户端会压缩/可能拒收，先给用户一个预期
        if size_bytes > 5 * 1024 * 1024:
            parts.append(
                "（整页长图较大，客户端可能压缩或发送失败；"
                "可在配置里关掉「截整页长图」）"
            )
        return "\n".join(parts)

    async def _send_image(self, event: AstrMessageEvent, path: Path):
        """主动发送图片，绕开事件回复管线，避免被附加「引用回复 / @」。"""
        try:
            sent = await self.context.send_message(
                event.unified_msg_origin, MessageChain().file_image(str(path)),
            )
            if not sent:
                logger.warning(f"解析卡片主动发送未找到匹配平台会话: {path.name}")
            return bool(sent)
        except Exception as e:
            logger.warning(f"解析卡片主动发送异常: {e}")
            return False

    @staticmethod
    async def _build_nodes_result(event: AstrMessageEvent, header: str, items: list[list]):
        sender_name = "希望解析器"
        sender_id = event.get_self_id()
        nodes = Comp.Nodes([])

        def _node(content):
            # AstrBot 4.x 的 Node.__init__(content, **_) 会把 uin/name 吞进 **_ 丢弃，
            # 必须构造后赋值属性，否则合并转发里昵称与 QQ 号全空
            node = Comp.Node(content=content)
            node.uin = sender_id
            node.name = sender_name
            return node

        if header:
            nodes.nodes.append(_node([Comp.Plain(header)]))
        for item in items:
            nodes.nodes.append(_node(item))
        return event.chain_result([nodes])

    # ==================== Markdown 图片集中展示 ====================

    @staticmethod
    def _collect_markdown_images(result: ParseResult) -> list[ImageContent]:
        """收集结果里所有**带公网 URL** 的图片，按展示顺序返回 ``ImageContent``。

        图片在 ``contents``（``img_contents``）和 ``graphics`` 里都可能出现，
        两种都要收；``repost`` 只递归一层（转发帖不会自己再套一层转发）。

        没有 URL 的图片（外部直接塞 Task 的构造方式）无法用 Markdown 渲染，
        这里直接跳过 —— 调用方据返回值数量决定是否值得走 Markdown。
        """
        images: list[ImageContent] = []

        for image in result.img_contents:
            if image.url:
                images.append(image)

        for graphic in result.graphics:
            if isinstance(graphic, ImageContent) and graphic.url:
                images.append(graphic)

        if result.repost is not None:
            images.extend(DeniaSharePlugin._collect_markdown_images(result.repost))

        return images

    @staticmethod
    async def _probe_image_size(image: ImageContent) -> tuple[int, int] | None:
        """读本地已落盘图片的真实宽高，拿不到返回 ``None``。

        这里只读本地文件：图片在发送前的媒体审计里已经下载过，所以是一次
        纯磁盘读取，不发网络请求。文件不在或不是图片时返回 ``None``，
        调用方回退到配置的固定宽高，不影响发送。
        """
        try:
            path = await image.path_task.safe_get()
        except Exception:
            return None
        if path is None:
            return None
        try:
            from PIL import Image as PILImage

            with PILImage.open(path) as img:
                return img.size
        except Exception:
            return None

    async def _resolve_markdown_size(self, image: ImageContent) -> tuple[int, int]:
        """算出这张图该用的 ``(宽, 高)``。

        ``auto``（默认）：按原图比例算高度，**不拉伸不变形**；高度超过上限
        时改为压宽度、高度封顶，比例依然不变。
        ``fixed``：直接用配置的宽高（旧行为，竖图会被压扁）。
        探测失败时回退配置值，保证至少能显示。
        """
        pconfig = get_config()
        width = pconfig.MARKDOWN_IMAGE_WIDTH
        max_height = pconfig.MARKDOWN_IMAGE_HEIGHT
        if pconfig.MARKDOWN_IMAGE_SIZE_MODE != "auto":
            return width, max_height

        size = await self._probe_image_size(image)
        if not size:
            return width, max_height
        orig_w, orig_h = size
        if orig_w <= 0 or orig_h <= 0:
            return width, max_height

        height = round(width * orig_h / orig_w)
        if height > max_height:
            height = max_height
            width = max(50, round(max_height * orig_w / orig_h))
        return max(50, width), max(50, height)

    async def _build_markdown_messages(
        self,
        result: ParseResult,
        images: list[ImageContent],
    ) -> list[str]:
        """把图片列表组装成若干条 Markdown 文本。

        - **只包含图片**：不带头信息（平台/作者/标题），不带图片标签文字，
          QQ 端看到的就是纯图片网格
        - 每行 ``MARKDOWN_IMAGE_PER_ROW`` 张，行内用空格分隔实现并排
        - 超过 ``MARKDOWN_IMAGE_MAX_PER_MSG`` 时按顺序切分，不重不漏
        - 末尾带一行弱化的引用小字，说明 QQ 保存去重误判的处理方式
        """
        pconfig = get_config()
        per_msg = pconfig.MARKDOWN_IMAGE_MAX_PER_MSG
        per_row = pconfig.MARKDOWN_IMAGE_PER_ROW

        messages: list[str] = []
        for offset in range(0, len(images), per_msg):
            chunk = images[offset : offset + per_msg]
            lines: list[str] = []
            for row_start in range(0, len(chunk), per_row):
                row = chunk[row_start : row_start + per_row]
                cells: list[str] = []
                for _idx, image in enumerate(row, start=offset + row_start + 1):
                    url = image.url
                    width, height = await self._resolve_markdown_size(image)
                    # QQ 官方机器人要求 Markdown 图片带 `#宽px #高px`，
                    # 缺了这段 QQ 只会把图片当链接文字渲染。标签文字留空
                    # （一个空格占位），QQ 就只渲染图片本身不出文字。
                    cells.append(f"![ #{width}px #{height}px]({url})")
                lines.append(" ".join(cells))

            messages.append("\n\n".join(lines))

        if messages:
            # QQ 对整条 markdown 消息的保存去重有误判（提示「已保存」但
            # 实际没存），在末尾用块引用（弱化的灰色小字）提示处理方式。
            # 文案由配置项 MARKDOWN_IMAGE_HINT_TEXT 提供，留空则不显示。
            hint = pconfig.MARKDOWN_IMAGE_HINT_TEXT
            if hint:
                messages[-1] += f"\n\n> {hint}"

        return messages

    async def _send_markdown_media(
        self,
        event: AstrMessageEvent,
        result: ParseResult,
        images: list[ImageContent],
    ) -> bool:
        """用 Markdown 消息集中发送图片，返回是否至少成功发出一条。

        走 ``event.send`` 主动发送而不是 yield：只有这样才能拿到发送回执，
        失败时由调用方回退到合并转发 / 逐条发送，不至于整组图丢掉。
        """
        messages = await self._build_markdown_messages(result, images)
        if not messages:
            return False

        sent_any = False
        for index, text in enumerate(messages, start=1):
            chain = MessageChain(chain=[Comp.Plain(text)])
            # 兼容不同 AstrBot 版本：方法优先，其次直接写属性
            if hasattr(chain, "use_markdown"):
                chain.use_markdown(True)
            else:
                chain.use_markdown_ = True
            try:
                ok = await event.send(chain)
            except Exception:
                logger.warning(
                    f"[denia_share] Markdown 图片发送异常（第 {index}/{len(messages)} 条）",
                    exc_info=True,
                )
                return sent_any
            if ok is False:
                logger.warning(
                    f"[denia_share] Markdown 图片未送达（第 {index}/{len(messages)} 条）"
                )
                return sent_any
            sent_any = True

        return sent_any

    async def _build_output(self, result: ParseResult) -> tuple[str, list[list]]:
        """统一构建 (标题头, 内容节点列表)。

        **已不再用于发送**：三路输出模式里文本走 :meth:`_send_text`（一条普通
        消息，不含长简介/AI 总结/合并转发），图片归富媒体一路。这里保留是为了
        兼容仍在引用它的旁路（如调试与既有测试），不要再在发送链路里用它。
        """
        header = f"希望解析器 | {result.platform.display_name} - {result.content_type}"
        if result.author:
            header += f" @{result.author.name}"

        nodes: list[list] = []
        if result.title:
            nodes.append([Comp.Plain(result.title)])
        if result.text:
            nodes.append([Comp.Plain(result.text[:300])])

        for key, prefix in (
            ("stats_line", ""),
            ("duration", "⏱ 时长："),
            ("online", ""),
            ("info", "📝 "),
        ):
            if value := result.extra.get(key):
                nodes.append([Comp.Plain(f"{prefix}{value}")])

        for image in result.img_contents:
            path = await image.path_task.safe_get()
            # exists() 兜底：PathTask 记着首次下载的路径，之后一律直接返回、
            # 不碰文件系统。文件被缓存清理任务删掉时，这里会把一个不存在的
            # 路径交给协议端（图片则渲染成裂图）。视频那条链路已有同样检查。
            if path and path.exists():
                nodes.append([Comp.Image.fromFileSystem(str(path))])
        for graphic in result.graphics:
            if isinstance(graphic, ImageContent):
                path = await graphic.path_task.safe_get()
                if path and path.exists():
                    nodes.append([Comp.Image.fromFileSystem(str(path))])
            elif isinstance(graphic, str):
                nodes.append([Comp.Plain(graphic)])

        return header, nodes

    async def _send_plain_output(self, event: AstrMessageEvent, header: str, nodes_content: list[list]):
        """非 OneBot 平台：合并成一条消息链。"""
        parts: list = []
        if header:
            parts.append(Comp.Plain(header + "\n"))

        text_lines: list[str] = []
        for node_content in nodes_content:
            for comp in node_content:
                if isinstance(comp, Comp.Plain):
                    text_lines.append(comp.text)
                elif isinstance(comp, Comp.Image):
                    parts.append(comp)
        if text_lines:
            parts.append(Comp.Plain("\n".join(text_lines)))

        if parts:
            yield event.chain_result(parts)

    async def _send_text(self, event: AstrMessageEvent, result: ParseResult):
        """发送**一条**普通文本消息（标题 + 干净地址 + 简洁播放信息 + 必要提示）。

        刻意**不**包含：``result.text`` 长简介、``graphics`` 正文、B站
        ``extra["info"]`` 里的 AI 总结，也不走 OneBot 合并转发 —— 一条普通
        ``Plain`` 消息，客户端里就是一条干净的文本。
        """
        lines = build_concise_text(result)
        if not lines:
            logger.info("[denia_share] 精简文本为空，跳过文本发送")
            return
        yield event.chain_result([Comp.Plain("\n".join(lines))])

    async def _try_send_media(
        self, event: AstrMessageEvent, result: ParseResult, *, on_delivered=None
    ):
        """按聚合配置发送富媒体，语音和文件保持独立发送。"""
        pconfig = get_config()
        # 每条媒体链路开始前先清标记：上一次「送达未知」不能继承到下一条链接。
        _DELIVERY_UNCERTAIN.set(False)

        # ---- Markdown 集中展示（优先于合并转发，且不依赖 OneBot）----
        # 合并转发只有 OneBot 支持，而 Markdown 恰好是 QQ 官方机器人能渲染
        # 图片的通道，所以这一段刻意放在 _is_onebot 判断之前。
        markdown_images = self._collect_markdown_images(result)
        if pconfig.should_send_markdown_images(len(markdown_images)):
            delivered = False
            try:
                delivered = await self._send_markdown_media(event, result, markdown_images)
            except Exception:
                logger.warning(
                    "[denia_share] Markdown 图片展示异常，回退原有媒体发送路径",
                    exc_info=True,
                )
            if delivered:
                if on_delivered is not None:
                    on_delivered()
                # 语音、文件、视频不受 Markdown 影响，仍按原规则单独发
                async for parts in self._iter_media_components(result, skip_images=True):
                    yield event.chain_result(parts)
                return
            logger.info("[denia_share] Markdown 图片未送达，回退到原有媒体发送路径")

        if not self._is_onebot(event) or pconfig.AGGREGATION_MODE == "none":
            async for parts in self._iter_media_components(result):
                yield event.chain_result(parts)
            return

        messages = [parts async for parts in self._iter_media_components(result)]
        # 只把图片、视频送进转发，避免一段语音或文件使整份图集发送失败。
        eligible = [
            parts for parts in messages
            if all(isinstance(part, (Comp.Image, Comp.Video)) for part in parts)
        ]
        image_count = sum(isinstance(part, Comp.Image) for parts in eligible for part in parts)
        video_count = sum(isinstance(part, Comp.Video) for parts in eligible for part in parts)
        if not eligible or not pconfig.should_aggregate_nodes(image_count, video_count, len(eligible)):
            for parts in messages:
                yield event.chain_result(parts)
            return

        try:
            forward = await self._build_nodes_result(event, "", eligible)
            # 主动发送才能捕获协议端拒绝；yield 的发送异常在宿主管线中发生。
            sent = await event.send(forward)
            if sent is False:
                raise RuntimeError("合并转发未发送成功")
        except (TimeoutError, ConnectionError):
            # 回执丢失时转发可能已经送达，不能自动重放整套图片/视频。
            # 标记「送达未知」而不是「失败」：调用方据此保留去重窗口、
            # 但不记成功历史（见 _process_url 的对应分支）。
            _DELIVERY_UNCERTAIN.set(True)
            logger.warning("[denia_share] 合并转发送达状态未知，不自动重发图片视频，继续发送独立音频", exc_info=True)
            for parts in messages:
                if not all(isinstance(part, (Comp.Image, Comp.Video)) for part in parts):
                    yield event.chain_result(parts)
            return
        except Exception:
            logger.warning("[denia_share] 合并转发失败，改为逐条发送已选媒体", exc_info=True)
            for parts in messages:
                yield event.chain_result(parts)
            return

        if on_delivered is not None:
            on_delivered()
        for parts in messages:
            if not all(isinstance(part, (Comp.Image, Comp.Video)) for part in parts):
                yield event.chain_result(parts)

    async def _iter_media_components(self, result: ParseResult, *, skip_images: bool = False):
        """构建原始媒体组件，复用下载、缓存和中转规则。

        旧实现里图片是塞进文本节点的（``_build_output``），而精简文本按设计
        **只含标题/地址/播放信息**，所以图片必须在这里显式发出去 —— 否则
        关掉文本就等于把图集弄丢了。卡片图片走 ``_send_image``，与这里无关。

        ``skip_images`` 供 Markdown 展示路径使用：图片已经由 Markdown 消息
        发出去了，这里只补发语音、文件、视频等非图片媒体，避免同一张图发两遍。

        **视频为什么需要特殊处理**：AstrBot 把 ``Video`` 段的 ``file:///...``
        原样交给协议端（图片 / 语音会先转成 base64，所以没这个问题），
        而那个路径是 astrbot 容器内的路径。协议端在另一个容器里读不到，
        视频就发不出去。

        所以开了「媒体中转」就先把文件注册成 ``{回调地址}/api/file/<token>``
        用 URL 发送；没开、注册失败、或地址不合法时，回退成原来的本地文件发送。
        """
        pconfig = get_config()
        relay_enabled = pconfig.MEDIA_RELAY_ENABLED
        callback_base = pconfig.MEDIA_RELAY_CALLBACK_URL
        ttl = pconfig.MEDIA_RELAY_TTL

        # 图片在 contents 里（contents/graphics 混排的平台两种都有），逐个发
        if not skip_images:
            for image in result.img_contents:
                path = await image.path_task.safe_get()
                if path is None or not path.exists():
                    continue
                yield [Comp.Image.fromFileSystem(str(path))]

            for graphic in result.graphics:
                if not isinstance(graphic, ImageContent):
                    continue
                path = await graphic.path_task.safe_get()
                if path is None or not path.exists():
                    continue
                yield [Comp.Image.fromFileSystem(str(path))]

        # 转发的原帖图片同样归富媒体（微博/NGA 那种 repost 里有图集的情况）。
        # 递归只走一层：repost 不会自己再套 repost。
        if result.repost is not None:
            async for parts in self._iter_media_components(
                result.repost, skip_images=skip_images
            ):
                yield parts

        for cont in result.contents:
            if not isinstance(cont, (VideoContent, AudioContent)):
                continue
            path = await cont.path_task.safe_get()
            if path is None:
                continue
            # 双保险：正常情况下主流程已经拦掉了「缓存命中但文件已清理」的情况
            # （见 _cache_media_gone）。这里再挡一次 —— 清理任务可能正好在
            # _get_cached_result 检查之后、这里之前把文件删了。
            if not path.exists():
                logger.warning(
                    f"[denia_share] 媒体文件已不存在，跳过发送: {path.name}"
                )
                continue

            if isinstance(cont, VideoContent):
                url = None
                if relay_enabled:
                    url = await register_file(path, callback_base, ttl)
                    if url is None:
                        logger.info(
                            f"[denia_share] 视频中转不可用，回退本地文件发送: {path.name}"
                        )
                if url:
                    yield [Comp.Video.fromURL(url)]
                else:
                    yield [Comp.Video.fromFileSystem(str(path))]
            else:
                yield [await self._build_audio_component(
                    result, path, relay_enabled, callback_base, ttl
                )]

    async def _try_send_audio(
        self,
        event: AstrMessageEvent,
        result: ParseResult,
        path: Path,
        relay_enabled: bool,
        callback_base: str,
        ttl: int,
    ):
        """单独发送一段音频，与媒体聚合共用组件构建。"""
        yield event.chain_result([await self._build_audio_component(
            result, path, relay_enabled, callback_base, ttl
        )])

    async def _build_audio_component(
        self,
        result: ParseResult,
        path: Path,
        relay_enabled: bool,
        callback_base: str,
        ttl: int,
    ):
        """按「音频发送方式」发音频，**一次只发一种**。

        两种形式的**发送链路并不等价**，不能互相兜底：

        - ``voice`` → :class:`Comp.Record`：AstrBot 侧会转 base64，所以不依赖
          协议端能读到本地路径。
        - ``file`` → :class:`Comp.File`：**不走**语音的 base64 链路。协议端在
          另一个容器时读不到这里的路径，所以复用了视频那套「中转 URL / 本地路径」
          的回退。

        这里刻意**没有**做「语音失败就改发文件」：用户选的是一种形式，
        悄悄换一种等于替他做了决定，而且会在群里多出一条消息。真发不出去时
        下载限制已由审计写入已选文本/卡片；协议端发送结果仍由宿主处理。
        """
        pconfig = get_config()
        if pconfig.AUDIO_SEND_MODE == "file":
            name = _audio_display_name(result, path)
            url = None
            if relay_enabled:
                url = await register_file(path, callback_base, ttl)
                if url is None:
                    logger.info(
                        f"[denia_share] 音频中转不可用，回退本地路径发送: {path.name}"
                    )
            if url:
                return Comp.File(name=name, url=url)
            return Comp.File(name=name, file=str(path))

        # 语音条：沿用既有 Record 路径（AstrBot 侧转 base64，无需中转）。
        return Comp.Record(file=str(path))

    # ==================== B站扫码登录 ====================

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili_login")
    async def bili_qr_login(self, event: AstrMessageEvent):
        """B站扫码登录，Cookie 会持久化保存"""
        sender_id = event.get_sender_id()
        if sender_id in self._bili_login_tasks and not self._bili_login_tasks[sender_id].done():
            yield event.plain_result("⏳ 你有一个正在进行的扫码登录，请先完成或等待超时")
            return

        try:
            qrcode_url, qrcode_key = await self.bili_qr_create()

            try:
                qr_image = qrcode.make(qrcode_url)
            except Exception:
                yield event.plain_result("❌ 二维码生成失败，请检查是否已安装 qrcode 库")
                return

            qr_path = self._bili_data_dir / f"qrcode_{sender_id}.png"
            qr_image.save(str(qr_path), "PNG")
            yield event.image_result(str(qr_path))
            yield event.plain_result(
                "📱 请使用 **B站App** 扫描上方二维码\n⏱️ 有效期约3分钟\n📋 扫码后请在手机上点击「确认登录」"
            )

            self.bili_login_start(
                sender_id, qrcode_key, notify_umo=event.unified_msg_origin, qr_path=qr_path
            )
        except ParseException as e:
            yield event.plain_result(f"❌ {e.message}")
        except Exception as e:
            logger.exception("扫码登录出错")
            yield event.plain_result(f"❌ 生成二维码失败: {e}")

    @filter.command("bili_check")
    async def bili_check_cookie(self, event: AstrMessageEvent):
        """检测B站Cookie是否有效"""
        if not self._bili_cookie:
            yield event.plain_result("⚠️ 尚未配置B站Cookie，请使用 /bili_login 扫码登录")
            return
        result = await self._bili_check_cookie_valid()
        if result["valid"]:
            yield event.plain_result(
                f"✅ B站Cookie有效\n用户: {result.get('username', '未知')}\nUID: {result.get('uid', 0)}"
            )
        else:
            yield event.plain_result(f"❌ B站Cookie失效\n错误: {result.get('error', '未知错误')}")

    # ---------- 扫码登录：WebUI 与聊天命令共用的实现 ----------

    async def bili_qr_create(self) -> tuple[str, str]:
        """向 B站申请扫码登录二维码。

        Returns:
            (扫码 URL, qrcode_key)

        Raises:
            ParseException: 申请失败或返回内容不完整。
        """
        if not self._bili_http_session or self._bili_http_session.closed:
            self._bili_http_session = aiohttp.ClientSession()

        async with self._bili_http_session.get(
            BILI_QR_GENERATE_URL, headers=self._bili_headers(),
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            data = await resp.json()

        if data.get("code") != 0:
            raise ParseException(f"获取二维码失败: {data.get('message', '未知错误')}")

        payload = data.get("data") or {}
        qrcode_url = payload.get("url")
        qrcode_key = payload.get("qrcode_key")
        if not qrcode_url or not qrcode_key:
            raise ParseException("B站未返回二维码地址或 qrcode_key")
        return str(qrcode_url), str(qrcode_key)

    @staticmethod
    def render_qr_png(data: str) -> bytes:
        """把文本渲染成 PNG 字节，供 WebUI 拼成 data URL 展示。"""
        buffer = io.BytesIO()
        qrcode.make(data).save(buffer, "PNG")
        return buffer.getvalue()

    def bili_login_state(self, task_id: str) -> dict[str, Any]:
        """读取扫码登录任务的当前状态（WebUI 轮询用）。"""
        state = self._bili_login_states.get(task_id)
        if state is None:
            return {"state": "unknown", "message": "登录任务不存在或已结束"}
        return dict(state)

    async def bili_login_cancel(self, task_id: str | None = None) -> bool:
        """停止后台扫码轮询，**不登出、不清除已保存的 Cookie**。

        Args:
            task_id: 指定任务；为空时取消所有仍在进行的任务（含聊天命令发起的）。

        幂等：任务不存在、已结束、重复取消都返回 True。只取消**未完成**的任务，
        因此已经扫码成功并写入 Cookie 的登录不会被误删。
        """
        tasks = getattr(self, "_bili_login_tasks", None) or {}
        if task_id:
            targets = [task_id]
        else:
            targets = [tid for tid, task in tasks.items() if not task.done()]

        pending: list[tuple[str, asyncio.Task]] = []
        for tid in targets:
            task = tasks.get(tid)
            state = self._bili_login_states.get(tid, {})
            if task is not None and not task.done() and state.get("state") not in {"success", "failed", "expired"}:
                task.cancel()
                pending.append((tid, task))
        if pending:
            await asyncio.gather(*(task for _, task in pending), return_exceptions=True)
        for tid, task in pending:
            # Cancelling before the coroutine starts never runs its finally block.
            if tasks.get(tid) is task:
                tasks.pop(tid, None)
                state = self._bili_login_states.get(tid)
                if state is not None and state.get("state") in {"waiting", "scanned"}:
                    state.update(state="cancelled", message="登录任务已取消")
                self._schedule_login_state_cleanup(tid)
        return True

    def bili_login_start(
        self,
        task_id: str,
        qrcode_key: str,
        *,
        notify_umo: str | None = None,
        qr_path: Path | None = None,
    ) -> None:
        """启动后台轮询任务。

        Args:
            task_id: 任务标识，聊天命令用发送者 ID，WebUI 用随机串。
            qrcode_key: ``bili_qr_create`` 返回的轮询凭据。
            notify_umo: 需要把登录结果发到聊天时填会话标识，WebUI 场景传 None。
            qr_path: 临时二维码文件，任务结束时删除（WebUI 不落盘，传 None）。
        """
        existing = self._bili_login_tasks.get(task_id)
        if existing is not None and not existing.done():
            return

        import time as _time

        self._bili_login_states[task_id] = {
            "state": "waiting",
            "message": "等待扫码",
            "username": "",
            "uid": 0,
            "started_at": _time.time(),
            "expires_in": QR_CODE_EXPIRE_TIME,
        }
        self._bili_login_tasks[task_id] = asyncio.create_task(
            self._bili_poll_qr_login(
                task_id, qrcode_key, notify_umo=notify_umo, qr_path=qr_path
            )
        )

    async def bili_cookie_status(self) -> dict[str, Any]:
        """检查当前 Cookie 是否仍有效。

        会真实请求 B站接口，仅在用户主动点「检测」时调用，不要放进总览的自动刷新里。
        """
        if not self._bili_cookie:
            return {"configured": False, "valid": False, "error": "尚未配置 Cookie"}
        result = await self._bili_check_cookie_valid()
        return {
            "configured": True,
            "valid": bool(result.get("valid")),
            "username": result.get("username", ""),
            "uid": result.get("uid", 0),
            "error": result.get("error", ""),
        }

    async def bili_logout(self) -> bool:
        """清除本地保存的 B站 Cookie（不会撤销 B站侧的登录态）。

        要清三处，少一处都会「看起来清了、实际还在用」：
        主模块的 ``bili_cookie.json``、解析器自己持久化的 ``bilibili_cookies.json``、
        以及配置项 ``BILI_CK``（否则重建解析器时又被读回来）。

        清除前先停掉仍在轮询的扫码任务：否则它可能在清除之后才判定成功，
        把刚清掉的 Cookie 又写回内存与文件（「清了又自己回来」）。
        """
        await self.bili_login_cancel()
        self._bili_cookie = ""
        # 同步「已生效的配置项值」：下面会把配置项也清空，不清这里的话
        # 下一次热更新会以为配置项「没变」而跳过（其实该走的分支已经走完了）
        self._bili_ck_applied = ""

        cleared = True
        parser = self.parsers.get("bilibili")
        if parser is not None and hasattr(parser, "clear_cookie"):
            try:
                if parser.clear_cookie() is False:
                    cleared = False
            except Exception:
                cleared = False
                logger.warning("[denia_share] 清除解析器 B站 Cookie 失败")

        pconfig = get_config()
        try:
            changed, errors = pconfig.apply_updates({"BILI_CK": ""})
            if errors or not changed and not pconfig.save():
                cleared = False
        except Exception:
            cleared = False
            logger.warning("[denia_share] 清除配置里的 B站 Cookie 失败", exc_info=True)

        try:
            self._bili_cookie_file.unlink(missing_ok=True)
            return cleared
        except OSError:
            logger.warning("[denia_share] 删除 bili_cookie.json 失败", exc_info=True)
            return False

    # ---------- 内部方法 ----------

    @staticmethod
    def _bili_headers() -> dict:
        return {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.bilibili.com/",
            "Accept": "application/json, text/plain, */*",
        }

    async def _bili_poll_qr_login(
        self,
        task_id: str,
        qrcode_key: str,
        *,
        notify_umo: str | None = None,
        qr_path: Path | None = None,
    ):
        """轮询扫码结果，成功后写入并持久化 Cookie。

        状态写进 ``self._bili_login_states``，WebUI 通过 ``bili_login_state`` 读取；
        聊天命令则靠 ``notify_umo`` 收结果。两条路径共用这一份实现。
        """
        import time as _time

        state = self._bili_login_states.get(task_id)
        if state is None:
            state = {"state": "waiting", "message": "等待扫码", "username": "", "uid": 0}
            self._bili_login_states[task_id] = state

        start = _time.time()
        try:
            while _time.time() - start < QR_CODE_EXPIRE_TIME:
                if not self._bili_http_session or self._bili_http_session.closed:
                    # 这里 break 会跳过 while 的 else（只有「时间耗尽」才走 else），
                    # 不显式置状态的话 UI 会一直显示「等待扫码」直到 300 秒回收。
                    state["state"] = "failed"
                    state["message"] = "登录会话已关闭，请重新获取二维码"
                    break
                try:
                    async with self._bili_http_session.get(
                        BILI_QR_POLL_URL, params={"qrcode_key": qrcode_key},
                        headers=self._bili_headers(), timeout=aiohttp.ClientTimeout(total=15),
                    ) as resp:
                        poll_data = await resp.json()
                        set_cookie_headers = resp.headers.getall("Set-Cookie", [])
                except (asyncio.TimeoutError, aiohttp.ClientError):
                    await asyncio.sleep(POLL_INTERVAL)
                    continue

                code = poll_data.get("data", {}).get("code", -1)
                if code == QR_CODE_EXPIRED:
                    state["state"] = "expired"
                    state["message"] = "二维码已过期，请重新获取"
                    break
                if code == QR_CODE_SCANNED:
                    state["state"] = "scanned"
                    state["message"] = "已扫码，请在手机上确认登录"
                elif code == QR_CODE_SUCCESS:
                    cookie_dict: dict[str, str] = {}
                    for header in set_cookie_headers:
                        part = header.split(";")[0].strip()
                        if "=" in part:
                            k, v = part.split("=", 1)
                            cookie_dict[k.strip()] = v.strip()
                    if not cookie_dict:
                        state["state"] = "failed"
                        state["message"] = "B站未返回 Cookie，请重试"
                        break
                    cookie_str = "; ".join(f"{k}={v}" for k, v in cookie_dict.items())
                    # 先校验候选值再提交：旧 Cookie 可能还有效，没通过校验的候选值
                    # 绝不能顶掉它。校验只拿候选值发请求，不写 self._bili_cookie。
                    result = await self._bili_check_cookie_valid(cookie_str)
                    if not result["valid"]:
                        state["state"] = "failed"
                        state["message"] = (
                            "Cookie 校验失败，已保留原登录态："
                            f"{result.get('error', '未知错误')}"
                        )
                        break
                    problem = await self._bili_commit_login_cookie(cookie_str)
                    if problem:
                        state["state"] = "failed"
                        state["message"] = problem
                        break
                    state["state"] = "success"
                    state["username"] = result.get("username", "")
                    state["uid"] = result.get("uid", 0)
                    state["message"] = "登录成功"
                    if notify_umo:
                        await self._bili_notify(
                            notify_umo,
                            f"🎉 登录成功！\n👤 用户: {result.get('username')}\n🆔 UID: {result.get('uid', 0)}",
                        )
                    break
                await asyncio.sleep(POLL_INTERVAL)
            else:
                state["state"] = "expired"
                state["message"] = "二维码已过期，请重新获取"
        except asyncio.CancelledError:
            state["state"] = "cancelled"
            state["message"] = "登录任务已取消"
            raise
        except Exception:
            logger.warning("[denia_share] B站扫码轮询未完成")
            state["state"] = "failed"
            state["message"] = "扫码登录未完成，请重新获取二维码"
        finally:
            self._bili_login_tasks.pop(task_id, None)
            # 状态不能跟着一起 pop：UI 还要读最终结果，见 _schedule_login_state_cleanup
            self._schedule_login_state_cleanup(task_id)
            if qr_path is not None:
                self._schedule_qr_cleanup(qr_path)

    def _schedule_login_state_cleanup(self, task_id: str) -> None:
        """登录任务结束后延迟回收状态，并把总量夹在上限内。

        两个理由：一是 WebUI 轮询读取，任务刚结束就删会让最后一轮拿到
        「登录任务不存在」而不是「登录成功」；二是 task_id 在 WebUI 侧是随机串，
        不清就会随点击次数无限增长（原先只 pop 了 ``_bili_login_tasks``，
        状态字典是只进不出的）。
        """
        if len(self._bili_login_states) > LOGIN_STATE_MAX:
            ordered = sorted(
                self._bili_login_states.items(),
                key=lambda kv: kv[1].get("started_at") or 0.0,
            )
            for old_id, _ in ordered[: len(ordered) - LOGIN_STATE_MAX]:
                # old_id == task_id 是「正在收尾的这条」：调用方在 finally 里已经先把
                # 本任务从 _bili_login_tasks 里 pop 掉了，所以下面那条豁免对它无效 ——
                # 一旦它恰好是最旧的一条，最终状态会被自己这次清理删掉，UI 再也读不到
                # 「登录成功」。所以本任务要单独豁免。
                if old_id == task_id or old_id in self._bili_login_tasks:
                    continue  # 本任务与还在跑的任务都不能丢，UI 要读它的进度
                self._bili_login_states.pop(old_id, None)

        async def _delayed_drop() -> None:
            try:
                await asyncio.sleep(LOGIN_STATE_TTL)
                self._bili_login_states.pop(task_id, None)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("回收扫码登录状态失败", exc_info=True)

        self._delayed_cleanup_tasks = [
            task for task in self._delayed_cleanup_tasks if not task.done()
        ]
        self._delayed_cleanup_tasks.append(asyncio.create_task(_delayed_drop()))

    def _schedule_qr_cleanup(self, qr_path: Path, delay: int = 120) -> None:
        """延迟删除二维码临时文件。

        二维码图片已经 yield 进消息管线，真正读取它的时刻在发送阶段，
        轮询一结束就删会与发送竞争、导致图片发不出去；所以推迟到
        ``delay`` 秒后再清理。顺带回收已完成的旧任务，避免列表无限增长。
        """
        async def _delayed_unlink() -> None:
            try:
                await asyncio.sleep(delay)
                qr_path.unlink(missing_ok=True)
            except Exception:
                logger.debug("清理二维码临时文件失败", exc_info=True)

        self._delayed_cleanup_tasks = [
            task for task in self._delayed_cleanup_tasks if not task.done()
        ]
        self._delayed_cleanup_tasks.append(asyncio.create_task(_delayed_unlink()))

    def _bili_apply_cookie_to_parser(self, cookie_str: str):
        parser = self.parsers.get("bilibili")
        if parser and hasattr(parser, "update_cookie"):
            parser.update_cookie(cookie_str)
            logger.info("B站 Cookie 已应用到解析器")

    async def _bili_check_cookie_valid(self, cookie_str: str | None = None) -> dict:
        cookie = self._bili_cookie if cookie_str is None else cookie_str
        if not cookie or not self._bili_http_session:
            return {"valid": False, "error": "Cookie 为空或会话未初始化"}
        headers = {**self._bili_headers(), "Cookie": cookie}
        try:
            async with self._bili_http_session.get(
                "https://api.bilibili.com/x/web-interface/nav",
                headers=headers, timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                data = await resp.json()
            if data.get("code") == 0 and data.get("data", {}).get("isLogin"):
                u = data["data"]
                return {"valid": True, "username": u.get("uname", ""), "uid": u.get("mid", 0)}
            return {"valid": False, "error": "登录态已失效或验证未通过"}
        except Exception:
            return {"valid": False, "error": "验证暂时失败，请稍后重试"}

    async def _bili_load_cookie(self):
        # **配置项优先**：BILI_CK 是用户显式填的，持久化文件只是扫码登录的暂存。
        # 取值顺序要和 apply_runtime_config 与 parser._init_credential 保持一致，
        # 否则「网页上改了 BILI_CK」还是会被旧登录态压住。
        configured = str(get_config().BILI_CK or "")
        # 记下启动时生效的配置项值：apply_runtime_config 靠它判断「到底变没变」
        self._bili_ck_applied = configured
        if configured:
            self._bili_cookie = configured
            return
        try:
            if self._bili_cookie_file.exists():
                saved = json.loads(self._bili_cookie_file.read_text(encoding="utf-8")).get("cookie", "")
                if saved:
                    self._bili_cookie = saved
                    logger.info("已从持久化文件加载B站 Cookie")
        except Exception as e:
            logger.warning(f"加载B站 Cookie 失败: {e}")

    async def _bili_commit_login_cookie(self, cookie_str: str) -> str:
        """Commit a verified QR credential; return a public error on failure."""
        pconfig = get_config()
        old_cookie = str(pconfig.BILI_CK or "")
        staged = self._bili_cookie_file.with_name(
            f".{self._bili_cookie_file.name}.{time.time_ns()}.tmp"
        )
        config_attempted = False
        try:
            with staged.open("x", encoding="utf-8") as stream:
                json.dump({"cookie": cookie_str}, stream, ensure_ascii=False)
            config_attempted = True
            changed, errors = pconfig.apply_updates({"BILI_CK": cookie_str})
            if errors or not changed and not pconfig.save():
                raise OSError("configuration save failed")
            staged.replace(self._bili_cookie_file)
        except Exception:
            if config_attempted:
                try:
                    changed, errors = pconfig.apply_updates({"BILI_CK": old_cookie})
                    if errors or not changed and not pconfig.save():
                        logger.warning("[denia_share] B站登录配置回滚未能持久保存")
                except Exception:
                    logger.warning("[denia_share] B站登录配置回滚未完成")
            return "登录凭据未能完整保存，请检查配置和存储后重试"
        finally:
            try:
                staged.unlink(missing_ok=True)
            except OSError:
                logger.warning("[denia_share] B站登录临时文件未能清理")
        self._bili_cookie = cookie_str
        self._bili_ck_applied = cookie_str
        self._bili_apply_cookie_to_parser(cookie_str)
        return ""

    async def _bili_notify(self, session: str, message: str):
        try:
            # session 期望是 unified_msg_origin（平台:消息类型:会话ID 三段式）；
            # 对历史调用方只传裸 QQ 号的情况做兼容兜底
            umo = session if ":" in session else f"default:FriendMessage:{session}"
            await self.context.send_message(umo, MessageChain().message(message))
        except Exception as e:
            logger.error(f"发送消息到 {session} 失败: {e}")

    # ==================== JSON 卡片工具 ====================

    @staticmethod
    def _has_json_component(event: AstrMessageEvent) -> bool:
        if not hasattr(event, "message_obj") or not hasattr(event.message_obj, "message"):
            return False
        for c in event.message_obj.message:
            if isinstance(c, Comp.Json):
                return True
            t = c.get("type") if isinstance(c, dict) else getattr(c, "type", None)
            if t and t != "reply" and "json" in str(t).lower():
                return True
        return False

    def _extract_links_from_event(self, event: AstrMessageEvent) -> list[str]:
        links: list[str] = []
        if hasattr(event, "message_obj") and hasattr(event.message_obj, "message"):
            for c in event.message_obj.message:
                if isinstance(c, Comp.Json):
                    links.extend(self._extract_links_from_json(c.data))
                elif isinstance(c, Comp.Plain):
                    links.extend(URL_PATTERN.findall(c.text or ""))
                elif isinstance(c, dict) and "json" in str(c.get("type", "")).lower():
                    links.extend(self._extract_links_from_json(c.get("data", c)))
        links.extend(URL_PATTERN.findall(event.message_str or ""))
        return links

    @staticmethod
    def _extract_links_from_json(data) -> list[str]:
        links: list[str] = []
        try:
            if isinstance(data, str):
                data = json.loads(data)

            def search(obj):
                found = []
                if isinstance(obj, dict):
                    meta = obj.get("meta") or {}
                    if isinstance(meta, dict):
                        for dk in ("detail_1", "detail", "news", "music"):
                            d = meta.get(dk) or {}
                            if isinstance(d, dict):
                                for uk in ("qqdocurl", "url", "jumpUrl"):
                                    v = d.get(uk)
                                    if isinstance(v, str) and v:
                                        found.append(v)
                    for v in obj.values():
                        if isinstance(v, str) and v.startswith(("http://", "https://")):
                            found.append(v)
                        elif isinstance(v, (dict, list)):
                            found.extend(search(v))
                elif isinstance(obj, list):
                    for item in obj:
                        found.extend(search(item))
                return found

            links.extend(search(data))
        except Exception as e:
            logger.warning(f"解析 JSON 消息组件失败: {e}")
        return links

    # ==================== 管理命令 ====================

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("denia_status")
    async def denia_status(self, event: AstrMessageEvent):
        """查看希望解析器的运行状态"""
        from . import __version__

        platforms = "、".join(self.parsers) if self.parsers else "（无）"
        backend = self.screenshot.backend
        shot_ready = "已就绪" if self.screenshot.is_configured else "未配置"
        yield event.plain_result(
            f"希望解析器 v{__version__}\n"
            f"已启用平台：{platforms}\n"
            f"卡片渲染：{'开' if self._renderer.enabled else '关'}\n"
            f"B站 Cookie：{'已配置' if self._bili_cookie else '未配置'}\n"
            f"截图后端：{backend}（{shot_ready}）"
            f"{'，链接兜底开' if self._screenshot_fallback else ''}\n"
            f"错误提示：{'开' if self._send_errors else '关'}"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("denia_clear")
    async def denia_clear_cache(self, event: AstrMessageEvent):
        """立即清空解析缓存（含渲染卡片与已下载媒体）"""
        from .core.media_utils import clear_cache_dir

        try:
            cleaned = await clear_cache_dir(self.cache_dir)
        except Exception as e:
            yield event.plain_result(f"❌ 清空缓存失败: {str(e)[:120]}")
            return
        self._result_cache.clear()
        self._render_cache.clear()
        self._link_dedup.clear()
        yield event.plain_result(f"🧹 已清空 {cleaned} 个缓存文件")

    async def terminate(self):
        cookiecloud = getattr(self, '_cookiecloud_sync', None)
        if cookiecloud is not None:
            await cookiecloud.close()
        music_controller = getattr(self, "_music_controller", None)
        if music_controller is not None:
            await music_controller.close()
        if self._cache_cleanup_task is not None and not self._cache_cleanup_task.done():
            self._cache_cleanup_task.cancel()
            try:
                await self._cache_cleanup_task
            except asyncio.CancelledError:
                pass
        await self.downloader.aclose()
        if self._proxy_downloader is not None:
            await self._proxy_downloader.aclose()
            self._proxy_downloader = None

        for task in list(self._bili_login_tasks.values()):
            if not task.done():
                task.cancel()
        self._bili_login_tasks.clear()

        for task in list(self._delayed_cleanup_tasks):
            if not task.done():
                task.cancel()
        self._delayed_cleanup_tasks.clear()

        if self._bili_http_session and not self._bili_http_session.closed:
            await self._bili_http_session.close()
