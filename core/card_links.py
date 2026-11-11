"""QQ 分享卡片 → 链接提取（json / xml / ark / 纯文本 CQ 码）。

**为什么需要这一层**

插件原来的卡片入口只认 ``Comp.Json``，而 AstrBot 的 aiocqhttp 适配器只把 OneBot
的 ``json`` 段映射成组件（``ComponentTypes`` 里有 ``json``，**没有** ``xml`` /
``ark`` / ``light_app``）。协议端（NapCat / LLOneBot / Lagrange）把 B站分享卡片
发成 xml 或 ark 时，那一段会在适配器里被整段丢弃，只留一条「不支持的消息段类型，
已忽略」的 warning；插件看到的是一条**空消息** —— ``message_str == ""``、
``message == []``。于是「发文本链接能解析、发卡片零反应」。

所以这里不再依赖组件模型，直接读 ``event.message_obj.raw_message`` 里的原始
OneBot 段。原始段一定还在（适配器只是没把它转成组件，没从原始事件里删掉）。

**三条硬约束**

1. 纯函数：无 I/O、不联网、不抛异常。输入来自协议端，形态完全不可控
   （``data`` 可能是 dict、可能是 JSON 字符串、XML 还带 HTML 实体）。
2. 不猜内容：只认 ``http(s)://`` 开头的字面链接。卡片里没有链接就返回空，
   由调用方决定是静默还是提示 —— 绝不拿标题去猜一个视频。
3. 保序去重：``detail_1.qqdocurl`` 与 ``detail_1.url`` 常是同一视频的短链 + 长链，
   这里按「已知分享字段优先」的顺序返回，最终顺序由调用方按解析器认领结果重排。
"""

from __future__ import annotations

import html
import json
import re
from typing import Any, Iterable

# 卡片里表示「分享目标」的字段，顺序即猜测的优先级。同名键在 X / 各家小程序
# 卡片里都出现过；不在这里的字段仍会被扫到，只是排在后面。
_PREFERRED_KEYS: tuple[str, ...] = (
    "qqdocurl",
    "jumpUrl",
    "jump_url",
    "shareUrl",
    "share_url",
    "sourceUrl",
    "source_url",
    "originUrl",
    "origin_url",
    "webUrl",
    "web_url",
    "url",
    "link",
    "href",
)

# 结构化卡片段。``json`` 会被 AstrBot 转成 ``Comp.Json``；其余四种不会，
# 只能从原始段里读 —— 这就是本模块存在的理由。
CARD_SEGMENT_TYPES: frozenset[str] = frozenset(
    {"json", "xml", "ark", "light_app", "miniapp", "miniappcard", "share"}
)

# 只吃 **ASCII 可见字符**。卡片里的链接是机器生成的，一定全是 ASCII；而
# ``[^\s]`` 会把「看这个 https://b23.tv/xxx，挺好」里的中文和标点一起吞进来
# （main.py 里 URL_PATTERN 的老毛病）。反斜杠也要排除：JSON 里的 ``\"``
# 会把 URL 一路吞到行尾。
_URL_RE = re.compile(r"https?://[^\s'\"<>\\\x80-\U0010ffff]+")

# CQ 码形态（post_format=string 的协议端）。正常路径下 AstrBot 会先把 message
# 转成数组，用不到这几条；留作兜底，代价为零。
_CQ_CODE_RE = re.compile(r"\[CQ:(json|xml),data=([^\]]*)\]", re.S)

# XML 卡片里链接既可能是文本节点，也可能是 ``url="..."`` 属性。
_XML_ATTR_RE = re.compile(r"""\b(?:url|src|href|qqdocurl)\s*=\s*["']([^"']+)["']""", re.I)

# URL 里合法的 ASCII 之后常跟中文标点（「看这个 https://b23.tv/xxx，挺好」），
# 正则的 ``[^\s]`` 会把标点和汉字一起吞进去。卡片链接是机器生成的，末尾不会
# 有这些字符，所以直接裁掉。
_TRAILING_NOISE = "，。、；：！？）】》〉」』”’,.;:!?)]}>\"'"

_JSON_MAX_DEPTH = 8


# ---------------------------------------------------------------- 原始段读取


def _segment_type(seg: Any) -> str:
    """取段的类型名，统一成小写字符串。

    OneBot 原始段是 ``{"type": "xml", ...}``；AstrBot 组件是 pydantic 对象，
    ``type`` 是 ``ComponentType`` 枚举（``str()`` 会得到 ``ComponentType.Json``），
    所以要取 ``.value``。
    """
    if isinstance(seg, dict):
        raw = seg.get("type") or seg.get("msg_type") or ""
    else:
        raw = getattr(seg, "type", "")
    raw = getattr(raw, "value", raw)
    return str(raw or "").lower()


