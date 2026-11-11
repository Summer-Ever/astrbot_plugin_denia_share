"""媒体与文件工具函数

涵盖缓存清理、ffmpeg 处理（音视频合并 / 转码 / 抽帧 / 转 GIF）、
文件名生成，以及时长格式化。
"""

import os
import re
import json
import math
import asyncio
import hashlib
import shutil
import time
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urljoin, urlparse

from astrbot.api import logger

from .exception import MediaProcessException

# 防解压炸弹：PIL 的默认上限约 89M 像素，且只在超过**两倍**时才抛错，
# 1~2 倍之间仅发一条 warning。本仓的图全部来自远端，一张 30000×30000 的 PNG
# 解压后能吃掉几 GB 内存 —— 显式压到 64M 像素：超过 128M 像素直接抛
# DecompressionBombError，而不是把进程拖死。
try:
    from PIL import Image as _PILImage

    _PILImage.MAX_IMAGE_PIXELS = 64 * 1024 * 1024
except Exception:  # PIL 缺失时本模块的其他功能仍要可用
    pass


def fmt_duration(duration: float) -> str:
    """格式化媒体时长，超过 1 小时后显示为 h:mm:ss。"""
    total_seconds = max(int(duration), 0)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


async def safe_unlink(path: Path):
    """安全删除文件（失败只记日志，不向上抛）"""
    try:
        if path.exists():
            path.unlink()
    except Exception:
        logger.warning(f"删除文件失败: {path}", exc_info=True)


# 缓存目录哨兵文件名。
#
# 为什么需要它：清理是**递归删除**，而「共享缓存目录」是个自由文本配置项，
# 填错了就会把无关目录清空。哨兵的作用是把「这是个缓存目录」变成可验证的事实，
# 而不是靠配置项里填了什么来推断。目录里已经有别的东西、又没有这个文件时，
# 清理函数一律拒绝执行。
CACHE_MARKER_NAME = ".denia_share_cache"


def is_cache_dir_trusted(cache_dir: Path) -> bool:
    """目录里是否有缓存哨兵（没有就说明不该对它做递归清理）。"""
    try:
        return (cache_dir / CACHE_MARKER_NAME).is_file()
    except OSError:
        return False


def ensure_cache_marker(cache_dir: Path, *, allow_nonempty: bool = False) -> bool:
    """确保缓存目录带哨兵文件。

    Args:
        cache_dir: 目标目录（应已存在）。
        allow_nonempty: 目录非空时是否仍然补写哨兵。插件自己管的目录
            （默认数据目录下的 cache/）传 True；用户填的共享目录传 False，
            这样「指向一个已有内容的目录」会被拒绝而不是被清空。

    Returns:
        是否可安全用作缓存目录。
    """
    marker = cache_dir / CACHE_MARKER_NAME
    try:
        if marker.is_file():
            return True
        if not allow_nonempty and any(cache_dir.iterdir()):
            return False
        marker.touch()
        return True
    except OSError as exc:
        logger.warning(f"写入缓存哨兵失败: {cache_dir} ({exc})")
        return False


async def cleanup_cache_dir(cache_dir: Path, ttl_hours: int) -> int:
    """清理缓存目录中超过 TTL 的过期文件（**递归**，含子目录）。

    目录没有缓存哨兵时直接返回 0 —— 说明它不是插件认领的缓存目录，
    宁可不清也不能清错。

    Args:
        cache_dir: 缓存目录路径。
        ttl_hours: 文件存活阈值（小时）。文件最后修改时间早于
                   (now - ttl_hours) 则视为过期。

    Returns:
        清理的文件数量。
    """
    if not cache_dir.exists():
        return 0

    if not is_cache_dir_trusted(cache_dir):
        logger.warning(
            f"缓存目录 {cache_dir} 缺少 {CACHE_MARKER_NAME} 哨兵，已跳过清理"
        )
        return 0

    cutoff = time.time() - ttl_hours * 3600
    cleaned = 0
    # 哨兵必须豁免。它是个空文件、mtime 就是创建时间，TTL 一到就会被当过期文件删掉，
    # 后果是连锁的：
    #   1. 下一轮 is_cache_dir_trusted 变 False → 清理**永久停摆**，
    #      「清除缓存」按钮也一起失效；
    #   2. 共享缓存目录更糟 —— 哨兵没了、目录里又有内容，重启时
    #      ensure_cache_marker(allow_nonempty=False) 会拒绝补写 → 永久回退到插件数据
    #      目录，媒体共享/中转静默失效（默认目录能自愈，因为它传的是 allow_nonempty=True）。
    # clear_cache_dir 一直是豁免的，只有这里漏了。
    marker = cache_dir / CACHE_MARKER_NAME

    # **递归遍历**：截图落在 cache_dir/screenshot/ 这类子目录里，只看顶层的话
    # 它们永远不会过期 —— 缓存页的体积一直涨，而「清理过期文件」清不动它。
    # clear_cache_dir 一直用的是 rglob，两者语义本来就该一致。
    for f in cache_dir.rglob("*"):
        if not f.is_file() or f == marker:
            continue
        try:
            if f.stat().st_mtime < cutoff:
                await safe_unlink(f)
                cleaned += 1
        except Exception:
            continue

    # 双保险：上面的 trusted 检查已通过（说明这确实是插件认领的目录），
    # 所以补写哨兵是安全的。万一它被外部删掉，至少能自愈而不是永久停摆。
    if not ensure_cache_marker(cache_dir, allow_nonempty=True):
        logger.warning(f"缓存哨兵补写失败，下轮清理可能被跳过: {cache_dir}")

    if cleaned > 0:
        logger.info(f"缓存清理完成: 已清理 {cleaned} 个过期文件 ({cache_dir})")
    return cleaned


