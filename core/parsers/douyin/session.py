# 抖音分享页会话：cookie 保持 + 重试 + WAF 挑战处理。
#
# 背景
# ----
# 抖音分享页（www.iesdouyin.com/share/<ty>/<id>）只在请求**带有效 ttwid cookie**
# 时才把作品数据放进 window._ROUTER_DATA；不带 ttwid 的冷请求只返回页面上下文
# （ua / host / itemId 之类），没有 videoInfoRes。
#
# 实测（2026-09）：
#   全新 client、不带 cookie、只发一次  -> no-data  6/6 失败
#   冷请求                              -> no-data，但 Set-Cookie 带回 ttwid
#   全新 client + 只带那个 ttwid        -> HIT(1)   4/4 成功
#   全新 client + 假 ttwid=fake123      -> no-data  3/3（必须有效）
#   同一个 client 连发 6 次             -> 第 4 次才拿到 ttwid，第 5 次才出数据
#
# 也就是说：**冷请求只负责拿 ttwid，带着它再请求才有数据。**
# 连接池复用不影响，User-Agent 也不影响，只有 Cookie 头是开关。
#
# 所以本模块做两件事：
#   1. 把 cookie 跨请求存下来（会话按解析器类缓存，跨消息存活）
#   2. 「没拿到作品数据」的响应要重试，而不是直接当失败

import asyncio
import base64
import hashlib
import json
import re
import time
from http.cookies import SimpleCookie

from httpx import AsyncClient

from astrbot.api import logger

# 本模块自建 AsyncClient，且 client_kwargs 是**外部注入**的（不 import
# base_parser），所以兜底必须自己 import 一次 —— 否则它自己不保证环境已就绪。
from ... import http_env  # noqa: F401  (import for side effect)

ROUTER_RE = re.compile(r"window\._ROUTER_DATA\s*=\s*(.*?)</script>", re.DOTALL)
CHALLENGE_RE = re.compile(r'cs="([A-Za-z0-9+/=]+)"')

# 请求过密时抖音会返回一个 JS 挑战页（正文含 waf_js）
WAF_MARK = "waf_js"

# 承载作品数据的字段名。video 页是 videoInfoRes，图文页是 slidesInfoRes
# 或 noteDetailRes，三个都认。
WORK_KEYS = ("videoInfoRes", "slidesInfoRes", "noteDetailRes")

MAX_ATTEMPTS = 6
MAX_POW_ITER = 1_000_000

# 单条作品的请求总预算（跨 URL 变体共享）。
#
# 为什么需要：`_parse_douyin` 最多试 4 个 URL（slides 时 2 种 html_type × 2 个域名），
# 每个 URL 内部 `fetch` 最多 MAX_ATTEMPTS(6) 次，cookie 失效时还要清空冷请求再来 6 次
# —— 最坏 4 × 12 = **48 次 GET**。而本仓自己的经验是「不要连发探测，否则出口 IP 会被
# 打进 __ac_nonce 限流页，之后怎么重试都没用」，48 次正是最容易触发限流的形状。
#
# 12 次足够覆盖正常路径（冷请求拿 ttwid + 热请求拿数据，通常 2~3 次）。
# 预算耗尽说明本机多半已经被限流，继续连发只会更糟 —— 那时应当把机会留给下一条消息。
MAX_REQUESTS_PER_WORK = 12

# 分享页会话（ttwid）的最大存活秒数，超时即整体换一份。
#
# cookie 是**跨消息**存活的，而 ttwid 有寿命。失效之后 fetch() 虽然有一条
# 「清空 cookie、冷请求一次」的自愈路径，但那条路要花请求预算；预算若已被
# 本条作品的前几个 URL 变体吃掉，自愈就走不动，只能干等插件重载 —— 表现为
# 「抖音忽然解析失败，重载一下又好了」。给它一个年龄上限，让旧凭证在烂掉
# 之前主动退场，比等它失效再去抢救省事得多。
# 3600 = 一小时；想更频繁就调小。
SESSION_MAX_AGE = 3600


def _b64d(value: str) -> bytes:
    # 抖音的 cs 字段是不带 padding 的 base64，直接 b64decode 会报 Incorrect padding
    return base64.b64decode(value + "=" * (-len(value) % 4))


def _b64e(value: bytes) -> str:
    return base64.b64encode(value).decode()


def router_data(text: str) -> dict | None:
    """取出 window._ROUTER_DATA 的 JSON；取不到返回 None。"""
    matched = ROUTER_RE.search(text or "")
    if not matched:
        return None
    try:
        data = json.loads(matched.group(1).strip())
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def has_work_data(text: str) -> bool:
    """_ROUTER_DATA 里是否真的带了作品数据（而不是只有页面上下文）。"""
    data = router_data(text)
    if data is None:
        return False
    for value in (data.get("loaderData") or {}).values():
        if not isinstance(value, dict):
            continue
        for key in WORK_KEYS:
            info = value.get(key)
            if isinstance(info, dict) and info.get("item_list"):
                return True
    return False


def looks_like_challenge(text: str) -> bool:
    """是不是被 WAF 挑战页拦了。"""
    return WAF_MARK in (text or "")


def solve_challenge(text: str) -> str | None:
    """解开 waf_js 的 PoW，返回 _wafchallengeid 的 cookie 值；解不开返回 None。

    挑战页里的 cs 是个 base64 的 JSON：
        {"v": {"a": <prefix 原始字节>, "c": <目标 sha256>}, ...}
    找 i 使 sha256(prefix + str(i)) == target 即可。实测 i 通常是个位数。
    """
    matched = CHALLENGE_RE.search(text or "")
    if not matched:
        return None
    try:
        payload = json.loads(_b64d(matched.group(1)))
        prefix = _b64d(payload["v"]["a"])
        target = _b64d(payload["v"]["c"]).hex()
    except Exception:
        return None

    for index in range(MAX_POW_ITER):
        if hashlib.sha256(prefix + str(index).encode()).hexdigest() == target:
            payload["d"] = _b64e(str(index).encode())
            return _b64e(json.dumps(payload, separators=(",", ":")).encode())
    return None