def _segment_data(seg: Any) -> Any:
    if isinstance(seg, dict):
        return seg.get("data", seg)
    return getattr(seg, "data", None)


def raw_segments(event: Any) -> list[Any]:
    """原始 OneBot 消息段列表；拿不到就是空列表。

    三种形态都要认：
    - ``aiocqhttp.Event`` / dict：``raw["message"]`` 是段数组
    - 少数适配器：``raw`` 本身就是段数组
    - ``post_format=string``：``raw["message"]`` 是一整串 CQ 码
    """
    obj = getattr(event, "message_obj", None)
    raw = getattr(obj, "raw_message", None)
    if raw is None:
        return []
    message = None
    if isinstance(raw, dict):
        message = raw.get("message")
    else:
        message = getattr(raw, "message", None)
    if message is None and isinstance(raw, (list, tuple)):
        message = raw
    if isinstance(message, list):
        return message
    return []


def raw_message_text(event: Any) -> str:
    """``post_format=string`` 协议端给的整串 CQ 码；拿不到就是空串。"""
    obj = getattr(event, "message_obj", None)
    raw = getattr(obj, "raw_message", None)
    if isinstance(raw, str):
        return raw
    message = raw.get("message") if isinstance(raw, dict) else getattr(raw, "message", None)
    return message if isinstance(message, str) else ""


def components(event: Any) -> list[Any]:
    """AstrBot 组件列表（``Comp.*``），拿不到就是空列表。"""
    obj = getattr(event, "message_obj", None)
    message = getattr(obj, "message", None) if obj else None
    return list(message) if isinstance(message, list) else []


# ---------------------------------------------------------------- 链接抽取


def _clean(url: str) -> str:
    """裁掉链接尾部粘上来的东西，并确认它真的是 http(s) 链接。

    卡片里的链接一定全是 ASCII；出现非 ASCII（中文、全角标点）就说明
    正则把后面的正文吞进来了 —— 在那里截断，而不是指望 strip 能剥干净。
    """
    url = str(url or "").strip()
    for index, char in enumerate(url):
        if ord(char) > 126 or char in "<>\"'\\ ":
            url = url[:index]
            break
    url = url.strip(_TRAILING_NOISE)
    if not url.startswith(("http://", "https://")):
        return ""
    return url if len(url) > len("https://") + 2 else ""


def walk_json(obj: Any, depth: int = 0) -> list[str]:
    """递归收集结构里的字面链接，已知分享字段排在前面。

    字符串既可能是链接，也可能是**又一层 JSON**（``ark`` 段就是
    ``{"data": "{\\"app\\":\\"com.tencent.structmsg\\"...}"}``），所以要尝试二次解析。
    """
    if depth > _JSON_MAX_DEPTH:
        return []
    found: list[str] = []
    if isinstance(obj, str):
        text = obj.strip()
        if text[:1] in "{[" and text[-1:] in "}]":
            try:
                return walk_json(json.loads(text), depth + 1)
            except (ValueError, TypeError):
                return []
        cleaned = _clean(text)
        return [cleaned] if cleaned else []
    if isinstance(obj, dict):
        for key in _PREFERRED_KEYS:
            if key in obj:
                found.extend(walk_json(obj[key], depth + 1))
        for value in obj.values():
            found.extend(walk_json(value, depth + 1))
        return found
    if isinstance(obj, (list, tuple)):
        for item in obj:
            found.extend(walk_json(item, depth + 1))
    return found


def links_from_xml(text: Any) -> list[str]:
    """从 xml 卡片里取链接。

    先 ``html.unescape``：卡片里的 ``&amp;`` 不还原会让 ``b23.tv/x?a=1&amp;b=2``
    这种链接带着实体发出去，短链跳转直接 404。
    """
    if not isinstance(text, str) or not text:
        return []
    unescaped = html.unescape(text)
    found = [_clean(m) for m in _URL_RE.findall(unescaped)]
    for match in _XML_ATTR_RE.finditer(unescaped):
        found.append(_clean(match.group(1)))
    return [u for u in found if u]


def links_from_segment(seg: Any) -> list[str]:
    """单个消息段（原始 OneBot 段或 AstrBot 组件）里的全部链接。"""
    kind = _segment_type(seg)
    data = _segment_data(seg)

    if kind in ("text", "plain"):
        # 原始段把正文放在 ``data.text``；``Comp.Plain`` 放在 ``.text``。
        body = getattr(seg, "text", None) if not isinstance(seg, dict) else None
        if not isinstance(body, str) and isinstance(data, dict):
            body = data.get("text")
        elif not isinstance(body, str):
            body = data
        return _links_from_free_text(body) if isinstance(body, str) else []

    if kind in ("xml",):
        bodies = []
        if isinstance(data, dict):
            for key in ("data", "xml", "content"):
                if isinstance(data.get(key), str):
                    bodies.append(data[key])
        elif isinstance(data, str):
            bodies.append(data)
        found: list[str] = []
        for body in bodies or [str(data)]:
            found.extend(links_from_xml(body))
        return found

    # json / ark / light_app / miniapp：一律结构化扫描
    if kind == "share":
        # OneBot 的 ``share`` 段是扁平结构；``Comp.Share`` 把 url 放在字段上，
        # 没有 ``data``，所以两条都要认。
        raw_url = data.get("url") if isinstance(data, dict) else getattr(seg, "url", None)
        cleaned = _clean(raw_url or "")
        return [cleaned] if cleaned else []
    return walk_json(data)