async def clear_cache_dir(cache_dir: Path) -> int:
    """清空缓存目录中的所有文件，并删除其中的空目录。

    只保留目录本身与哨兵文件。**目录没有缓存哨兵时直接返回 0**：
    清理是递归删除，而缓存目录可被配置项指向任意路径，所以必须先确认
    「这确实是插件认领的缓存目录」再动手，而不是反过来事后补救。
    """
    if not cache_dir.exists():
        return 0

    if not is_cache_dir_trusted(cache_dir):
        logger.warning(
            f"缓存目录 {cache_dir} 缺少 {CACHE_MARKER_NAME} 哨兵，已跳过清理。"
            f"如果确实要把它当缓存目录，请先在该目录下建一个空的 {CACHE_MARKER_NAME} 文件"
        )
        return 0

    cleaned = 0
    # 深路径优先，保证子目录先被清空才能 rmdir
    entries = sorted(cache_dir.rglob("*"), key=lambda p: len(p.parts), reverse=True)
    # 只跳过**缓存目录根下**的那个哨兵：按文件名比较会把子目录里恰好同名的文件
    # 一起放过（无害，但会让「哨兵」的语义变得含糊）
    marker = cache_dir / CACHE_MARKER_NAME

    for entry in entries:
        if entry == marker:
            continue
        try:
            # 先判断符号链接，避免误将链接目标当作缓存目录递归处理
            if entry.is_symlink() or entry.is_file():
                entry.unlink()
                cleaned += 1
            elif entry.is_dir():
                entry.rmdir()
        except FileNotFoundError:
            # 清理过程中可能已被下载任务或其他清理任务先行删除
            continue
        except OSError as exc:
            logger.warning(f"清理缓存失败: {entry} ({exc})")

    logger.info(f"缓存清空完成: 已清理 {cleaned} 个文件 ({cache_dir})")
    return cleaned


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    """尽力杀掉子进程并回收。

    清理路径里**不能再抛新异常**（会盖掉调用方要向上抛的那个），所以这里连
    ``BaseException`` 一起吞：取消中的第二次 await 可能再抛一次 CancelledError。
    """
    try:
        process.kill()
    except (ProcessLookupError, OSError):
        pass
    try:
        await process.wait()
    except BaseException:
        logger.debug("回收 ffmpeg 子进程失败", exc_info=True)


def ffmpeg_available() -> bool:
    """本机是否可用 ffmpeg（只看能不能在 PATH 里找到可执行文件）。

    **刻意不做缓存**，包括「缺失」这一侧。缺失结果一旦缓存住，用户中途装好
    ffmpeg 之后插件仍然会一直判定不可用 —— 而它没有任何「需要重启」的提示，
    表现为「装完还是不行」，比不检查更难查。

    代价是一次 ``shutil.which`` 的 PATH 扫描，只在**确实要合并音视频**（B站
    DASH 分离流）时才会走到，非 B站平台与纯图片链路一次都不调用，所以按需
    实时查询完全负担得起。

    不提供「ffmpeg 路径」配置项：这一项解决不了「PATH 里没有」以外的问题，
    而绝大多数用户遇到的就是 PATH 里没有。
    """
    return shutil.which("ffmpeg") is not None


async def exec_ffmpeg_cmd(cmd: list[str]) -> None:
    """执行 ffmpeg 命令。

    取消/退出时**先收掉子进程再向上抛**：ffmpeg 被留在后台会继续写目标文件，
    而调用方紧接着要 unlink 它 —— Windows 上文件被占用会 PermissionError，
    被 ``safe_unlink`` 吞成一条日志，于是「删掉残缺输出」这层保护静默失效。
    """
    logger.debug(f"Executing ffmpeg command: {' '.join(cmd)}")
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
    except FileNotFoundError:
        # 本机没装 ffmpeg（或它不在 PATH 里）。抛专门的类型而不是 RuntimeError：
        # B站的 CDN 重试循环要能认出「换地址也没用」，从而直接打断、不再重下整段视频。
        raise MediaProcessException("ffmpeg 未安装或无法找到可执行文件")

    try:
        _, stderr = await process.communicate()
    except BaseException:
        # 必须写 BaseException：CancelledError 不是 Exception 的子类
        await _terminate_process(process)
        raise
    return_code = process.returncode

    if return_code != 0:
        error_msg = stderr.decode().strip()
        raise RuntimeError(f"ffmpeg 执行失败: {error_msg}")


