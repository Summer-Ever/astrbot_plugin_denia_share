"""QQ 官方机器人的「分享卡片」文本 → 结构化字段。

**为什么需要这一层**

上一版 ``card_links.py`` 处理的是 OneBot（NapCat / LLOneBot / Lagrange）把卡片
塞进 ``json`` / ``xml`` / ``ark`` 消息段的情况。但 QQ 官方机器人（``qq_official``
平台，走 QQ 机器人开放接口）**根本不下发消息段**：它把卡片直接渲染成一段纯文本
放进 ``content`` 里，插件拿到的 ``message_str`` 就是::

    [卡片消息] 小程序
    摘要: [QQ小程序]《鸣潮》共鸣者战斗演示 | 莉莉
    source_logo: https://open.gtimg.cn/open/app_icon/00/95/17/76/100951776_100_m.png
    title: 《鸣潮》共鸣者战斗演示 | 莉莉
    preview: https://qq.ugcimg.cn/v1/...
    source: 哔哩哔哩

没有 json 段、没有 xml、没有任何 ``b23.tv`` 链接 —— 所以 ``has_card()`` 为假、
``extract_links()`` 只能抽到 ``source_logo`` 和 ``preview`` 两张**图片**链接，
插件对这张卡片必然零反应。

**这里只做一件事**：把这段文本还原成字段。至于「没有链接怎么办」，由调用方
决定（本插件当前的做法是拿 ``title`` 去 B站按标题反查，见
``core/parsers/bilibili/parser.py::search_video_url``）。

**三条硬约束**

1. 纯函数：无 I/O、不联网、不抛异常。输入是平台渲染的文本，形态不可控。
2. 不改写内容：``title`` 原样返回；只在取「摘要」时剥掉 QQ 加的方括号前缀
   （``[QQ小程序]`` / ``[分享帖子]``），因为那不是标题的一部分。
3. 链接只认字面 ``http(s)``，且**导航链接与图片链接分开**：``preview`` /
   ``source_logo`` 是封面图，绝不能当成「卡片里已经有链接了」。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# 卡片文本的起始标记。官方机器人用它把「这张不是普通聊天」标出来。
CARD_TEXT_MARKER = "[卡片消息]"

# 一行字段：``key: value``。key 可能是中文（``摘要``）也可能是英文
# （``source_logo``），所以两种冒号都收，key 长度限制防住「标题里带冒号」
# 被误判成字段名。
_FIELD_RE = re.compile(r"^(?P<key>[^\s:：]{1,16})\s*[:：]\s*(?P<value>.*)$")

# ``[QQ小程序]`` / ``[分享帖子]`` / ``[分享视频]`` —— QQ 加在摘要前面的来源标记。
_SUMMARY_PREFIX_RE = re.compile(r"^\s*\[[^\]]{1,20}\]\s*")

# 卡片里表示「点进去会到哪」的字段。``jump_url`` 是腾讯频道图文卡片用的，
# ``qqdocurl`` 是小程序卡片用的，其余是各版本出现过的写法。
_NAV_KEYS: tuple[str, ...] = (
    "jump_url",
    "jumpurl",
    "qqdocurl",
    "share_url",
    "shareurl",
    "url",
    "link",
    "href",
    "origin_url",
    "originurl",
)

# 封面 / 图标 / 二维码：是图片，不是分享目标。用来把「卡片自带链接」这个判断
# 限制在真正的导航链接上。
_IMAGE_KEYS: tuple[str, ...] = (
    "preview",
    "source_logo",
    "icon",
    "avatar",
    "cover",
    "image",
    "qrcode",
    "qr_code",
)

# 平台名字段 → 解析器键。``source`` 是卡片自报的来源（``哔哩哔哩`` /
# ``抖音`` ……），比标题里的关键词可靠得多，所以优先用它。
_SOURCE_PLATFORMS: tuple[tuple[str, str], ...] = (
    ("哔哩哔哩", "bilibili"),
    ("哔哩", "bilibili"),
    ("bilibili", "bilibili"),
    ("b站", "bilibili"),
    ("抖音", "douyin"),
    ("douyin", "douyin"),
    ("快手", "kuaishou"),
    ("kuaishou", "kuaishou"),
    ("微博", "weibo"),
    ("weibo", "weibo"),
    ("小红书", "xiaohongshu"),
    ("xiaohongshu", "xiaohongshu"),
    ("twitter", "twitter"),
    ("nga", "nga"),
    ("acfun", "acfun"),
    ("pixiv", "pixiv"),
    ("steam", "steam"),
    ("网易云", "netease"),
    ("qq音乐", "qqmusic"),
    ("小黑盒", "xiaoheihe"),
    ("github", "github"),
)

_URL_RE = re.compile(r"https?://[^\s'\"<>\\\x80-\U0010ffff]+")


def looks_like_card_text(text: str | None) -> bool:
    """这段文本是不是官方机器人渲染的卡片（只看标记，不解析）。"""
    return bool(text) and CARD_TEXT_MARKER in str(text)


@dataclass(frozen=True)
class ShareCard:
    """一张被渲染成文本的分享卡片。

    ``kind`` 是标记后面那个词（``小程序`` / ``图文卡片`` / ``大图卡片`` ……），
    仅用于日志与排查；``fields`` 是原样的 ``key → value``，不丢任何字段。
    """

    kind: str
    fields: dict[str, str] = field(default_factory=dict)

    # ---------------------------------------------------------------- 字段

    def _get(self, *keys: str) -> str:
        for key in keys:
            value = self.fields.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    @property
    def title(self) -> str:
        """卡片标题。

        优先 ``title`` 字段。没有时退回 ``摘要`` 并剥掉 ``[QQ小程序]`` 这类
        前缀 —— 官方卡片两者通常内容一致，但摘要带前缀、title 不带。
        """
        title = self._get("title")
        if title:
            return title
        summary = self._get("摘要", "summary", "digest")
        return _SUMMARY_PREFIX_RE.sub("", summary).strip()

    @property
    def summary(self) -> str:
        return _SUMMARY_PREFIX_RE.sub("", self._get("摘要", "summary", "digest")).strip()

    @property
    def source(self) -> str:
        """卡片自报的来源，例如 ``哔哩哔哩``。"""
        return self._get("source", "tag")

    @property
    def platform(self) -> str:
        """来源对应的解析器键；认不出返回空串。

        只做「整串包含」匹配，不做模糊推断 —— 猜错平台比不解析更糟。
        """
        source = self.source.strip().lower()
        if not source:
            return ""
        for needle, platform in _SOURCE_PLATFORMS:
            if needle in source:
                return platform
        return ""

    @property
    def links(self) -> list[str]:
        """卡片里的**导航**链接（点进去会到哪），不含封面图。"""
        found: list[str] = []
        for key in _NAV_KEYS:
            value = self.fields.get(key)
            if not isinstance(value, str):
                continue
            for match in _URL_RE.findall(value):
                if match not in found:
                    found.append(match)
        return found

    @property
    def image_links(self) -> list[str]:
        """封面 / 图标这类图片链接，仅供展示与排查。"""
        found: list[str] = []
        for key in _IMAGE_KEYS:
            value = self.fields.get(key)
            if not isinstance(value, str):
                continue
            for match in _URL_RE.findall(value):
                if match not in found:
                    found.append(match)
        return found

    def detail_lines(self) -> list[str]:
        """排查用的一行行文本（日志 / ``/denia_card``）。"""
        lines = [
            f"卡片类型: {self.kind or '（未知）'}",
            f"标题: {self.title[:80] or '（无）'}",
            f"来源: {self.source or '（无）'} → 平台: {self.platform or '（未识别）'}",
        ]
        if self.links:
            lines.append("导航链接: " + "、".join(self.links[:3]))
        else:
            lines.append("导航链接: （无，卡片只带标题）")
        if self.image_links:
            lines.append(f"图片链接: {len(self.image_links)} 个")
        return lines


def parse_card_text(text: str | None) -> ShareCard | None:
    """把卡片文本解析成 :class:`ShareCard`；不是卡片就返回 ``None``。

    解析规则只有两条：
    - 第一处出现 ``[卡片消息]`` 的行是标题行，标记后面那截是 ``kind``；
    - 之后每行按 ``key: value`` 拆；拆不出来的行拼回上一行（长标题会折行）。
    """
    if not looks_like_card_text(text):
        return None
    lines = str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n")

    kind = ""
    fields: dict[str, str] = {}
    last_key = ""
    started = False
    for line in lines:
        if not started:
            index = line.find(CARD_TEXT_MARKER)
            if index < 0:
                continue
            kind = line[index + len(CARD_TEXT_MARKER) :].strip()
            started = True
            continue

        stripped = line.strip()
        if not stripped:
            continue
        match = _FIELD_RE.match(stripped)
        if match:
            last_key = match.group("key").strip()
            fields[last_key] = match.group("value").strip()
        elif last_key:
            fields[last_key] = f"{fields[last_key]}\n{stripped}".strip()

    if not started:
        return None
    return ShareCard(kind=kind, fields=fields)


def _candidate_texts(event: object) -> list[str]:
    """这条事件里可能藏着卡片文本的所有字符串。

    三个位置都要看：``message_str``（官方机器人）、``Comp.Plain.text``
    （部分适配器把正文放在组件上）、以及 ``Comp.Reply`` 的引用内容 ——
    用户「引用一张卡片再发指令」时，卡片在引用的 chain 里，不在本条消息里。
    """
    texts: list[str] = []
    message_str = getattr(event, "message_str", "") or ""
    if isinstance(message_str, str) and message_str:
        texts.append(message_str)

    message_obj = getattr(event, "message_obj", None)
    components = getattr(message_obj, "message", None) if message_obj else None
    for comp in components or []:
        plain = getattr(comp, "text", None)
        if isinstance(plain, str) and plain:
            texts.append(plain)
        quoted = getattr(comp, "message_str", None)
        if isinstance(quoted, str) and quoted:
            texts.append(quoted)
        for item in getattr(comp, "chain", None) or []:
            item_text = getattr(item, "text", None)
            if isinstance(item_text, str) and item_text:
                texts.append(item_text)
    return texts


def card_of(event: object) -> ShareCard | None:
    """从事件里取卡片；没有就返回 ``None``。

    结构化卡片段（``json`` / ``xml``）不走这里 —— 那条路在 ``card_links.py``。
    这里只认官方机器人渲染的文本形态，两者互不干扰。
    """
    for text in _candidate_texts(event):
        card = parse_card_text(text)
        if card is not None:
            return card
    return None
