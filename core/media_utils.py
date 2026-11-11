"""媒体与文件工具函数

涵盖缓存清理、ffmpeg 处理（音视频合并 / 转码 / 抽帧 / 转 GIF）、
文件名生成，以及时长格式化。
"""

import os
import re
import asyncio
import hashlib
import shutil
import time
from pathlib import Path
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

# 720p 目标：**短边压到 ≤720** —— 横屏把高度压到 ≤720（如 1920x1080 → 1280x720），
# 竖屏把宽度压到 ≤720（抖音常见的 1080x1920 → 720x1280）。
# min(...) 保证**只缩不放**：源本来就低于 720p 时保持原分辨率。
_COMPRESS_VF = (
    "scale='if(gt(iw,ih),-2,floor(min(720,iw)/2)*2)':"
    "'if(gt(iw,ih),floor(min(720,ih)/2)*2,-2)'"
)

# 逐级加压的 CRF 档位。先给视觉上几乎无损的 25，仍超阈值再退到 30；
# 再往上画质损失就明显了 —— 两轮之后宁可发一个大一点的文件，也不把
# 糊成马赛克的视频塞给用户。
_COMPRESS_CRF_STEPS = (25, 30)


async def compress_video_if_needed(video_path: Path, threshold_mb: float) -> Path:
    """体积超过阈值的视频压到 720p（libx264），**音频不重编码**以保留音质。

    用途：抖音大视频直发 QQ 常因文件过大被协议端拒绝。阈值由调用方传入
    （配置 ``DOUYIN_COMPRESS_SIZE_MB``，0 = 不压缩）。

    设计要点：

    - **产物缓存**：输出固定为 ``<原名>_720p.mp4``。同一链接再次解析时，
      原始文件命中下载缓存 → 体积检查 → 压缩产物已存在 → 直接复用，
      不会重复转码，也不用重新下载。原文件保留（不删）：删了会导致下次
      解析重新走一遍 CDN 下载，还可能撞上风控。
    - **失败降级**：没装 ffmpeg / 转码失败 / 压缩产物反而更大时，一律返回
      原文件 —— 压缩是锦上添花，绝不能让它把「本来能发出去的视频」变成失败。
    - **音频**：``-c:a copy`` 直接复制音频流（抖音音轨是 AAC，MP4 容器装得下），
      音质零损失；个别编码装不进 MP4 时退回 AAC 192k 重编码，仍属高质量。

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
        return output_path

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

    best_path: Path | None = None
    best_size = original_size
    async with _compress_slots:
        # 双重检查：等槽的时候另一条协程可能已经把同一产物转完了
        if output_path.exists():
            return output_path
        for crf in _COMPRESS_CRF_STEPS:
            # 每档写**独立临时名**：失败只清自己的残缺输出，不会毁掉上一档
            # 已经成功（只是仍超限）的产物；正式名只在确定「这是当前最优」时提交。
            attempt_path = output_path.with_name(
                f"{output_path.stem}.crf{crf}.part.mp4"
            )
            encoded = False
            for audio_args in (("-c:a", "copy"), ("-c:a", "aac", "-b:a", "192k")):
                cmd = [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(video_path),
                    "-vf",
                    _COMPRESS_VF,
                    "-c:v",
                    "libx264",
                    "-preset",
                    "medium",
                    "-crf",
                    str(crf),
                    *audio_args,
                    "-movflags",
                    "+faststart",
                    str(attempt_path),
                ]
                try:
                    await exec_ffmpeg_cmd(cmd)
                except Exception:
                    # 音频流 copy 不兼容（少见编码装不进 MP4）或转码失败：
                    # 清掉残缺输出，换下一组音频参数重试。必须先清输出 ——
                    # 残缺文件顶着正式名会被 exists() 当成有效产物复用。
                    await safe_unlink(attempt_path)
                except BaseException:
                    # CancelledError 等非 Exception 子类：清掉残缺输出后原样抛，
                    # 取消语义不能被吞（与 merge_av / encode_video_to_h264 同形）。
                    await safe_unlink(attempt_path)
                    raise
                else:
                    encoded = True
                    break
            if not encoded:
                # 该档位两组音频参数都失败：试下一档，档位用尽后按原文件发送
                logger.warning(
                    f"[denia_share] 视频压缩失败（ffmpeg 退出非 0，crf={crf}），"
                    f"尝试下一档位或按原文件发送: {video_path.name}",
                    exc_info=True,
                )
                continue

            size = attempt_path.stat().st_size
            if size <= limit_bytes:
                os.replace(attempt_path, output_path)
                logger.info(
                    f"[denia_share] 视频压缩完成: {video_path.name} "
                    f"{original_size / 1024 / 1024:.1f}MB -> "
                    f"{size / 1024 / 1024:.1f}MB (crf={crf})"
                )
                return output_path
            if size < best_size:
                best_size, best_path = size, attempt_path
            logger.info(
                f"[denia_share] crf={crf} 压缩后仍为 "
                f"{size / 1024 / 1024:.1f}MB（阈值 {threshold_mb:g}MB），"
                f"尝试更激进的压缩档位"
            )

        # 所有档位都没压进阈值：提交体积更小的那份产物（通常仍是压缩产物），
        # 并如实告知 —— 用户需要知道为什么发出来的还是大文件。
        if best_path is not None:
            os.replace(best_path, output_path)
            logger.warning(
                f"[denia_share] 视频压缩后仍超过阈值 {threshold_mb:g}MB "
                f"({original_size / 1024 / 1024:.1f}MB -> "
                f"{best_size / 1024 / 1024:.1f}MB)，发送压缩产物"
            )
            return output_path
        logger.warning(
            f"[denia_share] 视频压缩失败，按原文件发送: {video_path.name}"
        )
        return video_path


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