async def merge_av(
    *,
    v_path: Path,
    a_path: Path,
    output_path: Path,
) -> None:
    """合并视频和音频"""
    logger.info(f"Merging {v_path.name} and {a_path.name} to {output_path.name}")

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(v_path),
        "-i",
        str(a_path),
        "-c",
        "copy",
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        str(output_path),
    ]

    try:
        await exec_ffmpeg_cmd(cmd)
    except BaseException:
        # 失败时删掉可能残留的 0 字节/残缺输出，避免被 encode_video_to_h264
        # 等下游的 exists() 缓存命中当成成品。
        # 捕 BaseException 而不是 RuntimeError：CancelledError 不是 Exception 的子类，
        # 取消时同样会留下残缺输出（与 download.py 的 .part 是同一形状）。
        await safe_unlink(output_path)
        raise
    await asyncio.gather(safe_unlink(v_path), safe_unlink(a_path))
    logger.info(f"Merged {output_path.name}, {fmt_size(output_path)}")


# ==================== m3u8 ====================

# 正常情况下清单最多两层（主列表 → 媒体列表）。给递归解析留三层余量，
# 同时防住「子清单指回自己」造成的死循环。
MAX_M3U8_PLAYLIST_DEPTH = 3

_M3U8_URI_RE = re.compile(r'URI=(?:"([^"]+)"|([^,\s]+))', re.IGNORECASE)
_M3U8_RESOLUTION_RE = re.compile(r"RESOLUTION=(\d+)x(\d+)", re.IGNORECASE)
_M3U8_BANDWIDTH_RE = re.compile(r"(?:AVERAGE-)?BANDWIDTH=(\d+)", re.IGNORECASE)


def parse_m3u8_playlist(
    text: str, base_url: str
) -> tuple[str | None, list[str], list[str]]:
    """解析一份 m3u8 清单。

    Returns:
        ``(init segment URL, 分片 URL 列表, 子清单 URL 列表)``，三者都可能为空。

    三种清单形态都要认得：

    - **媒体列表**：非 ``#`` 开头的行就是分片；
    - **主列表**：``#EXT-X-STREAM-INF`` 后面跟的那一行是**子清单地址**，
      不是分片——当成分片下载会得到一堆「把播放列表当视频内容」的垃圾文件；
    - **fMP4**：``#EXT-X-MAP`` 给出 init segment（moov 头），它必须拼在所有
      分片之前，缺了它整段流是没有 moov 的空壳，播放器只能卡住。
    """
    init_seg: str | None = None
    segments: list[str] = []
    variants: list[tuple[int, int, str]] = []
    pending_stream_inf: str | None = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            upper = line.upper()
            if upper.startswith("#EXT-X-MAP"):
                match = _M3U8_URI_RE.search(line)
                if match:
                    init_seg = urljoin(base_url, match.group(1) or match.group(2))
            elif upper.startswith("#EXT-X-STREAM-INF"):
                pending_stream_inf = line
            continue

        if pending_stream_inf is not None:
            # 主列表：这一行是子清单地址，按 分辨率 → 带宽 取最高的
            resolution = _M3U8_RESOLUTION_RE.search(pending_stream_inf)
            bandwidth = _M3U8_BANDWIDTH_RE.search(pending_stream_inf)
            pixels = (
                int(resolution.group(1)) * int(resolution.group(2))
                if resolution
                else 0
            )
            variants.append(
                (
                    pixels,
                    int(bandwidth.group(1)) if bandwidth else 0,
                    urljoin(base_url, line),
                )
            )
            pending_stream_inf = None
            continue

        segments.append(urljoin(base_url, line))

    ordered_variants = [url for _, _, url in sorted(variants, reverse=True)]
    return init_seg, segments, ordered_variants


async def remux_to_mp4(src: Path, dst: Path) -> bool:
    """把 m3u8 分片直接拼出来的 TS/fMP4 重新封装成规范 MP4（**零转码**）。

    为什么要做：m3u8 分片按顺序拼起来得到的只是个**字节流**，扩展名写成 ``.mp4``
    并不会让它变成 MP4。播放器只能靠猜——宽松的还能播，严格的（尤其电脑版 QQ）
    会直接拒绝，于是「下载成功」却发不出能看的视频。

    ``-c copy`` 只重写容器不重新编码，代价接近于零；``+faststart`` 把 moov 挪到
    文件头，边下边播才能立刻起播。

    失败一律返回 ``False`` 而不抛：调用方拿裸拼接的结果兜底即可，
    总比整个下载判定失败强。没装 ffmpeg 属于常态，不能因此把视频判死。
    """
    logger.info(f"重封装 m3u8 产物: {src.name} -> {dst.name}")
    try:
        await exec_ffmpeg_cmd(
            [
                "ffmpeg", "-y", "-i", str(src),
                "-c", "copy",
                "-movflags", "+faststart",
                str(dst),
            ]
        )
        return True
    except MediaProcessException:
        # 没装 ffmpeg：换环境重试也一样，直接放弃重封装
        logger.debug("未安装 ffmpeg，跳过 m3u8 重封装，使用裸拼接结果")
        await safe_unlink(dst)
        return False
    except RuntimeError as exc:
        logger.warning(f"m3u8 重封装失败，改用裸拼接结果: {exc}")
        await safe_unlink(dst)
        return False
    except BaseException:
        # 取消/退出：把残缺输出清掉再原样抛，取消语义不能被吞
        await safe_unlink(dst)
        raise