def _links_from_free_text(text: str) -> list[str]:
    """纯文本：字面链接 + CQ 码里的 json/xml。"""
    found = [_clean(m) for m in _URL_RE.findall(text)]
    for match in _CQ_CODE_RE.finditer(text):
        kind, payload = match.group(1).lower(), match.group(2)
        payload = html.unescape(payload)
        if kind == "xml":
            found.extend(links_from_xml(payload))
            continue
        try:
            found.extend(walk_json(json.loads(payload)))
        except (ValueError, TypeError):
            # CQ 码里的引号可能被转义成 ``\\"``，再试一次
            try:
                found.extend(walk_json(json.loads(payload.replace(r"\"", '"'))))
            except (ValueError, TypeError):
                continue
    return [u for u in found if u]


def _dedup(links: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(link for link in links if link))


def extract_links(event: Any) -> list[str]:
    """一条消息里的全部候选链接，按「最可能是分享目标」排序。

    三条来源依次叠加，互为兜底：原始段（最全）→ 组件（自定义适配器）→ 纯文本。
    """
    found: list[str] = []
    for seg in raw_segments(event):
        found.extend(links_from_segment(seg))
    for comp in components(event):
        found.extend(links_from_segment(comp))
    for text in (raw_message_text(event), getattr(event, "message_str", "") or ""):
        if text:
            found.extend(_links_from_free_text(text))
    return _dedup(found)


# ---------------------------------------------------------------- 卡片判定


def has_card(event: Any) -> bool:
    """这条消息里是否带结构化分享卡片（json / xml / ark / share）。"""
    for seg in raw_segments(event):
        if _segment_type(seg) in CARD_SEGMENT_TYPES:
            return True
    for comp in components(event):
        kind = _segment_type(comp)
        if kind in CARD_SEGMENT_TYPES or "json" in kind:
            return True
    # 还没解析成数组的 CQ 码形态
    for text in (raw_message_text(event), getattr(event, "message_str", "") or ""):
        if _CQ_CODE_RE.search(text):
            return True
    return False


def _card_objects(event: Any) -> list[Any]:
    """所有卡片段的 ``data``（原始段优先，去重后返回）。"""
    seen: list[int] = []
    objects: list[Any] = []
    for seg in [*raw_segments(event), *components(event)]:
        if _segment_type(seg) not in CARD_SEGMENT_TYPES:
            continue
        data = _segment_data(seg)
        if data is None or id(data) in seen:
            continue
        seen.append(id(data))
        objects.append(data)
    return objects


def _first_str(obj: Any, keys: Iterable[str]) -> str:
    """在卡片结构里找第一个非空字符串字段（用于 app / title / desc）。"""
    for key in keys:
        stack = [obj]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                value = node.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
                stack.extend(node.values())
            elif isinstance(node, str) and node.strip()[:1] in "{[":
                try:
                    stack.append(json.loads(node))
                except (ValueError, TypeError):
                    continue
            elif isinstance(node, (list, tuple)):
                stack.extend(node)
    return ""


def card_summary(event: Any) -> str:
    """一张卡片的可读摘要，供日志与 ``/denia_card`` 排查用。

    只输出结构信息与链接，不输出图片正文（卡片里常带 base64 预览图）。
    """
    segments = [f"{_segment_type(seg)}" for seg in raw_segments(event)]
    comps = [_segment_type(comp) for comp in components(event)]
    lines = [
        f"原始段: {', '.join(segments) or '（无）'}",
        f"组件: {', '.join(comps) or '（无）'}",
        f"message_str: {(getattr(event, 'message_str', '') or '')[:120]!r}",
    ]
    if raw_message_text(event):
        lines.append("原始消息是 CQ 码字符串（post_format=string）")
    objects = _card_objects(event)
    if objects:
        app = _first_str(objects, ("app", "appid", "app_id", "template_id"))
        title = _first_str(objects, ("title", "desc", "prompt", "brief", "summary"))
        lines.append(f"卡片 app: {app or '（未识别）'}")
        lines.append(f"卡片标题: {title[:80] if title else '（无）'}")
    links = extract_links(event)
    lines.append(f"提取链接({len(links)}): " + ("、".join(links[:5]) or "（无）"))
    return "\n".join(lines)
