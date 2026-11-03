"""配置管理模块。

配置项的**唯一来源**是 :data:`CONFIG_META`（分组 + 键 + 类型 + 默认值 + 文案）。
插件的 WebUI 页面直接从它渲染表单；``_conf_schema.json`` 是同一次定义的**存储契约**
（AstrBot 加载插件配置时会剔除 schema 之外的键，两者必须保持一致，
由 :func:`verify_schema_alignment` 在启动时自检并告警）。

相比 rika 原版移除了 Cloudflare 截图 Fallback（20+ 配置项）与 B站 Cookie 监控轮询。

不提供 LLM 文本翻译。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .constants import PLATFORMS

_config = None

_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

# 合并转发模式白名单：读侧只认这三个字面量，脏值一律回退 none（保持直发）
AGGREGATION_MODES: frozenset[str] = frozenset({"none", "all", "conditional"})


def _aggregation_threshold(raw: Any, default: int) -> int:
    """把合并转发阈值读成非负 int。

    非法值（非数字、None、空串）回退默认；负数按 0 处理——0 在
    ``should_aggregate_nodes`` 里表示「这一项不触发」，比报错更符合预期。
    """
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError, AttributeError):
        return default
    return max(0, value)

# 配置分组：展示顺序即此处的顺序，常用在前、折腾在后
CONFIG_GROUPS: tuple[tuple[str, str], ...] = (
    ("解析设置", "日常最常用到的几项"),
    ("权限控制", "谁能触发解析：白名单 / 黑名单"),
    ("B站设置", "B站 Cookie 与下载清晰度"),
    ("Steam 设置", "价格地区与史低数据源"),
    ("音乐设置", "QQ音乐、网易云的登录与搜歌点歌"),
    ("CookieCloud", "从自建服务同步选定平台的浏览器登录态"),
    ("网页截图", "把任意网页截成图片发送"),
    ("输出模式", "选择文本、卡片与富媒体，可任意组合"),
    ("卡片外观", "把解析结果渲染成分享卡片图片"),
    ("媒体发送", "视频怎么送到消息平台：共享目录或中转链接"),
    ("合并转发", "选中的媒体要不要打包成一条合并转发消息"),
    ("高级设置", "一般用不到，按需开启"),
    ("维护", "缓存与调试，改动后立即生效"),
)

# 大类（一级分组）：把 CONFIG_GROUPS 按使用场景再归并，WebUI 配置页按
# 「大类 → 分组 → （可选）子组 → 配置项」三级展示。仅影响展示层，不参与存储契约
# （_conf_schema.json 仍按「分组」生成，键与默认值都不变，无需重新生成）。
CONFIG_SECTIONS: tuple[dict[str, Any], ...] = (
    {
        "key": "basic",
        "label": "基础",
        "description": "解析行为、发送策略与访问权限，日常最常改",
        "groups": ("解析设置", "权限控制"),
    },
    {
        "key": "platform",
        "label": "平台接入",
        "description": "各平台的凭据、清晰度与第三方数据源",
        "groups": ("B站设置", "Steam 设置", "音乐设置", "CookieCloud", "网页截图"),
    },
    {
        "key": "output",
        "label": "输出与投递",
        "description": "选择发送内容，调整卡片与媒体投递",
        "groups": ("输出模式", "卡片外观", "媒体发送", "合并转发"),
    },
    {
        "key": "advanced",
        "label": "高级与维护",
        "description": "凭据、网络与缓存调试，一般用不到",
        "groups": ("高级设置", "维护"),
    },
)

# 配置项元数据。字段说明：
#   key/group/label/type/default/hint 必填；type 取值 string | text | int | bool | select
#   secret=True 的值不会通过插件 WebUI 回读；配置文件的存储方式与原生面板一致。
#   options/labels 仅 select 使用；min/max/unit 仅 int 使用；placeholder 仅输入框使用
#   list=True 表示「逗号分隔字符串」类型的列表项，配置页渲染成标签编辑器
#   而不是单行输入框（存储格式不变，纯展示层增强）；
#   list_source="platforms" 时进一步渲染成平台勾选网格（选项取自 config 接口
#   返回的 platforms 列表），目前仅 DISABLED_PLATFORMS 使用
#   subgroup 可选：组内第三级小标题（配置项多的分组按它再分段，如「卡片外观」
#   按 基础/内容元素/配色 拆分）；不带 subgroup 的项排在同组最前
#   widget="switch-grid"：WebUI 把这些项渲染成一排紧凑小开关的网格（平台开关这类
#   「十来个 bool」逐行摆太占地方），label 只放平台名，开/关文案由前端 tile 自带

# 分解析器代理开关：按平台逐个决定「这个解析器要不要走代理地址」。
#
# 键名规则 ``PROXY_<平台键大写>``（如 PROXY_BILIBILI），与
# :meth:`ParserConfig.proxy_for` 的查键规则一致 —— 由 PLATFORMS 生成而不是手写，
# 新增平台时自动多一个开关。开关只控制插件配置的代理地址；关闭时不使用插件代理，
# httpx 的环境变量代理仍可能生效。自定义解析器没有对应的插件代理开关。
#
# label 用正式名、hint 用正式名（不用 card_name 昵称）：配置页里混着「猴山」
# 「小蓝鸟」找人会卡一下，正式名才是配置该用的语言。
_PROXY_SWITCH_ITEMS: tuple[dict[str, Any], ...] = tuple(
    {
        "key": f"PROXY_{key.upper()}",
        "group": "高级设置",
        "subgroup": "网络",
        "widget": "switch-grid",
        "label": meta.display_name,
        "type": "bool",
        "default": False,
        "hint": (
            f"开启后「{meta.display_name}」的接口请求与媒体下载才走上面的代理地址；"
            "关闭时不使用插件代理，环境变量代理仍可能生效。代理地址留空时本开关无效"
        ),
    }
    for key, meta in PLATFORMS.items()
)

CONFIG_META: tuple[dict[str, Any], ...] = (
    {"key": "COOKIECLOUD_URL", "group": "CookieCloud", "label": "服务地址", "type": "string", "default": "", "hint": "填写你自建的 CookieCloud 地址，可包含子目录。"},
    {"key": "COOKIECLOUD_UUID", "group": "CookieCloud", "label": "用户密钥 UUID", "type": "string", "default": "", "secret": True, "hint": "与浏览器 CookieCloud 扩展中的用户密钥一致。"},
    {"key": "COOKIECLOUD_PASSWORD", "group": "CookieCloud", "label": "端到端加密密码", "type": "string", "default": "", "secret": True, "hint": "仅在机器人本地解密，不会发送给同步服务器。"},
    {"key": "COOKIECLOUD_PLATFORMS", "group": "CookieCloud", "label": "同步平台", "type": "string", "default": "", "list": True, "hint": "填写平台名：bilibili、xiaohongshu、pixiv、netease、qqmusic、xiaoheihe；留空不导入。GitHub Token 不参与同步。"},
    {"key": "COOKIECLOUD_INTERVAL_MINUTES", "group": "CookieCloud", "label": "自动同步间隔", "type": "int", "default": 0, "min": 0, "max": 1440, "unit": "分钟", "hint": "0 为仅手动；自动同步只更新尚未配置或仍由 CookieCloud 管理的登录态，不覆盖手动修改。"},
    {
        "key": "MUSIC_COMMANDS_ENABLED", "group": "音乐设置", "label": "启用搜歌与点歌",
        "type": "bool", "default": True,
        "hint": "搜歌发送带编号的结果卡片，点歌或选歌后按当前语音/文件设置发送。分享链接解析由平台开关单独控制。",
    },
    {
        "key": "MUSIC_DEFAULT_PLATFORM", "group": "音乐设置", "label": "默认搜歌平台",
        "type": "select", "default": "both", "options": ["both", "netease", "qqmusic"],
        "labels": ["两家一起搜", "网易云音乐", "QQ音乐"],
        "hint": "也可以在指令里指定，例如：搜歌 QQ 晴天。",
    },
    {
        "key": "MUSIC_SEARCH_LIMIT", "group": "音乐设置", "label": "搜歌结果数量",
        "type": "int", "default": 5, "min": 1, "max": 10,
        "hint": "结果卡片最多显示这些候选；两家一起搜时交错排列，编号连续。",
    },
    {
        "key": "MUSIC_SESSION_TTL_SECONDS", "group": "音乐设置", "label": "选歌列表有效期",
        "type": "int", "default": 300, "min": 30, "max": 1800, "unit": "秒",
        "hint": "每个会话、每个用户分别保存搜索列表，过期后需要重新搜歌。",
    },
    {
        "key": "MUSIC_SEARCH_COMMANDS", "group": "音乐设置", "label": "搜歌指令",
        "type": "string", "default": "点歌",
        "hint": "点歌 歌名显示本插件的搜歌卡片；可用逗号配置其他别名，不包含空格或斜杠。",
    },
    {
        "key": "MUSIC_PLAY_COMMANDS", "group": "音乐设置", "label": "点歌与选歌指令",
        "type": "string", "default": "听",
        "hint": "听1 或听 1 发送列表第1首音频，不附带解析卡片。可用逗号配置其他选歌别名。",
    },
    {
        "key": "NETEASE_CK", "group": "音乐设置", "label": "网易云 Cookie",
        "type": "text", "default": "", "secret": True,
        "hint": "在上方账号凭据导入 MUSIC_U 或完整 Cookie，会员歌曲按账号实际权限获取。扫码不可用时可直接导入。",
    },
    {
        "key": "QQMUSIC_CK", "group": "音乐设置", "label": "QQ音乐 Cookie",
        "type": "text", "default": "", "secret": True,
        "hint": "在上方账号凭据扫码或导入包含 uin 与音乐密钥的 Cookie；可检查、续期与清除。",
    },
    # ---------------- 解析设置 ---------------- #
    {
        "key": "DISABLED_PLATFORMS",
        "group": "解析设置",
        "label": "禁用的平台",
        "type": "string",
        "default": "",
        "list": True,
        "list_source": "platforms",
        "hint": "勾选=禁用该平台，都不勾=全部启用。「总览」页的平台开关改的是同一份（那边点一下立即生效）",
    },
    {
        "key": "VIDEO_DURATION_MAXIMUM",
        "group": "解析设置",
        "label": "视频/音频最大时长",
        "type": "int",
        "default": 480,
        "min": 0,
        "max": 86400,
        "unit": "秒",
        "hint": "超过此时长不下载视频或音频；已选文本和卡片仍可显示内容与原因。",
    },
    {
        "key": "VIDEO_SIZE_MAXIMUM_MB",
        "group": "解析设置",
        "label": "单个视频/音频体积上限",
        "type": "int",
        "default": 100,
        "min": 1,
        "max": 4096,
        "unit": "MB",
        "hint": "下载前按 Content-Length 判断，边下边判。QQ 大文件上传容易失败，建议不超过 60",
    },
    {
        "key": "SEND_ERROR_MESSAGES",
        "group": "解析设置",
        "label": "解析失败时发送错误提示",
        "type": "bool",
        "default": False,
        "hint": "关闭则只写日志、不在群里刷报错。排查问题时可临时打开",
    },
    # ---------------- 权限控制 ---------------- #
    # 名单是**逗号分隔的字符串**，不是列表：本插件的配置是扁平键值，
    # _conf_schema.json 只支持 bool / int / select / string / text，
    # DISABLED_PLATFORMS 已经是这个套路。判定顺序见 core/permissions.py。
    # list=True 只影响 WebUI 的呈现（标签编辑器，回车添加、点 × 删除），
    # 存储格式不变。
    {
        "key": "WHITELIST_ENABLE",
        "group": "权限控制",
        "subgroup": "白名单",
        "label": "启用白名单",
        "type": "bool",
        "default": False,
        "hint": "开启后**只有**名单里的用户 / 群能触发解析，管理员始终豁免。名单全空 = 除管理员外谁都不能用",
    },
    {
        "key": "WHITELIST_USER",
        "group": "权限控制",
        "subgroup": "白名单",
        "label": "白名单用户",
        "type": "string",
        "default": "",
        "list": True,
        "placeholder": "用户 ID，回车添加",
        "hint": "留空 = 不按用户放行（私聊也会因此被拒）。支持整串粘贴，逗号 / 空格都能自动拆开",
    },
    {
        "key": "WHITELIST_GROUP",
        "group": "权限控制",
        "subgroup": "白名单",
        "label": "白名单群组",
        "type": "string",
        "default": "",
        "list": True,
        "placeholder": "群号，回车添加",
        "hint": "私聊没有群号，不受这一项影响。支持整串粘贴，逗号 / 空格都能自动拆开",
    },
    {
        "key": "BLACKLIST_ENABLE",
        "group": "权限控制",
        "subgroup": "黑名单",
        "label": "启用黑名单",
        "type": "bool",
        "default": False,
        "hint": "开启后名单里的用户 / 群不能触发解析，管理员仍豁免。与白名单同时开启时先看白名单、再看黑名单",
    },
    {
        "key": "BLACKLIST_USER",
        "group": "权限控制",
        "subgroup": "黑名单",
        "label": "黑名单用户",
        "type": "string",
        "default": "",
        "list": True,
        "placeholder": "用户 ID，回车添加",
        "hint": "名单里的用户不能触发解析。支持整串粘贴，逗号 / 空格都能自动拆开",
    },
    {
        "key": "BLACKLIST_GROUP",
        "group": "权限控制",
        "subgroup": "黑名单",
        "label": "黑名单群组",
        "type": "string",
        "default": "",
        "list": True,
        "placeholder": "群号，回车添加",
        "hint": "私聊没有群号，不受这一项影响。支持整串粘贴，逗号 / 空格都能自动拆开",
    },
    # ---------------- B站设置 ---------------- #
    {
        "key": "BILI_CK",
        "group": "B站设置",
        "label": "哔哩哔哩 Cookie",
        "type": "text",
        "default": "",
        "secret": True,
        "hint": "可在「配置 → 账号凭据 → B站」或总览页扫码登录；画质按账号权限获取",
    },
    {
        "key": "BILI_QUALITY",
        "group": "B站设置",
        "label": "B站下载清晰度",
        "type": "select",
        "default": "1080P",
        "options": ["360P", "480P", "720P", "1080P", "1080P+", "4K", "8K"],
        "hint": "高画质需要对应账号权限；未登录时实际只能拿到 720P",
    },
    {
        "key": "BILI_CODEC",
        "group": "B站设置",
        "label": "B站视频编码偏好",
        "type": "select",
        "default": "H.264 优先",
        "options": ["H.264 优先", "H.265 优先", "AV1 优先"],
        "hint": (
            "同一个视频 B站提供多种压缩格式，这里决定优先下哪一种。"
            "H.264 任何客户端都能播；AV1 体积最小但老客户端（如电脑版 QQ）放出来会花屏；"
            "H.265 介于两者之间。4K/8K 往往只有 H.265/AV1 可选，此时会自动退而求其次，"
            "不会下载失败——所以切到 H.264 优先不会让 4K 视频下不下来，只会拿不到该档的 H.264 版本"
        ),
    },
    # ---------------- Steam 设置 ---------------- #
    {
        "key": "STEAM_REGION",
        "group": "Steam 设置",
        "label": "价格地区代码",
        "type": "string",
        "default": "cn",
        "placeholder": "cn",
        "hint": "决定官方价格的货币（cn/us/jp 等），同时用作 ITAD 的 country 参数",
    },
    {
        "key": "ITAD_API_KEY",
        "group": "Steam 设置",
        "label": "IsThereAnyDeal API Key",
        "type": "string",
        "default": "",
        "secret": True,
        "hint": "只有想看「历史最低价」才需要，免费申请 https://isthereanydeal.com/apps 。留空时 Steam 解析照常工作，只是不显示史低",
    },
    # ---------------- 网页截图 ---------------- #
    {
        "key": "SCREENSHOT_BACKEND",
        "group": "网页截图",
        "subgroup": "通用",
        "label": "截图后端",
        "type": "select",
        "default": "thum",
        "options": ["thum", "cloudflare"],
        "labels": ["thum（免 key）", "cloudflare（需账号）"],
        "hint": "thum=image.thum.io，900×900 视窗图开箱即用；cloudflare 可截长图/等待 JS，但必须有账号",
    },
    {
        "key": "SCREENSHOT_FULL_PAGE",
        "group": "网页截图",
        "subgroup": "通用",
        "label": "截整页长图",
        "type": "bool",
        "default": True,
        "hint": "开启=截整页（长文章能看全），关闭=只截首屏视窗。实测差别很大：同一个维基长文"
                "关掉只有 900×900 的方图（约 0.3 MB，只看到顶部一小块），开完整页约 4.4 MB、"
                "高度按页面内容走（thum 上限 16000px，触顶时底部会缺）。"
                "代价是长图可能几 MB、群里发出去客户端会压缩，所以默认开启但可关。"
                "两个后端都支持整页（thum 走 /fullpage，cloudflare 走 fullPage）",
    },
    {
        "key": "SCREENSHOT_FALLBACK",
        "group": "网页截图",
        "subgroup": "通用",
        "label": "匹配不到平台的链接自动截图",
        "type": "bool",
        "default": False,
        "hint": "默认关闭，避免群里刷图。开启后任何解析不了的 http 链接都会尝试截图",
    },
    {
        "key": "CF_ACCOUNT_ID",
        "group": "网页截图",
        "subgroup": "Cloudflare 后端",
        "label": "Cloudflare Account ID",
        "type": "string",
        "default": "",
        "hint": "仅 cloudflare 后端需要，在 CF 控制台右侧栏可复制",
    },
    {
        "key": "CF_API_TOKEN",
        "group": "网页截图",
        "subgroup": "Cloudflare 后端",
        "label": "Cloudflare API Token",
        "type": "string",
        "default": "",
        "secret": True,
        "hint": "仅 cloudflare 后端需要，需带 Browser Rendering 写权限",
    },
    # ---------------- 卡片外观 ---------------- #
    {
        "key": "OUTPUT_TEXT_ENABLED",
        "group": "输出模式",
        "label": "文本",
        "type": "bool",
        "default": False,
        "hint": "一条普通文字消息，包含标题、链接、简洁播放信息与必要提示。可与卡片、富媒体同时发送。",
    },
    {
        "key": "OUTPUT_CARD_ENABLED",
        "group": "输出模式",
        "label": "卡片",
        "type": "bool",
        "default": True,
        "hint": "需同时开启「启用卡片渲染」。渲染失败时只跳过卡片，不改变其他输出选项。",
    },
    {
        "key": "OUTPUT_MEDIA_ENABLED",
        "group": "输出模式",
        "label": "富媒体",
        "type": "bool",
        "default": True,
        "hint": "发送原图、视频、动图和音频。关闭后不下载视频本体，卡片仅获取需要的图片。",
    },
    {
        "key": "AUDIO_SEND_MODE",
        "group": "输出模式",
        "label": "音频发送方式",
        "type": "select",
        "default": "voice",
        "options": ["voice", "file"],
        "labels": ["语音条", "音频文件"],
        "hint": "仅在富媒体开启时生效。语音条由宿主和协议端处理；音频文件保留原格式。每段音频只发送一种形式。",
    },
    {
        "key": "SEND_BACKGROUND_MUSIC",
        "group": "输出模式",
        "label": "发送图文配乐",
        "type": "bool",
        "default": False,
        "hint": "图文作品自带背景音乐时，是否把配乐也一并发出。目前抖音图文支持；默认关闭，避免普通图文突然多发一段音乐。",
    },
    {
        "key": "OUTPUT_MODES_INITIALIZED",
        "group": "输出模式",
        "label": "输出模式迁移标记",
        "type": "bool",
        "default": False,
        "internal": True,
        "hint": "由插件维护，避免宿主补齐默认值后重复迁移用户选择。",
    },
    {
        "key": "RENDER_ENABLED",
        "group": "卡片外观",
        "subgroup": "基础",
        "label": "启用卡片渲染",
        "type": "bool",
        "default": True,
        "hint": "仅控制卡片渲染。关闭后不发送卡片，不影响已选文本和富媒体。",
    },
    {
        "key": "RENDER_THEME",
        "group": "卡片外观",
        "subgroup": "基础",
        "label": "卡片主题",
        "type": "select",
        "default": "dark",
        "options": ["dark", "light"],
        "labels": ["深色", "浅色"],
        "hint": "只影响发送出去的卡片图片，与网页界面主题无关",
    },
    {
        "key": "RENDER_LAYOUT",
        "group": "卡片外观",
        "subgroup": "基础",
        "label": "卡片布局",
        "type": "select",
        "default": "standard",
        "options": ["standard", "magazine", "immersive", "feed"],
        "labels": ["standard 标准", "magazine 杂志", "immersive 沉浸", "feed 信息流"],
        "hint": "切换后可在「解析」页立刻预览效果",
    },
    {
        "key": "RENDER_WIDTH",
        "group": "卡片外观",
        "subgroup": "基础",
        "label": "卡片宽度",
        "type": "int",
        "default": 800,
        "min": 520,
        "max": 1080,
        "unit": "px",
        "hint": "520~1080，越宽信息越舒展，但在聊天软件里显示会更小",
    },
    {
        "key": "RENDER_COVER_FULL_SIZE",
        "group": "卡片外观",
        "subgroup": "内容元素",
        "label": "封面按原始尺寸展示",
        "type": "bool",
        "default": False,
        "hint": "开启后不裁切封面，长图会完整铺在卡片顶部",
    },
    {
        "key": "RENDER_FONT_PATH",
        "group": "卡片外观",
        "subgroup": "基础",
        "label": "自定义字体文件",
        "type": "string",
        "default": "",
        "hint": "留空自动探测系统字体；卡片出现方块字时才需要指定绝对路径",
    },
    {
        "key": "RENDER_ACCENT_COLOR",
        "group": "卡片外观",
        "subgroup": "配色",
        "label": "自定义强调色",
        "type": "string",
        "default": "",
        "placeholder": "#FB7299",
        "pattern": "^#[0-9a-fA-F]{6}$",
        "hint": "留空=每个平台用自己的品牌色。填 #RRGGBB 后所有卡片统一用这个强调色（徽章/光晕/水印圆点）",
    },
    {
        "key": "RENDER_WATERMARK",
        "group": "卡片外观",
        "subgroup": "内容元素",
        "label": "卡片水印文字",
        "type": "string",
        "default": "希望解析",
        "max_length": 12,
        "hint": "卡片右下角的水印文字，留空则不显示水印，最长 12 个字符",
    },
    {
        "key": "RENDER_DESC_MAX_LINES",
        "group": "卡片外观",
        "subgroup": "内容元素",
        "label": "正文最大行数",
        "type": "int",
        "default": 0,
        "min": 0,
        "max": 12,
        "unit": "行",
        "hint": "0=按布局默认（4~6 行）。简介超长时按此行数截断，卡片更紧凑",
    },
    {
        "key": "RENDER_SHOW_AVATAR",
        "group": "卡片外观",
        "subgroup": "内容元素",
        "label": "显示作者头像",
        "type": "bool",
        "default": True,
        "hint": "关闭后作者行只保留昵称与签名，不绘制头像圆（头像也不会再被加载）",
    },
    {
        "key": "RENDER_SHOW_PLAY_BUTTON",
        "group": "卡片外观",
        "subgroup": "内容元素",
        "label": "封面播放按钮",
        "type": "bool",
        "default": True,
        "hint": "显示视频封面中央的播放按钮，关闭后仅保留封面图片。",
    },
    {
        "key": "RENDER_SHOW_AI_SUMMARY",
        "group": "卡片外观",
        "subgroup": "内容元素",
        "label": "显示 B站 AI 总结",
        "type": "bool",
        "default": False,
        "hint": "标准、杂志、信息流布局可显示；沉浸式不显示。无可用总结时隐藏。",
    },
    {
        "key": "RENDER_GRADIENT_TOP",
        "group": "卡片外观",
        "subgroup": "配色",
        "label": "背景渐变·顶部色",
        "type": "string",
        "default": "",
        "placeholder": "#242B3F",
        "pattern": "^#[0-9a-fA-F]{6}$",
        "hint": "留空=跟随卡片主题。填 #RRGGBB 覆盖卡片背景渐变的顶部颜色",
    },
    {
        "key": "RENDER_GRADIENT_BOTTOM",
        "group": "卡片外观",
        "subgroup": "配色",
        "label": "背景渐变·底部色",
        "type": "string",
        "default": "",
        "placeholder": "#12161F",
        "pattern": "^#[0-9a-fA-F]{6}$",
        "hint": "留空=跟随卡片主题。填 #RRGGBB 覆盖卡片背景渐变的底部颜色",
    },
    # ---------------- 媒体发送 ---------------- #
    {
        "key": "CACHE_DIR",
        "group": "媒体发送",
        "subgroup": "方式一 · 本地共享目录",
        "label": "共享缓存目录",
        "type": "string",
        "default": "",
        "placeholder": "/app/sharedFolder/denia_share/cache",
        "hint": (
            "① 本地文件方式：填一个「所有容器都把宿主机同一目录挂到它上面」的容器内路径，"
            "协议端（NapCat 等）就能直接读走视频。留空=用插件数据目录 —— 分容器部署时协议端读不到，"
            "视频会发不出去（图片语音不受影响，它们会转成 base64）。"
            "安全限制：清理缓存会递归删除该目录，所以根目录、把插件数据目录包在里面的上级目录，"
            "以及「已有内容且没有 .denia_share_cache 哨兵文件」的目录都会被拒绝并回退到插件数据目录"
        ),
    },
    {
        "key": "MEDIA_RELAY_ENABLED",
        "group": "媒体发送",
        "subgroup": "方式二 · 中转链接",
        "label": "启用媒体中转",
        "type": "bool",
        "default": False,
        "hint": (
            "② 链接方式：把已下载的视频注册成 AstrBot 的临时 HTTP 链接（/api/file/<token>）再发送。"
            "协议端只要能访问到下面的回调地址即可，不需要共享挂载；注册失败会自动回退本地文件"
        ),
    },
    {
        "key": "MEDIA_RELAY_CALLBACK_URL",
        "group": "媒体发送",
        "subgroup": "方式二 · 中转链接",
        "label": "AstrBot 回调地址",
        "type": "string",
        "default": "",
        "placeholder": "http://astrbot:6185",
        "hint": (
            "留空时回退 AstrBot 全局 callback_api_base。必须填「消息平台所在容器能访问到」的地址，"
            "同一 Docker 网络内可直接用容器名，如 http://astrbot:6185"
        ),
    },
    {
        "key": "MEDIA_RELAY_TTL",
        "group": "媒体发送",
        "subgroup": "方式二 · 中转链接",
        "label": "中转链接有效期",
        "type": "int",
        "default": 300,
        "min": 30,
        "max": 86400,
        "unit": "秒",
        "hint": "到期后链接失效。视频越大、链路越慢越要留足时间，建议不低于 120 秒",
    },
    # ---------------- 合并转发 ---------------- #
    # 合并转发（一条消息里装多个节点）只有 OneBot 协议端支持，其它协议端拿不到
    # 这条入口，所以整组说明都标了「仅 OneBot 启用」。默认 none：升级后行为
    # 与现在完全一致（逐条直发），不会突然把一堆图片塞成一条消息。
    {
        "key": "AGGREGATION_MODE",
        "group": "合并转发",
        "label": "聚合方式",
        "type": "select",
        "default": "none",
        "options": ["none", "all", "conditional"],
        "labels": ["不聚合", "全部聚合", "按条件聚合"],
        "hint": (
            "仅 OneBot 启用。将选中的图片、视频逐条发送，或合并成一条转发消息。"
            "按条件聚合时，命中下面任一阈值才合并。"
            "语音条、音频文件独立发送，简短文字和卡片不受影响；转发明确失败时改为逐条发送。"
        ),
    },
    {
        "key": "AGGREGATION_IMAGE_THRESHOLD",
        "group": "合并转发",
        "label": "图片数量阈值",
        "type": "int",
        "default": 3,
        "min": 0,
        "max": 99,
        "unit": "张",
        "hint": "仅「按条件聚合」生效：图片数达到该值就合并。0=这一项不触发",
    },
    {
        "key": "AGGREGATION_VIDEO_THRESHOLD",
        "group": "合并转发",
        "label": "视频数量阈值",
        "type": "int",
        "default": 2,
        "min": 0,
        "max": 99,
        "unit": "个",
        "hint": "仅「按条件聚合」生效：视频数达到该值就合并。0=这一项不触发",
    },
    {
        "key": "AGGREGATION_NODE_THRESHOLD",
        "group": "合并转发",
        "label": "总节点阈值",
        "type": "int",
        "default": 5,
        "min": 0,
        "max": 999,
        "unit": "个",
        "hint": "仅「按条件聚合」生效：本次待发送的图片与视频节点总数达到该值时合并。语音、文件、精简文字和卡片不计入。0=这一项不触发",
    },
    # ---------------- Markdown 图片展示 ---------------- #
    # 与合并转发是**互斥的两条路径**：命中 Markdown 阈值时优先走 Markdown，
    # 不再打包成 Nodes。合并转发只有 OneBot 支持，而 Markdown 恰好是 QQ 官方
    # 机器人能渲染图片的通道，所以这条路径不依赖 _is_onebot。
    {
        "key": "MARKDOWN_IMAGE_ENABLED",
        "group": "合并转发",
        "label": "用 Markdown 集中展示图片",
        "type": "bool",
        "default": False,
        "hint": (
            "开启后，图片数量达到下面的阈值时不再合并转发，而是把图片按 "
            "Markdown 语法集中成一条（或几条）消息展示。"
            "适用于 QQ 官方机器人这类不支持合并转发的协议端；"
            "图片本身必须带公网 URL（平台直链），本地文件无法用 Markdown 渲染。"
            "本项优先于「聚合方式」，关闭后行为与原来完全一致"
        ),
    },
    {
        "key": "MARKDOWN_IMAGE_THRESHOLD",
        "group": "合并转发",
        "label": "Markdown 图片阈值",
        "type": "int",
        "default": 3,
        "min": 0,
        "max": 99,
        "unit": "张",
        "hint": "图片数量达到该值就改用 Markdown 集中展示。0=这一项不触发（等于关闭）",
    },
    {
        "key": "MARKDOWN_IMAGE_MAX_PER_MSG",
        "group": "合并转发",
        "label": "单条最大图片数",
        "type": "int",
        "default": 10,
        "min": 1,
        "max": 30,
        "unit": "张",
        "hint": "单条 Markdown 消息最多展示多少张图，超出时按顺序自动拆成多条，不重不漏",
    },
    {
        "key": "MARKDOWN_IMAGE_PER_ROW",
        "group": "合并转发",
        "label": "每行图片数",
        "type": "int",
        "default": 3,
        "min": 1,
        "max": 5,
        "unit": "张",
        "hint": "Markdown 里每行并排展示的图片数量，1 表示每张图独占一行",
    },
    {
        "key": "MARKDOWN_IMAGE_WIDTH",
        "group": "合并转发",
        "label": "Markdown 图片宽度",
        "type": "int",
        "default": 200,
        "min": 50,
        "max": 600,
        "unit": "px",
        "hint": (
            "QQ 官方机器人要求 Markdown 图片写成 ![文字 #宽px #高px](链接)，"
            "缺了宽高 QQ 只显示成 [image] 链接文字、图片不出来。默认 200"
        ),
    },
    {
        "key": "MARKDOWN_IMAGE_HEIGHT",
        "group": "合并转发",
        "label": "Markdown 图片高度",
        "type": "int",
        "default": 200,
        "min": 50,
        "max": 600,
        "unit": "px",
        "hint": (
            "等比模式下这是「最大高度」：按原图比例算出的高度超过它就压宽度封顶，"
            "比例不变。固定模式则直接作为显示高度"
        ),
    },
    {
        "key": "MARKDOWN_IMAGE_SIZE_MODE",
        "group": "合并转发",
        "label": "图片尺寸模式",
        "type": "select",
        "default": "auto",
        "options": ["auto", "fixed"],
        "labels": ["等比不扭曲（推荐）", "固定宽高"],
        "hint": (
            "auto：按每张原图的真实宽高算等比高度，不拉伸不变形；"
            "fixed：所有图强制成上面设置的宽×高，竖图会被压扁。"
            "本地图片读不到尺寸时回退到固定宽高"
        ),
    },
    {
        "key": "MARKDOWN_IMAGE_HINT_TEXT",
        "group": "合并转发",
        "label": "图集末尾提示文案",
        "type": "string",
        "default": "若提示已保存，点击再次保存即可",
        "hint": (
            "显示在 Markdown 图集最后一条消息末尾的一行弱化小字。"
            "QQ 保存多图消息时会把后面的图误报成「已保存」且实际没存，"
            "这行字用来提醒用户再点一次保存。留空则不显示任何提示"
        ),
    },
    # ---------------- 高级设置 ---------------- #
    {
        "key": "XIAOHEIHE_CK", "group": "高级设置", "subgroup": "平台凭据",
        "label": "小黑盒 Cookie", "type": "text", "default": "", "secret": True,
        "hint": "在上方「账号凭据 → 小黑盒」导入浏览器 Cookie。需要登录的帖子按账号权限获取，平台仍要求验证时请先在网页完成。",
    },
    {
        "key": "XHS_CK",
        "group": "高级设置",
        "subgroup": "平台凭据",
        "label": "小红书 Cookie",
        "type": "text",
        "default": "",
        "secret": True,
        "hint": "部分笔记需要登录态才能完整解析，从浏览器复制 xiaohongshu.com 的 cookie",
    },
    {
        "key": "PIXIV_CK",
        "group": "高级设置",
        "subgroup": "平台凭据",
        "label": "Pixiv Cookie",
        "type": "text",
        "default": "",
        "secret": True,
        "hint": "留空也能搜索。填了收录更全，但搜索结果会出现 R18 —— 插件对非全年龄内容有硬性过滤，不会放宽",
    },
    {
        "key": "GITHUB_TOKEN",
        "group": "高级设置",
        "subgroup": "平台凭据",
        "label": "GitHub Token",
        "type": "string",
        "default": "",
        "secret": True,
        "hint": "强烈建议填。免 token 限额 60 次/小时且按出口 IP 计算，共享 IP 下极易被限流；填后 5000 次/小时",
    },
    {
        "key": "PROXY",
        "group": "高级设置",
        "subgroup": "网络",
        "label": "代理地址",
        "type": "string",
        "default": "",
        "placeholder": "http://127.0.0.1:7897",
        "hint": (
            "留空 = 不使用插件代理，环境变量代理仍可能生效。填运行 AstrBot 那台机器上的地址；"
            "具体哪些平台走它由下面各平台的「走代理」开关决定，开关默认全关。"
            "Docker 分容器部署时这里填宿主地址（如 http://172.18.0.1:7890），"
            "不要填 127.0.0.1 —— 那是容器自己"
        ),
    },
    *_PROXY_SWITCH_ITEMS,
    {
        "key": "PROXY_SCREENSHOT",
        "group": "高级设置",
        "subgroup": "网络",
        "widget": "switch-grid",
        "label": "网页截图",
        "type": "bool",
        "default": False,
        "hint": (
            "开启后截图请求才走上面的代理地址；关闭时不使用插件代理，"
            "环境变量代理仍可能生效。代理地址留空时本开关无效"
        ),
    },
    {
        "key": "HTTP_VERIFY_SSL",
        "group": "高级设置",
        "subgroup": "网络",
        "label": "校验 HTTPS 证书",
        "type": "bool",
        "default": True,
        "hint": (
            "默认开启。下载与自建请求都会校验服务端证书，防止中间人截获带 Cookie 的请求。"
            "只有遇到证书链有问题的站点（自建反代、内网镜像）导致解析失败时，才临时关掉"
        ),
    },
    {
        "key": "TWITTER_MEDIA_PROXY_BASE",
        "group": "高级设置",
        "subgroup": "网络",
        "label": "Twitter 图片/视频反代根地址",
        "type": "string",
        "default": "",
        "placeholder": "https://your-proxy.example",
        "hint": "留空=不启用。服务器连不上 twimg 时填写",
    },
    # ---------------- 维护 ---------------- #
    {
        "key": "CACHE_TTL_HOURS",
        "group": "维护",
        "subgroup": "缓存清理",
        "label": "缓存保留时长",
        "type": "int",
        "default": 24,
        "min": 0,
        "max": 8760,
        "unit": "小时",
        "hint": "超过此时间未使用的缓存会被自动清理，设为 0 禁用自动清理",
    },
    {
        "key": "RESULT_CACHE_TTL_SECONDS",
        "group": "维护",
        "subgroup": "缓存清理",
        "label": "重复解析间隔",
        "type": "int",
        "default": 600,
        "min": 0,
        "max": 86400,
        "unit": "秒",
        "hint": (
            "同一条链接在此时长内再次发来时，直接用上一次的结果，不重新请求 B站/抖音等平台。"
            "设为 0 表示每次都重新解析。"
            "间隔别设得比「缓存保留时长」长：磁盘上的媒体文件先被清掉时，"
            "卡片还能发但视频会发不出来（插件会自动重新解析，只是白费一次请求）"
        ),
    },
    {
        "key": "DUPLICATE_LINK_WINDOW_SECONDS",
        "group": "维护",
        "subgroup": "缓存清理",
        "label": "同群重复链接去重",
        "type": "int",
        "default": 60,
        "min": 0,
        "max": 3600,
        "unit": "秒",
        "hint": (
            "**只在群聊里生效**：同一个群里，第一个人发某链接后的此时长内，其他人再发同一条"
            "链接就**不再解析、也不再发卡片**，只回一句「这个链接刚刚已经解析过了哦」，"
            "且这句提示也只发一次。私聊不去重。设为 0 关闭。"
            "与「重复解析间隔」的区别：那个是复用上次结果**照发卡片**，这个是**不响应**——"
            "后者是为了止住群里一条热链被连着转发时刷屏"
        ),
    },
    {
        "key": "CACHE_CLEANUP_INTERVAL_MINUTES",
        "group": "维护",
        "subgroup": "缓存清理",
        "label": "缓存清理间隔",
        "type": "int",
        "default": 60,
        "min": 1,
        "max": 1440,
        "unit": "分钟",
        "hint": "后台清理任务多久跑一次，改动需重载插件后才生效",
    },
    {
        "key": "DEBUG_LOG_ENABLED",
        "group": "维护",
        "subgroup": "日志",
        "label": "输出调试日志",
        "type": "bool",
        "default": True,
        "hint": "关闭后只保留警告与错误日志，排查问题时可打开",
    },
)

_ITEM_BY_KEY: dict[str, dict[str, Any]] = {item["key"]: item for item in CONFIG_META}

CONFIG_GROUP_KEYS: dict[str, tuple[str, ...]] = {
    group: tuple(item["key"] for item in CONFIG_META if item["group"] == group)
    for group, _ in CONFIG_GROUPS
}

_KEY_GROUP_MAP: dict[str, str] = {
    key: group for group, keys in CONFIG_GROUP_KEYS.items() for key in keys
}

_DEFAULTS: dict[str, Any] = {item["key"]: item["default"] for item in CONFIG_META}


def config_meta_payload() -> dict[str, Any]:
    """返回供 WebUI 渲染的配置元数据（大类 + 分组 + 全部配置项定义）。

    ``groups`` 保持扁平结构不变（向后兼容，旧前端/脚本直接读它）；
    ``sections`` 是大类视角的导航：配置页按「大类 → 分组 → 子组 → 配置项」
    渲染，子组来自配置项的可选 ``subgroup`` 字段。
    """
    return {
        "sections": [
            {
                "key": section["key"],
                "label": section["label"],
                "description": section["description"],
                # 只带真实存在的分组：CONFIG_SECTIONS 写错组名时这里静默收窄，
                # 由 section_coverage_problems() 在自检里把问题报出来
                "groups": [
                    name for name in section["groups"] if name in CONFIG_GROUP_KEYS
                ],
            }
            for section in CONFIG_SECTIONS
        ],
        "groups": [
            {
                "name": name,
                "description": description,
                "keys": [key for key in CONFIG_GROUP_KEYS.get(name, ()) if not _ITEM_BY_KEY[key].get("internal")],
            }
            for name, description in CONFIG_GROUPS
        ],
        "items": [dict(item) for item in CONFIG_META if not item.get("internal")],
    }


def section_coverage_problems() -> list[str]:
    """校验 CONFIG_SECTIONS 对 CONFIG_GROUPS 的覆盖，返回问题列表（空 = 正常）。

    每个分组必须恰好归属一个大类；引用不存在的分组、一个分组进两个大类、
    分组没有归属，都会报出来。不在 import 时断言 —— 配置模块在 stub 环境里
    也要能 import，覆盖问题由自检（test/webui/selfcheck.py）钉住。
    """
    problems: list[str] = []
    known = {name for name, _ in CONFIG_GROUPS}
    seen: dict[str, str] = {}
    for section in CONFIG_SECTIONS:
        for group in section["groups"]:
            if group not in known:
                problems.append(
                    f"大类「{section['label']}」引用了不存在的分组「{group}」"
                )
            elif group in seen:
                problems.append(
                    f"分组「{group}」同时属于「{seen[group]}」和「{section['label']}」"
                )
            else:
                seen[group] = section["label"]
    for name, _ in CONFIG_GROUPS:
        if name not in seen:
            problems.append(f"分组「{name}」没有归属任何大类")
    return problems


def coerce_value(item: dict[str, Any], raw: Any) -> tuple[Any, str | None]:
    """把前端传来的值转成配置要求的类型。

    Returns:
        (转换后的值, 错误描述)。错误描述非 None 时调用方应丢弃该值。
    """
    key = item["key"]
    label = item.get("label", key)
    kind = item["type"]

    if kind == "bool":
        if isinstance(raw, bool):
            return raw, None
        if isinstance(raw, str):
            lowered = raw.strip().lower()
            if lowered in {"true", "1", "yes", "on"}:
                return True, None
            if lowered in {"false", "0", "no", "off", ""}:
                return False, None
        return None, f"{label}：需要是开关值"

    if kind == "int":
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            return None, f"{label}：需要是整数"
        low, high = item.get("min"), item.get("max")
        if low is not None and value < low:
            return None, f"{label}：不能小于 {low}"
        if high is not None and value > high:
            return None, f"{label}：不能大于 {high}"
        return value, None

    text = "" if raw is None else str(raw).strip()
    if kind == "select":
        options = item.get("options") or []
        if text and text not in options:
            return None, f"{label}：只能是 {' / '.join(options)} 之一"
    if kind == "text" and len(text) > 8000:
        return None, f"{label}：内容过长（超过 8000 字）"
    max_length = item.get("max_length")
    if max_length is not None and len(text) > int(max_length):
        return None, f"{label}：不能超过 {max_length} 个字符"
    pattern = item.get("pattern")
    if pattern and text:
        import re

        if not re.match(pattern, text):
            return None, f"{label}：格式不正确（需要 {pattern}）"
    return text, None


def verify_schema_alignment(schema: dict[str, Any]) -> list[str]:
    """自检 ``_conf_schema.json`` 与 :data:`CONFIG_META` 的键集合是否一致。

    AstrBot 加载插件配置时会剔除 schema 中不存在的键（``check_config_integrity``），
    两者一旦不一致，用户保存的值会在下次重载时静默丢失，因此必须显式比对。

    Returns:
        描述差异的字符串列表，空列表表示一致。
    """
    schema_keys: set[str] = set()
    schema_group_of: dict[str, str] = {}
    problems: list[str] = []

    for group, node in schema.items():
        if not isinstance(node, dict):
            problems.append(f"schema 分组 {group} 不是对象")
            continue
        items = node.get("items")
        if not isinstance(items, dict):
            problems.append(f"schema 分组 {group} 缺少 items")
            continue
        for key in items:
            schema_keys.add(key)
            schema_group_of[key] = group

    meta_keys = set(_ITEM_BY_KEY)
    for key in sorted(schema_keys - meta_keys):
        problems.append(f"schema 有而 CONFIG_META 缺失的键：{key}")
    for key in sorted(meta_keys - schema_keys):
        problems.append(f"CONFIG_META 有而 schema 缺失的键：{key}")
    for key in sorted(schema_keys & meta_keys):
        if schema_group_of[key] != _KEY_GROUP_MAP.get(key):
            problems.append(
                f"{key} 在 schema 里属于 {schema_group_of[key]} 组，"
                f"但 CONFIG_META 归到 {_KEY_GROUP_MAP.get(key)} 组"
            )
    for group in schema:
        if group not in CONFIG_GROUP_KEYS:
            problems.append(f"schema 里存在 CONFIG_GROUPS 未声明的分组：{group}")
    return problems


def migrate_music_commands(config: Any) -> bool:
    """Migrate the old direct-play defaults to 点歌 / 听N, preserving custom names."""
    previous = {
        'MUSIC_SEARCH_COMMANDS': {'搜歌', '音乐搜索'},
        'MUSIC_PLAY_COMMANDS': {'点歌', '选歌'},
    }
    group = config.get('音乐设置')
    nested = group if isinstance(group, dict) else {}
    changed = False
    for key, legacy in previous.items():
        default = _DEFAULTS[key]
        raw = nested.get(key, config.get(key, default))
        if raw == default and key in config and config[key] != default:
            raw = config[key]
        aliases = {part for part in re.split(r'[,，、\s]+', str(raw or '')) if part}
        if not aliases or not aliases <= legacy:
            continue
        if not isinstance(group, dict):
            group = {}
            config['音乐设置'] = group
        group[key] = default
        # The compatibility fallback must not resurrect stale flat aliases.
        config.pop(key, None)
        changed = True
    return changed


def migrate_grouped_config(config: Any) -> bool:
    """旧版扁平配置 → 分组配置，幂等。"""
    changed = False
    for group, keys in CONFIG_GROUP_KEYS.items():
        group_cfg = config.get(group)
        if not isinstance(group_cfg, dict):
            group_cfg = {}
            config[group] = group_cfg
        for key in keys:
            default = _DEFAULTS.get(key)
            flat_val = config.get(key, default)
            nested_val = group_cfg.get(key, default)
            if nested_val == default and flat_val != default:
                group_cfg[key] = flat_val
                config[key] = default
                changed = True
    return changed


def migrate_proxy_switches(config: Any) -> bool:
    """1.2.6 起「全局代理」拆成代理地址 + 各平台「走代理」开关，一次性迁移，幂等。

    **为什么必须迁移**：升级后 ``PROXY_<平台>`` 在配置里不存在，读侧按默认值
    ``False`` 走 —— 于是老用户所有平台**静默变直连**。对只给 Pixiv 配了代理的人，
    症状是「更新完 Pixiv 突然解析不了了」，而排查方向会先去看代理通不通，
    恰好把注意力引到错误的组件上。而且这个窗口持续到用户第一次保存配置为止，
    不能指望用户自觉去勾。

    迁移规则：``PROXY`` 非空、且存储配置里**一个 ``PROXY_*`` 键都没有**时，
    把全部平台开关（含 ``PROXY_SCREENSHOT``）置 True，完整保留升级前的
    「配了代理就全走」语义；之后用户自己关掉国内那几个平台即可。

    「一个键都没有」这个判据是幂等的关键：用户只要动过一次开关，配置里就有了
    对应键，后续升级不再覆盖他的选择。

    **顺带收拾扁平残留键**：``_cfg_get`` 的回退规则是「分组值 == 默认值且扁平值
    ≠ 默认值 → 用扁平值」，而这些开关的默认值恰好就是 False。于是老配置里残留的
    扁平 ``PROXY_*: true`` 会盖掉用户在新版里**显式关掉**的同一个开关，而且
    分组里存的是 False（等于默认值），迁移也看不出区别 —— 症状是「我明明关了，
    重启后又走代理了」。所以这里把扁平位并入分组后删掉，让分组成为唯一事实来源。

    Returns:
        是否改动了配置（True 时调用方应落盘）。
    """
    switches = [item["key"] for item in CONFIG_META if item["key"].startswith("PROXY_")]
    if not switches:
        return False

    address = ""
    nested = config.get("高级设置")
    if isinstance(nested, dict):
        address = str(nested.get("PROXY", "") or "").strip()
    if not address:
        address = str(config.get("PROXY", "") or "").strip()
    if not address:
        # 从来没配过代理：开关保持默认 False，行为不变
        return False

    group = config.get("高级设置")
    if not isinstance(group, dict):
        group = {}
        config["高级设置"] = group

    # 扁平残留键并入分组后删掉：分组有值 = 那是用户最新的选择，扁平位只是历史残留
    changed = False
    for key in switches:
        if key not in config:
            continue
        if key not in group:
            group[key] = config[key]
        del config[key]
        changed = True

    # 分组里已经出现过开关，就认为用户做过选择，不再全开
    if any(key in group for key in switches):
        return changed

    for key in switches:
        group[key] = True
    return True


def migrate_output_modes(config: Any) -> bool:
    """迁移旧输出习惯，兼容宿主已经按 schema 填齐默认值的情况。"""
    group = config.get("输出模式")
    if not isinstance(group, dict):
        group = {}
        config["输出模式"] = group
    keys = ("OUTPUT_TEXT_ENABLED", "OUTPUT_CARD_ENABLED", "OUTPUT_MEDIA_ENABLED")
    marker = "OUTPUT_MODES_INITIALIZED"
    if group.get(marker) is True:
        return False
    # 兼容开发版的分组和扁平键，并保留明确的 false。
    legacy = config.get("解析设置")
    explicit = False
    for key in keys:
        if isinstance(legacy, dict) and key in legacy:
            group[key] = legacy.pop(key)
            explicit = True
        if key in config:
            group[key] = config.pop(key)
            explicit = True
    # 标记缺失、已有开关的原始配置属于明确选择；标记 false、值都等于
    # 默认值的形态则是宿主为老版本补齐 schema，仍需迁移旧输出习惯。
    explicit = explicit or (marker not in group and any(key in group for key in keys))
    explicit = explicit or any(key in group and group[key] != _DEFAULTS[key] for key in keys)
    if not explicit:
        render_group = config.get("卡片外观")
        render_enabled = render_group.get("RENDER_ENABLED", True) if isinstance(render_group, dict) else True
        if render_enabled is True and config.get("RENDER_ENABLED") is False:
            render_enabled = False
        for key in keys:
            group[key] = _DEFAULTS[key]
        if render_enabled is False:
            group["OUTPUT_TEXT_ENABLED"] = True
            group["OUTPUT_CARD_ENABLED"] = False
    group[marker] = True
    return True


class ParserConfig:
    """解析器配置 - 由 main.py 初始化"""

    def __init__(self, astrbot_config: Any, cache_dir: Path, config_dir: Path):
        self._cfg = astrbot_config
        self.cache_dir = cache_dir
        self.config_dir = config_dir

    def _cfg_get(self, key: str, default: Any = None) -> Any:
        """优先读分组配置，回退到扁平旧配置。"""
        group = _KEY_GROUP_MAP.get(key)
        if group is not None:
            group_cfg = self._cfg.get(group)
            if isinstance(group_cfg, dict) and key in group_cfg:
                default_val = _DEFAULTS.get(key, default)
                flat_val = self._cfg.get(key, default_val)
                nested_val = group_cfg[key]
                if nested_val == default_val and flat_val != default_val:
                    return flat_val
                return nested_val
        return self._cfg.get(key, default)

    # ---------------- WebUI 读写 ---------------- #

    def current_values(self) -> dict[str, Any]:
        """返回所有配置项的当前生效值（键 → 值）。"""
        return {
            item["key"]: self._cfg_get(item["key"], item["default"])
            for item in CONFIG_META if not item.get("internal")
        }

    def apply_updates(self, payload: Any) -> tuple[list[str], list[str]]:
        """按白名单把 WebUI 提交的值写回配置。

        只接受 :data:`CONFIG_META` 里声明的键；写入分组位置的同时把扁平旧键重置为
        默认值，避免遗留的旧值把新值盖掉（与 :func:`migrate_grouped_config` 同源）。

        Args:
            payload: 前端提交的 ``{配置键: 新值}``。

        Returns:
            (实际变更的键列表, 错误信息列表)。
        """
        if not isinstance(payload, dict):
            return [], ["提交内容不是对象"]

        changed: list[str] = []
        errors: list[str] = []

        for key, raw in payload.items():
            item = _ITEM_BY_KEY.get(key)
            if item is None or item.get("internal"):
                errors.append(f"未知配置项：{key}")
                continue
            value, error = coerce_value(item, raw)
            if error is not None:
                errors.append(error)
                continue
            if self._cfg_get(key, item["default"]) == value:
                continue
            group_cfg = self._cfg.get(_KEY_GROUP_MAP[key])
            if not isinstance(group_cfg, dict):
                group_cfg = {}
                self._cfg[_KEY_GROUP_MAP[key]] = group_cfg
            group_cfg[key] = value
            self._cfg[key] = item["default"]
            changed.append(key)

        if changed:
            if not self.save():
                errors.append(
                    "配置已生效但没能写入磁盘，重启插件后会回滚；"
                    "请检查配置目录是否可写、磁盘是否已满"
                )
        return changed, errors

    def reset_to_defaults(self) -> list[str]:
        """把所有配置项恢复为默认值，返回被改动的键列表。"""
        changed, _ = self.apply_updates(
            {item["key"]: item["default"] for item in CONFIG_META if not item.get("internal")}
        )
        return changed

    def save(self) -> bool:
        """持久化配置（AstrBotConfig 提供 save_config，缺失时静默跳过）。

        Returns:
            是否真的写盘成功。失败必须让调用方看见 —— 只写内存的修改在
            重启后会无声回滚，前端却已经提示「已保存」。
        """
        from astrbot.api import logger

        save = getattr(self._cfg, "save_config", None)
        if not callable(save):
            logger.warning(
                "[denia_share] 配置对象没有 save_config，本次修改只存在于内存，"
                "重启后会回滚"
            )
            return False
        try:
            save()
            return True
        except Exception:
            logger.exception(
                "[denia_share] 配置写入磁盘失败，本次修改只存在于内存，重启后会回滚"
            )
            return False

    # ---------------- 平台 ---------------- #

    @property
    def DISABLED_PLATFORMS(self) -> list[str]:
        raw = self._cfg_get("DISABLED_PLATFORMS", "")
        if not raw:
            return []
        return [p.strip().lower() for p in str(raw).split(",") if p.strip()]

    @property
    def VIDEO_DURATION_MAXIMUM(self) -> int:
        return int(self._cfg_get("VIDEO_DURATION_MAXIMUM", 480))

    @property
    def VIDEO_SIZE_MAXIMUM_MB(self) -> int:
        """单个视频体积上限（MB），超限不下载。"""
        return max(1, int(self._cfg_get("VIDEO_SIZE_MAXIMUM_MB", 100)))

    @property
    def PROXY(self) -> str:
        """代理地址，形如 http://127.0.0.1:7890，留空不使用。

        这里只存地址；**哪些解析器真的走它**由各 ``PROXY_<平台>`` 开关决定，
        见 :meth:`proxy_for`。
        """
        return str(self._cfg_get("PROXY", "") or "").strip()

    def proxy_for(self, platform_key: str) -> str | None:
        """某个平台解析器的插件代理；开关没开（或地址为空）时返回 None，不传插件代理。
        httpx 的环境变量代理仍可能生效。

        查键规则：``PROXY_<平台键大写>``。内置平台的开关由 CONFIG_META 按
        PLATFORMS 生成；自定义解析器没有对应配置项，``_cfg_get`` 拿到默认值
        False，永远不使用插件代理。
        """
        address = self.PROXY
        if not address:
            return None

        key = f"PROXY_{str(platform_key).strip().upper()}"
        if not bool(self._cfg_get(key, False)):
            return None
        return address

    @property
    def SCREENSHOT_PROXY(self) -> str:
        """截图服务的插件代理；开关关闭时返回空串，不传插件代理；环境变量代理仍可能生效。"""
        return self.PROXY if bool(self._cfg_get("PROXY_SCREENSHOT", False)) else ""

    @property
    def HTTP_VERIFY_SSL(self) -> bool:
        """是否校验 HTTPS 证书（默认开启；关掉会让带 Cookie 的请求可被中间人截获）。"""
        return bool(self._cfg_get("HTTP_VERIFY_SSL", True))

    @property
    def XHS_CK(self) -> str | None:
        return self._cfg_get("XHS_CK", None)

    @property
    def COOKIECLOUD_URL(self) -> str:
        return str(self._cfg_get('COOKIECLOUD_URL', '') or '').strip()

    @property
    def COOKIECLOUD_UUID(self) -> str:
        return str(self._cfg_get('COOKIECLOUD_UUID', '') or '').strip()

    @property
    def COOKIECLOUD_PASSWORD(self) -> str:
        return str(self._cfg_get('COOKIECLOUD_PASSWORD', '') or '')

    @property
    def COOKIECLOUD_PLATFORMS(self) -> str:
        return str(self._cfg_get('COOKIECLOUD_PLATFORMS', '') or '')

    @property
    def COOKIECLOUD_INTERVAL_MINUTES(self) -> int:
        return int(self._cfg_get('COOKIECLOUD_INTERVAL_MINUTES', 0))

    @property
    def XIAOHEIHE_CK(self) -> str:
        return str(self._cfg_get("XIAOHEIHE_CK", "") or "").strip()

    @property
    def NETEASE_CK(self) -> str:
        return str(self._cfg_get("NETEASE_CK", "") or "").strip()

    @property
    def QQMUSIC_CK(self) -> str:
        return str(self._cfg_get("QQMUSIC_CK", "") or "").strip()

    @property
    def MUSIC_COMMANDS_ENABLED(self) -> bool:
        return self._cfg_get("MUSIC_COMMANDS_ENABLED", True)

    @property
    def MUSIC_DEFAULT_PLATFORM(self) -> str:
        return self._cfg_get("MUSIC_DEFAULT_PLATFORM", "both")

    @property
    def MUSIC_SEARCH_LIMIT(self) -> int:
        return self._cfg_get("MUSIC_SEARCH_LIMIT", 5)

    @property
    def MUSIC_SESSION_TTL_SECONDS(self) -> int:
        return self._cfg_get("MUSIC_SESSION_TTL_SECONDS", 300)

    @property
    def MUSIC_SEARCH_COMMANDS(self) -> str:
        return self._cfg_get("MUSIC_SEARCH_COMMANDS", "点歌")

    @property
    def MUSIC_PLAY_COMMANDS(self) -> str:
        return self._cfg_get("MUSIC_PLAY_COMMANDS", "听")

    # ---------------- Pixiv / GitHub ---------------- #

    @property
    def PIXIV_CK(self) -> str:
        """Pixiv Cookie（可选）。

        配了 Cookie 搜索收录会更全，但也会让搜索结果开始出现 R18 作品。
        解析器侧对 xRestrict != 0 有硬性过滤，Cookie 不会放宽这条限制。
        """
        return str(self._cfg_get("PIXIV_CK", "") or "").strip()

    @property
    def GITHUB_TOKEN(self) -> str:
        """GitHub Personal Access Token（可选，但强烈建议）。

        免 token 时限额 60 次/小时且按**出口 IP**计，共享 IP 下几乎必被限流。
        填了 token 提升到 5000 次/小时（按账号计）。
        """
        return str(self._cfg_get("GITHUB_TOKEN", "") or "").strip()

    # ---------------- 网页截图 ---------------- #

    @property
    def SCREENSHOT_BACKEND(self) -> str:
        val = str(self._cfg_get("SCREENSHOT_BACKEND", "thum")).strip().lower()
        return val if val in {"thum", "cloudflare"} else "thum"

    @property
    def SCREENSHOT_FALLBACK(self) -> bool:
        """链接匹配不到任何平台时，是否自动截图（默认关，避免群里刷图）。"""
        return bool(self._cfg_get("SCREENSHOT_FALLBACK", False))

    @property
    def SCREENSHOT_FULL_PAGE(self) -> bool:
        """是否截整页长图（默认开）。

        默认开而不是默认关，是因为视窗截图在长页面上几乎不可用：thum 后端
        实测关掉整页只给 900×900 的方图，一个维基长文只能看到顶部一小块。

        **不能直接 ``bool(...)``**，两个坑：

        1. 空值。``bool('')`` 是 False，于是「键存在但值为空」会**静默关掉整页
           截图**，现象恰好等于「这功能从来没接上过」——最难查的那种。空值一律
           当作「没填」，回退默认。
        2. 字符串。``bool('false')`` 是 **True**，手改配置写成 ``"false"``
           反而会打开。字符串要走显式映射（与保存侧 ``_parse_bool`` 同一套
           词表，避免读侧写侧对同一个值给出相反解释）。
        """
        value = self._cfg_get("SCREENSHOT_FULL_PAGE", True)
        if value is None:
            return True
        if isinstance(value, str):
            text = value.strip().lower()
            if not text:
                return True
            if text in {"true", "1", "yes", "on"}:
                return True
            if text in {"false", "0", "no", "off"}:
                return False
            return True  # 无法识别的字符串回退默认，不猜
        return bool(value)

    @property
    def CF_ACCOUNT_ID(self) -> str:
        return str(self._cfg_get("CF_ACCOUNT_ID", "") or "").strip()

    @property
    def CF_API_TOKEN(self) -> str:
        return str(self._cfg_get("CF_API_TOKEN", "") or "").strip()

    # ---------------- B站 ---------------- #

    @property
    def BILI_CK(self) -> str | None:
        return self._cfg_get("BILI_CK", None)

    @property
    def BILI_QUALITY(self) -> str:
        return str(self._cfg_get("BILI_QUALITY", "1080P"))

    @property
    def BILI_CODEC(self) -> str:
        """B站视频编码偏好。

        读侧也要兜一层：配置文件可能被手工改过或从旧版本带过来一个
        不在白名单里的值，此时静默回退默认，而不是把脏值透给解析器
        （解析器只认这三个字面量，拿到别的就只有 codec 顺序错乱或报错）。
        """
        value = str(self._cfg_get("BILI_CODEC", "H.264 优先") or "").strip()
        if value in ("H.264 优先", "H.265 优先", "AV1 优先"):
            return value
        return "H.264 优先"

    # ---------------- Steam ---------------- #

    @property
    def STEAM_REGION(self) -> str:
        """Steam 价格地区代码（同时用于 ITAD 的 country 参数）。"""
        return str(self._cfg_get("STEAM_REGION", "cn") or "cn").strip().lower()

    @property
    def ITAD_API_KEY(self) -> str:
        """IsThereAnyDeal API key（可选）。

        只有想要「历史最低价」才需要。免费申请：https://isthereanydeal.com/apps
        留空时 Steam 解析照常工作，只是不显示史低。
        """
        return str(self._cfg_get("ITAD_API_KEY", "") or "").strip()

    # ---------------- 缓存 ---------------- #

    @property
    def CACHE_TTL_HOURS(self) -> int:
        return int(self._cfg_get("CACHE_TTL_HOURS", 24))

    @property
    def CACHE_CLEANUP_INTERVAL_MINUTES(self) -> int:
        return int(self._cfg_get("CACHE_CLEANUP_INTERVAL_MINUTES", 60))

    @property
    def DUPLICATE_LINK_WINDOW_SECONDS(self) -> int:
        """**群聊里**同一链接的「去重静默窗口」（秒）。0 = 关闭。

        与 :attr:`RESULT_CACHE_TTL_SECONDS` 是两回事，别混：

        - 重复解析间隔：命中缓存后**照旧发卡片**，省的是「再请求一次平台接口」
        - 本项：窗口内的后来者**不解析也不发卡片**，只回一句提示（且只回一次），
          治的是「群里一条热链被连着转发，机器人连发好几张卡」

        私聊不去重，见 :meth:`main.DeniaSharePlugin._dedup_applies`。
        """
        return int(self._cfg_get("DUPLICATE_LINK_WINDOW_SECONDS", 60))

    @property
    def RESULT_CACHE_TTL_SECONDS(self) -> int:
        """同一条链接多久之内不重复解析（秒）。0 = 每次都重新解析。

        与 :attr:`CACHE_TTL_HOURS` 是**两回事**：那个管磁盘上的媒体文件留多久，
        这个管内存里的解析结果复用多久。前者默认 24 小时、后者默认 10 分钟，
        所以「缓存保留时长 > 重复解析间隔」是正常的；反过来配（重复解析间隔
        比文件留得久）也不会出错 —— 命中时发现文件没了会自动重新解析。
        """
        return int(self._cfg_get("RESULT_CACHE_TTL_SECONDS", 600))

    # ---------------- 输出内容 ---------------- #

    @property
    def OUTPUT_TEXT_ENABLED(self) -> bool:
        return bool(self._cfg_get("OUTPUT_TEXT_ENABLED", False))

    @property
    def OUTPUT_CARD_ENABLED(self) -> bool:
        return bool(self._cfg_get("OUTPUT_CARD_ENABLED", True))

    @property
    def OUTPUT_MEDIA_ENABLED(self) -> bool:
        return bool(self._cfg_get("OUTPUT_MEDIA_ENABLED", True))

    @property
    def AUDIO_SEND_MODE(self) -> str:
        """音频按哪种形式发出：``voice``（语音条）或 ``file``（音频文件）。

        读侧**必须**走白名单回退：配置文件里的值可能来自 AstrBot 原生面板、
        手改 JSON 或跨版本残留，而发送层只认这两个字面量。脏值若透下去，
        就会落进「既不是语音也不是文件」的空档里，那条路没有任何兜底。
        """
        value = str(self._cfg_get("AUDIO_SEND_MODE", "voice") or "").strip()
        return value if value in ("voice", "file") else "voice"

    @property
    def SEND_BACKGROUND_MUSIC(self) -> bool:
        """图文作品自带配乐时是否一并发送（解析侧读取）。"""
        return bool(self._cfg_get("SEND_BACKGROUND_MUSIC", False))

    def output_modes(self) -> dict[str, bool]:
        """本次请求实际允许输出哪几路（**已计入卡片总开关**）。

        卡片的条件是「用户勾了卡片」**且**「卡片总开关打开」——两个都开才算
        一路输出。总开关只掐卡片，文本与媒体永远不受它影响。

        ``card_effective`` 与 ``card_selected`` 分开返回：前者是真正会不会发卡片，
        后者是用户的选择。配置页与提示需要区分「我没勾」和「我勾了但总开关关了」。
        """
        card_selected = self.OUTPUT_CARD_ENABLED
        card_effective = bool(card_selected and self.RENDER_ENABLED)
        return {
            "text": self.OUTPUT_TEXT_ENABLED,
            "card_effective": card_effective,
            "card_selected": card_selected,
            "media": self.OUTPUT_MEDIA_ENABLED,
        }

    def any_output_enabled(self) -> bool:
        """三路全关时应短路：既不解析也不下载，更不占去重与历史。"""
        modes = self.output_modes()
        return bool(modes["text"] or modes["card_effective"] or modes["media"])

    # ---------------- 合并转发 ---------------- #

    @property
    def AGGREGATION_MODE(self) -> str:
        """合并转发模式：``none`` / ``all`` / ``conditional``。

        和 :attr:`AUDIO_SEND_MODE` 同样的道理，读侧必须走白名单回退：值可能
        来自 AstrBot 原生面板、手改 JSON 或旧版本残留，脏值透下去就会掉进
        「既不是 all 也不是 conditional 也不是 none」的分支里。
        """
        value = str(self._cfg_get("AGGREGATION_MODE", "none") or "").strip().lower()
        return value if value in AGGREGATION_MODES else "none"

    @property
    def AGGREGATION_IMAGE_THRESHOLD(self) -> int:
        return _aggregation_threshold(self._cfg_get("AGGREGATION_IMAGE_THRESHOLD", 3), 3)

    @property
    def AGGREGATION_VIDEO_THRESHOLD(self) -> int:
        return _aggregation_threshold(self._cfg_get("AGGREGATION_VIDEO_THRESHOLD", 2), 2)

    @property
    def AGGREGATION_NODE_THRESHOLD(self) -> int:
        return _aggregation_threshold(self._cfg_get("AGGREGATION_NODE_THRESHOLD", 5), 5)

    def should_aggregate_nodes(
        self,
        image_count: int,
        video_count: int,
        node_count: int,
    ) -> bool:
        """按当前配置判断这次是否发送合并转发。

        ``all`` 要求**确实有节点**才合并：没有内容时合成一条空消息没有意义，
        直接 False，调用方可以照常走逐条直发。``conditional`` 是任一**启用**
        的阈值命中即可，阈值为 0 表示这一项不触发而不是「立刻满足」；
        ``none`` 恒为 False。
        """
        mode = self.AGGREGATION_MODE
        if mode == "all":
            return int(node_count or 0) > 0
        if mode != "conditional":
            return False
        return any(
            threshold > 0 and count >= threshold
            for threshold, count in (
                (self.AGGREGATION_IMAGE_THRESHOLD, image_count),
                (self.AGGREGATION_VIDEO_THRESHOLD, video_count),
                (self.AGGREGATION_NODE_THRESHOLD, node_count),
            )
        )

    # ---------------- Markdown 图片展示 ---------------- #

    @property
    def MARKDOWN_IMAGE_ENABLED(self) -> bool:
        return bool(self._cfg_get("MARKDOWN_IMAGE_ENABLED", False))

    @property
    def MARKDOWN_IMAGE_THRESHOLD(self) -> int:
        return _aggregation_threshold(self._cfg_get("MARKDOWN_IMAGE_THRESHOLD", 3), 3)

    @property
    def MARKDOWN_IMAGE_MAX_PER_MSG(self) -> int:
        """单条 Markdown 最多图片数，至少 1（0 会让拆分死循环）。"""
        return max(
            1,
            _aggregation_threshold(self._cfg_get("MARKDOWN_IMAGE_MAX_PER_MSG", 10), 10),
        )

    @property
    def MARKDOWN_IMAGE_PER_ROW(self) -> int:
        """每行图片数，夹在 1..5。"""
        value = _aggregation_threshold(self._cfg_get("MARKDOWN_IMAGE_PER_ROW", 3), 3)
        return min(5, max(1, value))

    @property
    def MARKDOWN_IMAGE_WIDTH(self) -> int:
        """Markdown 内嵌图宽度（px）。QQ 要求显式宽高，缺了不渲染。"""
        value = _aggregation_threshold(self._cfg_get("MARKDOWN_IMAGE_WIDTH", 200), 200)
        return min(600, max(50, value))

    @property
    def MARKDOWN_IMAGE_HEIGHT(self) -> int:
        """Markdown 内嵌图高度（px）。等比模式下作为最大高度上限。"""
        value = _aggregation_threshold(self._cfg_get("MARKDOWN_IMAGE_HEIGHT", 200), 200)
        return min(600, max(50, value))

    @property
    def MARKDOWN_IMAGE_SIZE_MODE(self) -> str:
        """尺寸模式：``auto`` 等比不扭曲 / ``fixed`` 固定宽高。

        读侧白名单回退：脏值一律当 ``auto``，因为等比是更安全的默认
        （固定宽高会把竖图压扁，属于用户能立刻看出来的观感问题）。
        """
        value = str(self._cfg_get("MARKDOWN_IMAGE_SIZE_MODE", "auto") or "").strip().lower()
        return value if value in ("auto", "fixed") else "auto"

    @property
    def MARKDOWN_IMAGE_HINT_TEXT(self) -> str:
        """Markdown 图集末尾的提示文案，留空表示不显示。

        单行渲染在块引用里，换行会被压成空格；超长文案截断，避免把
        消息撑得很难看。
        """
        value = str(
            self._cfg_get("MARKDOWN_IMAGE_HINT_TEXT", "若提示已保存，点击再次保存即可") or ""
        )
        value = " ".join(value.split())
        return value[:80]

    def should_send_markdown_images(self, image_count: int) -> bool:
        """这次是否改用 Markdown 集中展示图片。

        与 :meth:`should_aggregate_nodes` 独立判定：Markdown 是本插件自己的
        通道（QQ 官方机器人可渲染），不依赖 OneBot；命中时由调用方优先走它。
        阈值为 0 视为关闭，避免「0 张图也触发」这种反直觉结果。
        """
        if not self.MARKDOWN_IMAGE_ENABLED:
            return False
        threshold = self.MARKDOWN_IMAGE_THRESHOLD
        return threshold > 0 and int(image_count or 0) >= threshold

    # ---------------- 渲染 ---------------- #

    @property
    def RENDER_ENABLED(self) -> bool:
        return bool(self._cfg_get("RENDER_ENABLED", True))

    @property
    def RENDER_THEME(self) -> str:
        val = str(self._cfg_get("RENDER_THEME", "dark")).strip().lower()
        return val if val in {"dark", "light"} else "dark"

    @property
    def RENDER_LAYOUT(self) -> str:
        val = str(self._cfg_get("RENDER_LAYOUT", "standard")).strip().lower()
        return val if val in {"standard", "magazine", "immersive", "feed"} else "standard"

    @property
    def RENDER_WIDTH(self) -> int:
        return max(520, min(1080, int(self._cfg_get("RENDER_WIDTH", 800))))

    @property
    def RENDER_FONT_PATH(self) -> str:
        return str(self._cfg_get("RENDER_FONT_PATH", "") or "").strip()

    @property
    def RENDER_COVER_FULL_SIZE(self) -> bool:
        return bool(self._cfg_get("RENDER_COVER_FULL_SIZE", False))

    @property
    def RENDER_ACCENT_COLOR(self) -> str:
        """自定义强调色（#RRGGBB），留空表示按平台品牌色。"""
        value = str(self._cfg_get("RENDER_ACCENT_COLOR", "") or "").strip()
        return value if _HEX_COLOR_RE.match(value) else ""

    @property
    def RENDER_WATERMARK(self) -> str:
        """卡片右下角水印文字，留空表示不显示。"""
        return str(self._cfg_get("RENDER_WATERMARK", "希望解析") or "").strip()[:12]

    @property
    def RENDER_DESC_MAX_LINES(self) -> int:
        """正文最大行数，0 表示按布局默认。"""
        return max(0, min(12, int(self._cfg_get("RENDER_DESC_MAX_LINES", 0))))

    @property
    def RENDER_SHOW_AVATAR(self) -> bool:
        return bool(self._cfg_get("RENDER_SHOW_AVATAR", True))

    @property
    def RENDER_SHOW_PLAY_BUTTON(self) -> bool:
        return bool(self._cfg_get("RENDER_SHOW_PLAY_BUTTON", True))

    @property
    def RENDER_SHOW_AI_SUMMARY(self) -> bool:
        """是否在卡片上显示 B站 AI 总结（默认关闭；沉浸式布局始终不显示）。"""
        return bool(self._cfg_get("RENDER_SHOW_AI_SUMMARY", False))

    @property
    def RENDER_GRADIENT_TOP(self) -> str:
        value = str(self._cfg_get("RENDER_GRADIENT_TOP", "") or "").strip()
        return value if _HEX_COLOR_RE.match(value) else ""

    @property
    def RENDER_GRADIENT_BOTTOM(self) -> str:
        value = str(self._cfg_get("RENDER_GRADIENT_BOTTOM", "") or "").strip()
        return value if _HEX_COLOR_RE.match(value) else ""

    def renderer_options(self) -> dict[str, Any]:
        """返回构造 ShareCardRenderer 的关键字参数（配置的唯一装配点）。

        main.py 的常驻渲染器与 webui.py 的临时预览渲染器都用它，
        新增外观项只需要在这里和 ``_build_renderer`` 的覆盖层各加一行。
        """
        return {
            "enabled": self.RENDER_ENABLED,
            "width": self.RENDER_WIDTH,
            "theme": self.RENDER_THEME,
            "font_path": self.RENDER_FONT_PATH or None,
            "layout": self.RENDER_LAYOUT,
            "cover_full_size": self.RENDER_COVER_FULL_SIZE,
            "accent_color": self.RENDER_ACCENT_COLOR or None,
            "watermark": self.RENDER_WATERMARK,
            "desc_max_lines": self.RENDER_DESC_MAX_LINES,
            "show_avatar": self.RENDER_SHOW_AVATAR,
            "show_play_button": self.RENDER_SHOW_PLAY_BUTTON,
            "show_ai_summary": self.RENDER_SHOW_AI_SUMMARY,
            "gradient_top": self.RENDER_GRADIENT_TOP or None,
            "gradient_bottom": self.RENDER_GRADIENT_BOTTOM or None,
        }

    # ---------------- 媒体发送 ---------------- #

    @property
    def CACHE_DIR(self) -> str:
        """Docker 共享缓存目录，留空表示用插件数据目录（默认行为）。

        留空始终使用插件数据目录，不自动切换到可能未挂载的共享路径。
        """
        return str(self._cfg_get("CACHE_DIR", "") or "").strip()

    @property
    def MEDIA_RELAY_ENABLED(self) -> bool:
        """是否把已下载的视频注册成 AstrBot 的临时 HTTP 链接再发送。"""
        return bool(self._cfg_get("MEDIA_RELAY_ENABLED", False))

    @property
    def MEDIA_RELAY_CALLBACK_URL(self) -> str:
        """中转链接用的回调地址；留空时回退 AstrBot 全局 callback_api_base。"""
        return str(self._cfg_get("MEDIA_RELAY_CALLBACK_URL", "") or "").strip().rstrip("/")

    @property
    def MEDIA_RELAY_TTL(self) -> int:
        """中转链接有效期（秒），最小 30。"""
        return max(30, int(self._cfg_get("MEDIA_RELAY_TTL", 300)))

    # ---------------- 行为与调试 ---------------- #

    @property
    def SEND_ERROR_MESSAGES(self) -> bool:
        """解析失败时是否向对话发送错误提示（关闭则只记日志，群里更安静）。"""
        return bool(self._cfg_get("SEND_ERROR_MESSAGES", False))

    @property
    def TWITTER_MEDIA_PROXY_BASE(self) -> str:
        """Twitter/X 媒体反代根地址（结尾斜杠会被去掉），留空表示不启用。"""
        return str(self._cfg_get("TWITTER_MEDIA_PROXY_BASE", "") or "").strip().rstrip("/")

    @property
    def DEBUG_LOG_ENABLED(self) -> bool:
        return bool(self._cfg_get("DEBUG_LOG_ENABLED", True))

    # ---------------- 权限控制 ----------------
    # 名单返回**原始字符串**（逗号分隔），解析交给 core/permissions.py ——
    # 那里同时收中文逗号与顿号，且在两个开关都关时根本不解析。

    @property
    def WHITELIST_ENABLE(self) -> bool:
        return bool(self._cfg_get("WHITELIST_ENABLE", False))

    @property
    def WHITELIST_USER(self) -> str:
        return str(self._cfg_get("WHITELIST_USER", "") or "")

    @property
    def WHITELIST_GROUP(self) -> str:
        return str(self._cfg_get("WHITELIST_GROUP", "") or "")

    @property
    def BLACKLIST_ENABLE(self) -> bool:
        return bool(self._cfg_get("BLACKLIST_ENABLE", False))

    @property
    def BLACKLIST_USER(self) -> str:
        return str(self._cfg_get("BLACKLIST_USER", "") or "")

    @property
    def BLACKLIST_GROUP(self) -> str:
        return str(self._cfg_get("BLACKLIST_GROUP", "") or "")


def init_config(astrbot_config: Any, cache_dir: Path, config_dir: Path) -> ParserConfig:
    global _config
    _config = ParserConfig(astrbot_config, cache_dir, config_dir)
    return _config


def get_config() -> ParserConfig:
    global _config
    if _config is None:
        raise RuntimeError("ParserConfig not initialized yet")
    return _config