async def encode_video_to_h264(video_path: Path) -> Path:
    """将视频重新编码到 h264"""
    output_path = video_path.with_name(f"{video_path.stem}_h264{video_path.suffix}")
    if output_path.exists():
        return output_path
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(video_path),
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "23",
        str(output_path),
    ]
    try:
        await exec_ffmpeg_cmd(cmd)
    except BaseException:
        # 同 merge_av：捕 BaseException，取消时也要清掉残缺输出
        await safe_unlink(output_path)
        raise
    logger.info(f"视频重新编码为 H.264 成功: {output_path}, {fmt_size(output_path)}")
    await safe_unlink(video_path)
    return output_path


async def extract_video_first_frame(video_path: Path) -> Path:
    """从视频中提取第一帧"""
    first_frame_path = video_path.with_suffix(".jpg")
    if first_frame_path.exists():
        return first_frame_path

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(video_path),
        "-ss",
        "00:00:01",
        "-vframes",
        "1",
        str(first_frame_path),
    ]

    await exec_ffmpeg_cmd(cmd)
    return first_frame_path


# ==================== 大视频压缩 ====================

# 压缩是 CPU 密集型转码，与下载的并发闸门（MAX_CONCURRENT_MEDIA_DOWNLOADS）
# 是两回事：下载只占带宽与连接，转码吃满核。给独立的小闸门，让同时来的
# 几条大视频排队转码而不是互相争抢 CPU —— 单实例串行到 2 并发即可。
COMPRESS_CONCURRENCY = 2
_compress_slots = asyncio.Semaphore(COMPRESS_CONCURRENCY)

# 目标体积留 6% 余量：MP4 容器开销 + x264 单遍 ABR 的过冲都吃这点空间，
# 贴着阈值编码很容易压出一个「刚好超一点」的文件，等于白转码一遍。
_COMPRESS_SAFETY = 0.94

# 音频码率取不到时的估算值（抖音音轨普遍 128k AAC）。
_DEFAULT_AUDIO_BPS = 128_000

# 音频最多占总码率预算的比例。**音频优先原流复制**，只有长视频把预算摊薄到
# 「光音轨就装不下」时才按这个上限降码率重编码 —— 体积达标是硬要求，
# 音质是尽量项：326s 的 128k 音轨自己就占 5.2MB，5MB 阈值下复制必然超限。
_AUDIO_BUDGET_RATIO = 0.25
_MIN_AUDIO_BPS = 32_000

# 视频码率下限：低于它画面已经没法看，但**体积达标优先**，所以这个值压得很低，
# 画质由 _target_short_side 降分辨率来兜（360p 承载低码率远比 720p 划算）。
_MIN_VIDEO_BPS = 60_000

# 无时长信息时退回的 CRF 档位（探测失败才会用到）。
_COMPRESS_CRF_STEPS = (25, 30)

# ---- 定质量优先（内容自适应）----
# 实测（真实抖音 720p30 内容，锁同一码率 597k，VMAF 对原片逐帧算）：
#   veryfast 39.08 / medium 43.92 / slow 46.75 / x265-medium 61.46
# 同体积下 preset 就是最大的画质杠杆，slow 比 veryfast 高 7.7 分而只慢 2.4 倍。
# x265 再高 15 分，但 Win10 不带 HEVC 解码、安卓中低端只保 H.264 硬解，
# 发出去可能根本放不出来 —— 所以默认仍是 H.264，H.265 交给用户显式开启。
_COMPRESS_PRESET = "slow"
_COMPRESS_CRF = 23

# 抖音大量 60fps 源，限到 30fps 能省近四成运动码率。**只在源帧率高于 30 时加**：
# 对 24fps 的片子套 fps=30 会插重复帧，白花码率还劣化画质。
_COMPRESS_FPS = 30

# 轻去噪近乎白送（实测 +0.8 VMAF，耗时几乎不变）。nlmeans 慢 4~5 倍，不用。
_COMPRESS_DENOISE = "hqdn3d"

# 抽样估算：在 20%/50%/80% 处各取 6 秒试压，按秒外推全片体积。
# 这是 B站 per-shot 定质量转码 / ab-av1 crf-search 的工程简化版 ——
# 用几秒钱的探针判断「这片子定质量压能不能达标」，避免为难压内容白转一整遍。
_SAMPLE_SECONDS = 6.0
_SAMPLE_POINTS = (0.2, 0.5, 0.8)
# 外推安全系数：抽样点未必覆盖最复杂的段落，估宽一点免得整片压完才发现超标。
_SAMPLE_MARGIN = 1.15

# CRF 每升高这么多，体积约减半。**这是实测拟合值，不是流传的「+6 减半」**：
# 真实抖音内容 crf23->26 体积 ×0.807、crf23->28 ×0.693，反推 k≈9.5。
# 取 9 略偏激进，所以算出的 CRF 还要再抽一次样验证，估不准就退回预算兜底。
_CRF_HALVING = 9.0
# CRF 上限：再高画面就开始明显糊了，宁可交给预算兜底去降分辨率。
_CRF_MAX = 34