class DouyinShareSession:
    """跨请求保持 cookie 的分享页会话。"""

    def __init__(self, client_kwargs):
        # client_kwargs 是 BaseParser.client_kwargs（classmethod），
        # 这样代理 / 超时 / 证书校验这些配置照样生效
        self._client_kwargs = client_kwargs
        self._cookies: dict[str, str] = {}
        self._lock = asyncio.Lock()
        # 单条作品的请求预算，由解析器在开始解析时重置（见 start_work）
        self._requests_left = MAX_REQUESTS_PER_WORK
        # 会话创建时刻（单调时钟），供 expired() 判定
        self._born = time.monotonic()

    def expired(self) -> bool:
        """会话年龄是否已越过 SESSION_MAX_AGE，该换一份新 cookie 了。"""
        return time.monotonic() - self._born >= SESSION_MAX_AGE

    def start_work(self) -> None:
        """开始解析一条新作品：重置请求预算。

        预算跨 URL 变体共享 —— 那正是要防的东西（4 个 URL 各自重试一轮就是 48 次）。
        """
        self._requests_left = MAX_REQUESTS_PER_WORK

    def _spend_request(self) -> bool:
        """消耗一次请求预算；返回 False 表示已用尽，调用方应停止重试。"""
        if self._requests_left <= 0:
            return False
        self._requests_left -= 1
        return True

    def cookie_header(self) -> dict[str, str]:
        if not self._cookies:
            return {}
        joined = "; ".join(f"{name}={value}" for name, value in self._cookies.items())
        return {"Cookie": joined}

    def remember(self, response) -> None:
        try:
            raw = response.headers.get_list("set-cookie")
        except Exception:
            return
        for header in raw:
            jar = SimpleCookie()
            try:
                jar.load(header)
            except Exception:
                continue
            for name, morsel in jar.items():
                if morsel.value:
                    self._cookies[name] = morsel.value

    async def fetch(self, url: str, headers: dict[str, str]) -> str:
        """抓分享页，保持 cookie，并重试到真的拿到作品数据。

        第一发通常只拿回 ttwid（页面里没有作品数据），第二发带上去才有。
        """
        async with self._lock:
            text = await self._fetch_with_retries(url, headers)
            if has_work_data(text) or not self._cookies:
                return text

            # 重试完还是没有作品数据，而且我们**手里带着 cookie**：
            # 最可能是缓存的 ttwid 已失效。而带着失效 cookie 时服务端不会重新下发
            # （冷请求才会），所以必须清空 cookie 再冷来一次，否则会永久卡死。
            self._cookies.clear()
            logger.debug("[douyin] 带 cookie 重试仍未取到作品数据，清空会话冷请求一次")
            return await self._fetch_with_retries(url, headers)

    async def _fetch_with_retries(self, url: str, headers: dict[str, str]) -> str:
        async with AsyncClient(
            **self._client_kwargs(headers=headers, follow_redirects=True)
        ) as client:
            text = ""
            solved = False
            for attempt in range(MAX_ATTEMPTS):
                if not self._spend_request():
                    # 预算用尽：把已经拿到的内容交回去，让上层决定（换路径或报错）。
                    # 继续连发探测只会把出口 IP 打进 __ac_nonce 限流页，
                    # 那之后就怎么重试都没用了 —— 见 MAX_REQUESTS_PER_WORK 的说明。
                    logger.warning(
                        f"[douyin] 本条作品的请求预算已用尽"
                        f"（{MAX_REQUESTS_PER_WORK} 次），停止重试"
                    )
                    return text
                try:
                    response = await client.get(url, headers=self.cookie_header())
                except Exception:
                    await asyncio.sleep(self._backoff(attempt))
                    continue

                self.remember(response)
                text = response.text or ""

                if has_work_data(text):
                    return text

                # 挑战只解一次：解完还拿不到数据说明不是挑战的问题（多半是限流），
                # 再解就是白烧 CPU（每次最多 MAX_POW_ITER 次 sha256）
                if not solved and looks_like_challenge(text):
                    solved = True
                    # PoW 是同步的 CPU 活儿，直接跑会把整个事件循环按住，丢线程池
                    challenge = await asyncio.to_thread(solve_challenge, text)
                    if challenge:
                        self._cookies["_wafchallengeid"] = challenge
                    else:
                        # 挑战解不开，重试没有意义，直接把页面交回去让上层报错
                        return text

                await asyncio.sleep(self._backoff(attempt))
            return text

    @staticmethod
    def _backoff(attempt: int) -> float:
        return 0.3 + 0.2 * attempt


_SESSIONS: dict[type, DouyinShareSession] = {}


def get_share_session(parser_cls) -> DouyinShareSession:
    """按解析器类缓存会话，让 cookie 跨消息存活。"""
    session = _SESSIONS.get(parser_cls)
    if session is not None and session.expired():
        # 旧会话只有 cookie，没有长连接（client 是每次 fetch 现开的），
        # 直接丢掉即可，不需要关闭动作。
        logger.debug("[douyin] 分享页会话已超时，重建并重新取得 ttwid")
        session = None
    if session is None:
        session = DouyinShareSession(parser_cls.client_kwargs)
        _SESSIONS[parser_cls] = session
    return session