def _crf_for_target(est_bytes: int, target_bytes: int, base_crf: int) -> int:
    """由「base_crf 的估算体积」反推「压进 target_bytes 需要的 CRF」。

    守 720p 抬 CRF 通常比降分辨率划算得多：实测 1200s 近静态素材，
    360p/69k 只有 VMAF 57.98，而 720p 定质量能到 96.71。
    """
    if est_bytes <= target_bytes or target_bytes <= 0:
        return base_crf
    delta = _CRF_HALVING * math.log2(est_bytes / target_bytes)
    return min(_CRF_MAX, base_crf + int(math.ceil(delta)))


def _scale_filter(short_side: int, flags: str = "lanczos") -> str:
    """短边压到 ≤short_side，**只缩不放**。

    横屏（iw>ih）短边是高，竖屏短边是宽 —— 抖音常见的 1080x1920 会压成
    720x1280，而不是把宽压到 720 却把高留成 1920。floor(.../2)*2 保证偶数，
    libx264 的 yuv420p 不接受奇数边长。

    降采样用 lanczos：比默认 bicubic 保留更多细节，且只在真的要缩时才生效
    （源就是 720p 时整个 scale 是空操作，flags 无影响）。
    """
    return (
        f"scale='if(gt(iw,ih),-2,floor(min({short_side},iw)/2)*2)':"
        f"'if(gt(iw,ih),floor(min({short_side},ih)/2)*2,-2)':flags={flags}"
    )


def _video_filter(info: "_MediaInfo | None", short_side: int) -> str:
    """拼视频滤镜链，顺序有讲究：fps → 去噪 → 缩放。

    fps 放最前：先砍掉一半帧，后面的去噪和缩放就只处理一半的像素量。
    去噪放在缩放前：噪点在原分辨率上更好分辨，缩完再去噪会糊掉细节。
    """
    parts: list[str] = []
    if info is not None and info.fps > _COMPRESS_FPS + 0.5:
        parts.append(f"fps={_COMPRESS_FPS}")
    parts.append(_COMPRESS_DENOISE)
    parts.append(_scale_filter(short_side))
    return ",".join(parts)


def _target_short_side(video_bps: int) -> int:
    """按视频码率预算挑分辨率：预算够就守住 720p，不够才降。

    720p 是用户要的目标，所以门槛压得很低（300k）—— 只有长视频把预算
    摊到实在撑不起 720p 时才退到 540p / 360p，宁可小一点也不要满屏马赛克。
    """
    if video_bps >= 300_000:
        return 720
    if video_bps >= 150_000:
        return 540
    return 360


class _MediaInfo(NamedTuple):
    duration: float
    audio_bps: int
    fps: float
    width: int
    height: int


def _parse_fps(*raw: object) -> float:
    """ffprobe 的帧率是 "30/1" / "60000/1001" 这种分数字符串，"0/0" 表示未知。"""
    for value in raw:
        if not isinstance(value, str) or "/" not in value:
            continue
        num, _, den = value.partition("/")
        try:
            n, d = float(num), float(den)
        except ValueError:
            continue
        if d > 0 and n > 0:
            return n / d
    return 0.0


async def _probe_media(path: Path) -> _MediaInfo | None:
    """探测时长 / 音频码率 / 帧率 / 分辨率。拿不到时长就返回 None。

    时长是**按体积反推码率**的唯一依据，没有它就只能退回 CRF 档位（而 CRF
    不保证体积上限）。帧率决定要不要限到 30fps，分辨率决定缩放是不是空操作。
    ffprobe 与 ffmpeg 同包发行，缺失属于少数环境。
    """
    if shutil.which("ffprobe") is None:
        return None
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries",
        "format=duration:stream=codec_type,bit_rate,width,height,"
        "r_frame_rate,avg_frame_rate",
        "-of", "json", str(path),
    ]
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
    except (FileNotFoundError, OSError):
        return None
    try:
        stdout, _ = await process.communicate()
    except BaseException:
        await _terminate_process(process)
        raise
    if process.returncode != 0:
        return None
    try:
        info = json.loads(stdout.decode("utf-8", "replace"))
        duration = float(info.get("format", {}).get("duration") or 0)
    except (ValueError, TypeError, AttributeError):
        return None
    if duration <= 0:
        return None
    audio_bps = 0
    fps = width = height = 0
    for stream in info.get("streams") or []:
        kind = stream.get("codec_type")
        if kind == "audio" and not audio_bps:
            try:
                audio_bps = int(stream.get("bit_rate") or 0)
            except (TypeError, ValueError):
                audio_bps = 0
        elif kind == "video" and not width:
            fps = _parse_fps(stream.get("avg_frame_rate"), stream.get("r_frame_rate"))
            try:
                width = int(stream.get("width") or 0)
                height = int(stream.get("height") or 0)
            except (TypeError, ValueError):
                width = height = 0
    return _MediaInfo(
        duration=duration,
        audio_bps=audio_bps or _DEFAULT_AUDIO_BPS,
        fps=fps,
        width=width,
        height=height,
    )


def _encoder_args(codec: str) -> list[str]:
    """编码器与前缀参数。preset 是最大的画质杠杆，见 _COMPRESS_PRESET 处的实测。

    H.265 必须打 ``hvc1`` tag：默认的 ``hev1`` 在苹果系与不少播放器上直接黑屏。
    """
    if codec == "h265":
        return ["-c:v", "libx265", "-preset", "medium", "-tag:v", "hvc1"]
    return ["-c:v", "libx264", "-preset", _COMPRESS_PRESET,
            "-profile:v", "high", "-pix_fmt", "yuv420p"]


async def _compress_encode(
    src: Path,
    dst: Path,
    *,
    vf: str,
    video_args: list[str],
    audio_args: list[str],
    seek: float | None = None,
    seconds: float | None = None,
) -> bool:
    """转码一遍，成功返回 True。失败清掉残缺输出再返回 False。

    ``seek``/``seconds`` 供抽样探针用：``-ss`` 必须在 ``-i`` **之前**才是快速
    关键帧定位，放后面会从头解码到该点，抽 3 段就等于把整片解 3 遍。

    残缺文件必须先清：它顶着 ``.part`` 名不会被当产物复用，但留着会白占磁盘。
    取消（CancelledError 不是 Exception 子类）原样上抛，不能吞。
    """
    cmd = ["ffmpeg", "-y"]
    if seek is not None:
        cmd += ["-ss", f"{seek:.3f}"]
    cmd += ["-i", str(src)]
    if seconds is not None:
        cmd += ["-t", f"{seconds:.3f}"]
    cmd += [
        "-vf", vf,
        *video_args,
        *audio_args,
        "-movflags", "+faststart",
        str(dst),
    ]
    try:
        await exec_ffmpeg_cmd(cmd)
    except Exception:
        await safe_unlink(dst)
        return False
    except BaseException:
        await safe_unlink(dst)
        raise
    return True


async def _estimate_crf_bytes(
    src: Path, info: _MediaInfo, vf: str, encoder: list[str], crf: int
) -> int | None:
    """抽 3 段试压，外推「定质量 CRF 压整片」的体积（含音轨）。探不了返回 None。

    存在的意义：定质量模式体积完全由内容复杂度决定，静态图文视频可能几 MB
    就够、实拍运动画面可能要上百 MB。不先探一下就只能二选一 —— 要么盲目整片
    转一遍（难压内容白等几分钟），要么像旧实现那样按平均预算预判分辨率
    （把本来能守 720p 的静态内容无谓砍到 360p）。几秒钱的探针换掉这个两难。
    """
    tmp = src.with_name(f"{src.stem}.probe.part.mp4")
    sampled_secs = 0.0
    sampled_bytes = 0
    try:
        for point in _SAMPLE_POINTS:
            start = max(0.0, info.duration * point - _SAMPLE_SECONDS / 2)
            seg = min(_SAMPLE_SECONDS, info.duration - start)
            if seg < 1.0:
                continue
            if not await _compress_encode(
                src, tmp, vf=vf,
                video_args=[*encoder, "-crf", str(crf)],
                audio_args=["-an"], seek=start, seconds=seg,
            ):
                return None
            sampled_bytes += tmp.stat().st_size
            sampled_secs += seg
        if sampled_secs <= 0:
            return None
        video_bytes = sampled_bytes / sampled_secs * info.duration
        audio_bytes = info.audio_bps * info.duration / 8
        return int((video_bytes + audio_bytes) * _SAMPLE_MARGIN)
    except OSError:
        return None
    finally:
        await safe_unlink(tmp)


async def compress_video_if_needed(
    video_path: Path, threshold_mb: float, *, codec: str = "h264"
) -> Path:
    """体积超过阈值就压缩，**画质优先**：能定质量达标就不按码率硬砍。

    用途：抖音大视频直发 QQ 常因文件过大被协议端拒绝。阈值由调用方传入
    （配置 ``DOUYIN_COMPRESS_SIZE_MB``，0 = 不压缩）。

    两阶段策略（抄 B站 per-shot 定质量转码的思路，见 ``_estimate_crf_bytes``）：

    1. **定质量优先**：抽样估算 CRF 压整片的体积，估算达标就守 720p 定质量压。
       静态/图文/录屏类内容在这里就能压到几 MB 且画质近乎无损，分辨率一点不丢。
    2. **预算兜底**：估算超标（实拍运动画面）才按「阈值 ÷ 时长」反推码率，用
       maxrate/bufsize 封顶。CRF 只作质量上限（capped CRF）。

    **为什么不能只用其中一种**：CRF 是定质量模式，体积完全由内容复杂度决定，
    **没有任何上限保证** —— 实测一条 142MB / 326s 的 1280x720 抖音原片，
    crf=25 压出来 173MB（比原片还大），crf=30 仍有 115MB。反过来只按平均预算
    预判分辨率也不行：1200s 的静态视频会被摊成 69k 码率 / 360p，而它其实
    定质量守 720p 只要几 MB。**分辨率必须由内容难度决定，不是由时长平均值决定。**

    设计要点：

    - **产物缓存**：输出固定为 ``<原名>_720p.mp4``。同一链接再次解析时，
      原始文件命中下载缓存 → 体积检查 → 压缩产物已存在且达标 → 直接复用。
      原文件保留（不删）：删了会导致下次解析重新走一遍 CDN 下载，还可能撞风控。
      阈值被调低后旧产物不再达标，会被清掉重压，而不是一直发超限文件。
    - **失败降级**：没装 ffmpeg / 转码失败 / 压缩产物反而更大时，一律返回
      原文件 —— 压缩是锦上添花，绝不能让它把「本来能发出去的视频」变成失败。
    - **音频**：``-c:a copy`` 直接复制音频流（抖音音轨是 AAC，MP4 容器装得下），
      音质零损失；个别编码装不进 MP4 时退回 AAC 192k 重编码，仍属高质量。
      音频码率会从总预算里先扣掉，剩下的才分给视频。
    - **codec**：默认 ``h264``（QQ 各端都能播）。``h265`` 同体积画质高约 15 VMAF，
      但 Win10 不自带 HEVC 解码、安卓中低端只保 H.264 硬解，可能放不出来，
      所以只在用户显式开启时才用。

    Returns:
        压缩产物的路径，或（未压缩/压缩失败/产物更大时的）原路径。
    """
    if threshold_mb <= 0:
        return video_path
    try:
        original_size = video_path.stat().st_size
    except OSError:
        return video_path
    limit_bytes = int(threshold_mb * 1024 * 1024)
    if original_size <= limit_bytes:
        return video_path

    output_path = video_path.with_name(f"{video_path.stem}_720p.mp4")
    if output_path.exists():
        try:
            cached_size = output_path.stat().st_size
        except OSError:
            cached_size = 0
        if cached_size <= limit_bytes:
            return output_path
        await safe_unlink(output_path)

    if not ffmpeg_available():
        logger.warning(
            f"[denia_share] 视频 {fmt_size(video_path)} 超过压缩阈值 "
            f"{threshold_mb:g}MB，但未安装 ffmpeg，跳过压缩按原文件发送"
        )
        return video_path

    logger.info(
        f"[denia_share] 视频 {fmt_size(video_path)} 超过阈值 {threshold_mb:g}MB，"
        f"开始压缩到 720p: {video_path.name}"
    )

    probe = await _probe_media(video_path)
    target_bytes = int(limit_bytes * _COMPRESS_SAFETY)
    # 每档产物先留在 .part 名上，最后只提交选中的那一份，其余清掉。
    results: list[tuple[Path, int, str]] = []

    async with _compress_slots:
        # 双重检查：等槽的时候另一条协程可能已经把同一产物转完了
        if output_path.exists():
            return output_path

        async def run(
            vf: str, video_args: list[str], tag: str, audio_variants: list[list[str]]
        ) -> int | None:
            """跑一档（按给定音频参数依次尝试），返回产物体积。"""
            path = output_path.with_name(f"{output_path.stem}.{tag}.part.mp4")
            for audio_args in audio_variants:
                if await _compress_encode(
                    video_path, path, vf=vf,
                    video_args=video_args, audio_args=audio_args,
                ):
                    size = path.stat().st_size
                    results.append((path, size, tag))
                    return size
            logger.warning(
                f"[denia_share] 视频压缩失败（ffmpeg 退出非 0，{tag}）: {video_path.name}"
            )
            return None

        # 音频默认原流复制（音质零损失）；个别编码装不进 MP4 时退回 AAC 192k。
        copy_audio = [["-c:a", "copy"], ["-c:a", "aac", "-b:a", "192k"]]
        encoder = _encoder_args(codec)

        if probe is not None:
            # ---- 阶段1：定质量优先，守 720p，靠抬 CRF 而不是降分辨率达标 ----
            vf720 = _video_filter(probe, 720)
            settled = False
            est = await _estimate_crf_bytes(
                video_path, probe, vf720, encoder, _COMPRESS_CRF
            )
            crf = _COMPRESS_CRF
            if est is not None:
                crf = _crf_for_target(est, target_bytes, _COMPRESS_CRF)
                if crf != _COMPRESS_CRF:
                    # 抬过 CRF 必须重探一次：拟合曲线只是估算，估不准宁可退回
                    # 兜底，也不要整片压完才发现超标 —— 那才是真的白等几分钟。
                    est = await _estimate_crf_bytes(
                        video_path, probe, vf720, encoder, crf
                    )
            if est is not None and est <= target_bytes:
                logger.info(
                    f"[denia_share] 抽样估算 crf{crf} 约 {est / 1048576:.1f}MB "
                    f"可达标，守 720p 定质量压缩: {video_path.name}"
                )
                size = await run(
                    vf720, [*encoder, "-crf", str(crf)], f"crf{crf}", copy_audio
                )
                settled = size is not None and size <= limit_bytes
            elif est is not None:
                logger.info(
                    f"[denia_share] 抽样估算 crf{crf} 约 {est / 1048576:.1f}MB "
                    f"仍超阈值，改按码率预算压缩: {video_path.name}"
                )

            # ---- 阶段2：预算兜底，只有内容确实难压时才走 ----
            if not settled:
                duration, audio_bps = probe.duration, probe.audio_bps
                total_bps = int(target_bytes * 8 / duration)
                # 音频能原流复制就复制（音质零损失）—— 判据是「扣掉整条音轨后，
                # 剩下的预算还够不够撑起一段能看的视频」。只有长视频把预算摊到
                # 连音轨都装不下时，才按比例给音频封顶并降码率重编码。
                if total_bps - audio_bps >= _MIN_VIDEO_BPS:
                    audio_target = audio_bps
                    audio_variants = copy_audio
                    audio_note = f"{audio_bps // 1000}k 原流复制"
                else:
                    audio_target = min(
                        audio_bps,
                        max(int(total_bps * _AUDIO_BUDGET_RATIO), _MIN_AUDIO_BPS),
                    )
                    audio_variants = [["-c:a", "aac", "-b:a", str(audio_target)]]
                    audio_note = (
                        f"{audio_bps // 1000}k -> {audio_target // 1000}k "
                        f"重编码（预算不足）"
                    )
                video_bps = max(total_bps - audio_target, _MIN_VIDEO_BPS)
                logger.info(
                    f"[denia_share] 压缩预算: 时长 {duration:.0f}s / 音频 {audio_note} "
                    f"/ 视频 {video_bps // 1000}k "
                    f"/ 短边 {_target_short_side(video_bps)}"
                )
                # capped CRF —— 质量优先，但 maxrate 给体积封顶。
                size = await run(
                    _video_filter(probe, _target_short_side(video_bps)),
                    [*encoder, "-crf", str(_COMPRESS_CRF),
                     "-maxrate", str(video_bps), "-bufsize", str(video_bps * 2)],
                    "abr", audio_variants,
                )
                if size is not None and size > limit_bytes:
                    # 单遍 ABR 有过冲：按实测比例收紧预算再来一遍纯 ABR。
                    tightened = max(
                        int(video_bps * target_bytes / size * 0.95), _MIN_VIDEO_BPS
                    )
                    logger.info(
                        f"[denia_share] 首档 {size / 1048576:.1f}MB 仍超阈值，"
                        f"收紧视频码率到 {tightened // 1000}k 重压"
                    )
                    await run(
                        _video_filter(probe, _target_short_side(tightened)),
                        [*encoder, "-b:v", str(tightened),
                         "-maxrate", str(tightened), "-bufsize", str(tightened * 2)],
                        "abr2", audio_variants,
                    )
        else:
            # 探测不到时长就没有码率依据，退回 CRF 档位（不保证体积上限）。
            logger.info(
                f"[denia_share] 无法探测视频时长，退回 CRF 档位: {video_path.name}"
            )
            for crf in _COMPRESS_CRF_STEPS:
                size = await run(
                    _video_filter(None, 720), [*encoder, "-crf", str(crf)],
                    f"crf{crf}", copy_audio,
                )
                if size is not None and size <= limit_bytes:
                    break

        # 选产物：优先达标的那一档（先跑的画质更好），都不达标就挑最小的，
        # 但**必须比原文件小**才值得发 —— 否则压缩纯属帮倒忙。
        chosen: tuple[Path, int, str] | None = None
        for item in results:
            if item[1] <= limit_bytes:
                chosen = item
                break
        if chosen is None:
            viable = [item for item in results if item[1] < original_size]
            if viable:
                chosen = min(viable, key=lambda item: item[1])

        for path, _size, _tag in results:
            if chosen is None or path != chosen[0]:
                await safe_unlink(path)

        if chosen is None:
            logger.warning(
                f"[denia_share] 视频压缩失败，按原文件发送: {video_path.name}"
            )
            return video_path

        path, size, tag = chosen
        os.replace(path, output_path)
        if size <= limit_bytes:
            logger.info(
                f"[denia_share] 视频压缩完成: {video_path.name} "
                f"{original_size / 1048576:.1f}MB -> {size / 1048576:.1f}MB ({tag})"
            )
        else:
            logger.warning(
                f"[denia_share] 视频压缩后仍超过阈值 {threshold_mb:g}MB "
                f"({original_size / 1048576:.1f}MB -> {size / 1048576:.1f}MB)，"
                f"发送压缩产物"
            )
        return output_path


def fmt_size(file_path: Path) -> str:
    """格式化文件大小"""
    return f"大小: {file_path.stat().st_size / 1024 / 1024:.2f} MB"


def generate_file_name(url: str, default_suffix: str = "") -> str:
    """根据 url 生成文件名"""
    path = Path(urlparse(url).path)
    suffix = path.suffix if path.suffix else default_suffix
    url_hash = hashlib.md5(url.encode()).hexdigest()[:16]
    file_name = f"{url_hash}{suffix}"
    return file_name


def is_module_available(module_name: str) -> bool:
    """检查模块是否可用"""
    import importlib.util
    return importlib.util.find_spec(module_name) is not None


def is_docker_environment() -> bool:
    """当前是否跑在 Docker 容器里（供 WebUI 与缓存目录提示使用）。"""
    return os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")
