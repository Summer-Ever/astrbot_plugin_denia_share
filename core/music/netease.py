"""网易云音乐适配器：搜索、单曲详情与带登录态的取流。"""

from __future__ import annotations

from typing import Any, Final
from urllib.parse import urljoin, urlsplit

import httpx

from .client import (
    BaseProvider,
    as_dict,
    as_list,
    cookie_value,
    fetch_error,
    float_of,
    int_of,
    safe_media_url,
    text_of,
)
from .crypto import music_u_of, weapi_form
from .models import MusicError, PlayURL, Song

SEARCH_URL: Final[str] = "https://music.163.com/api/search/get/web"
DETAIL_URL: Final[str] = "https://music.163.com/api/song/detail"
LYRIC_URL: Final[str] = "https://music.163.com/api/song/lyric"
OUTER_URL: Final[str] = "https://music.163.com/song/media/outer/url"
WEAPI_PLAY_URL: Final[str] = "https://music.163.com/weapi/song/enhance/player/url/v1"
WEAPI_ACCOUNT_URL: Final[str] = "https://music.163.com/weapi/w/nuser/account/get"
BASE_URL: Final[str] = "https://music.163.com/"
SONG_URL: Final[str] = "https://music.163.com/song?id={}"

HEADERS: Final[dict[str, str]] = {"Referer": BASE_URL}
# 点歌音质（MUSIC_QUALITY）：设定的是「尝试的最高档」，拿不到自动降一档。
# high 与升级前行为一致（默认）；无损需要账号权益，匿名只拿得到 standard。
QUALITY_TIERS: Final[tuple[str, ...]] = ("lossless", "exhigh", "standard")
_QUALITY_TOP: Final[dict[str, str]] = {
    "standard": "standard",
    "high": "exhigh",
    "lossless": "lossless",
}


def _quality_tiers() -> tuple[str, ...]:
    """按配置的音质档返回尝试顺序：所选档优先，逐级降档兜底。"""
    from ..config import get_config

    value = str(get_config().current_values().get('MUSIC_QUALITY', 'high') or 'high')
    top = _QUALITY_TOP.get(value.strip().lower(), "exhigh")
    return QUALITY_TIERS[QUALITY_TIERS.index(top):]

DEFAULT_TITLE: Final[str] = "未知歌曲"
DEFAULT_ARTIST: Final[str] = "未知歌手"
# fee=1/4 表示付费/会员曲目，其余（0/8）可以匿名拿到试听或全曲
PAID_FEES: Final[frozenset[int]] = frozenset({1, 4})


def _artist_names(node: Any) -> str:
    """``artists`` / ``ar`` 两种字段形态都接受。"""
    names = []
    for raw in as_list(node):
        item = as_dict(raw)
        name = text_of(item.get("name"))
        if name:
            names.append(name)
    return "/".join(names)


def _parse_song(raw: Any) -> Song | None:
    """把一首歌的两种字段形态（完整版 / 精简版）统一成 :class:`Song`。"""
    item = as_dict(raw)
    song_id = item.get("id")
    if song_id is None:
        return None
    value = str(song_id).strip()
    if not value or not value.isdigit() or int(value) <= 0:
        return None
    album = as_dict(item.get("album") or item.get("al"))
    duration_ms = int_of(item.get("duration") or item.get("dt"))
    return Song(
        platform="netease",
        id=value,
        title=text_of(item.get("name"), DEFAULT_TITLE),
        artist=_artist_names(item.get("artists") or item.get("ar")) or DEFAULT_ARTIST,
        album=text_of(album.get("name")),
        duration=round(duration_ms / 1000, 3) if duration_ms > 0 else 0.0,
        cover=safe_media_url(album.get("picUrl")),
        url=SONG_URL.format(value),
    )


def _collect_songs(node: Any) -> list[Song]:
    songs: list[Song] = []
    for raw in as_list(node):
        song = _parse_song(raw)
        if song is not None:
            songs.append(song)
    return songs


class NeteaseProvider(BaseProvider):
    """网易云音乐：匿名可搜可看详情，取流优先走登录态 weapi。"""

    platform = "netease"

    # -- 内部 ---------------------------------------------------------------
    def _login_cookie(self) -> str:
        return self.cookie if music_u_of(self.cookie) else ""

    def _headers(self) -> dict[str, str]:
        headers = dict(HEADERS)
        if self.cookie:
            headers["Cookie"] = self.cookie
        return headers

    def _song_or_error(self, song: Any) -> Song:
        """接受 :class:`Song`、字典或带属性的对象，统一取出平台与 id。"""
        if isinstance(song, Song):
            return song
        if isinstance(song, dict):
            value = song
        else:
            value = {
                "platform": getattr(song, "platform", "netease"),
                "id": getattr(song, "id", ""),
                "title": getattr(song, "title", ""),
                "duration": getattr(song, "duration", 0.0),
            }
        song_id = str(value.get("id") or "").strip()
        if not song_id.isdigit() or int(song_id) <= 0:
            raise MusicError("网易云歌曲 id 无效，请重新搜索后再试")
        duration = value.get("duration") or 0.0
        return Song(
            platform="netease",
            id=song_id,
            title=text_of(value.get("title"), DEFAULT_TITLE),
            artist=text_of(value.get("artist"), DEFAULT_ARTIST),
            album=text_of(value.get("album")),
            duration=float(duration) if isinstance(duration, (int, float)) else 0.0,
            cover=safe_media_url(value.get("cover")),
            url=SONG_URL.format(song_id),
        )

    @staticmethod
    def _play_item(payload, song):
        for raw in as_list(as_dict(payload).get('data')):
            item = as_dict(raw)
            if str(item.get('id', '')) != song.id:
                continue
            url = safe_media_url(item.get('url'))
            if not url:
                continue
            trial = as_dict(item.get('freeTrialInfo'))
            duration = float_of(item.get('time')) / 1000
            if trial:
                # freeTrialInfo uses seconds; player.time uses milliseconds.
                span = float_of(trial.get('end')) - float_of(trial.get('start'))
                return PlayURL(url=url, duration=span if span > 0 else duration,
                               preview=True, message='当前仅可获取试听片段')
            return PlayURL(url=url, duration=duration or song.duration)
        return None

    async def _weapi_play(self, song: Song) -> PlayURL | None:
        """Try account-authorized playback before any anonymous fallback."""
        cookie = self._login_cookie()
        if not cookie:
            return None
        headers = {"Referer": BASE_URL, "Cookie": cookie}
        for level in _quality_tiers():
            form = weapi_form(
                {
                    "ids": f"[{song.id}]",
                    "level": level,
                    "encodeType": "mp3",
                    "csrf_token": cookie_value(cookie, '__csrf'),
                }
            )
            payload = await self._request_json(
                "POST", WEAPI_PLAY_URL, headers=headers, data=form, what="网易云取流"
            )
            playable = self._play_item(payload, song)
            if playable:
                return playable
        return None

    async def _public_play(self, song):
        attempts = [
            ('https://music.163.com/api/song/enhance/player/url/v1',
             {'ids':f'[{song.id}]', 'level':'standard', 'encodeType':'mp3'}),
            ('https://music.163.com/api/song/enhance/player/url',
             {'ids':f'[{song.id}]', 'br':128000}),
        ]
        for url, params in attempts:
            try:
                payload = await self._request_json('GET', url, headers=self._headers(), params=params, what='网易云取流')
                playable = self._play_item(payload, song)
                if playable:
                    return playable
            except MusicError:
                continue
        return None

    async def _outer_url(self, song_id: str) -> str:
        """公开外链：302 到 CDN 或 200 直接回流才算可播放。"""
        target = f"{OUTER_URL}?id={song_id}.mp3"
        try:
            async with self.client(follow_redirects=False) as client:
                async with client.stream('GET', target, headers=self._headers()) as response:
                    status = response.status_code
                    if 300 <= status < 400:
                        location = safe_media_url(urljoin(target, response.headers.get('location', '')))
                        host = (urlsplit(location).hostname or '').lower()
                        if location and '404' not in urlsplit(location).path and (host == 'music.126.net' or host.endswith('.music.126.net')):
                            return location
                    if status == 200 and response.headers.get('content-type', '').lower().startswith('audio/'):
                        return target
                    return ''
        except Exception as exc:  # noqa: BLE001 - 网络异常统一收窄
            raise fetch_error(exc) from None

    # -- 对外 ---------------------------------------------------------------
    async def search(self, keyword: str, limit: int = 5) -> list[Song]:
        query = str(keyword or "").strip()
        if not query:
            return []
        size = max(1, min(int(limit or 5), 10))
        params = {"s": query, "type": 1, "offset": 0, "limit": size, "total": "true"}
        payload = await self._request_json(
            "GET", SEARCH_URL, headers=self._headers(), params=params, what="网易云搜索"
        )
        songs = _collect_songs(as_dict(as_dict(payload).get("result")).get("songs"))[:size]
        if not songs:
            return []
        try:
            await self._fill_covers(songs)
        except MusicError:
            # 封面只是装饰，补图失败不该让整次搜索失败
            pass
        return songs

    async def _fill_covers(self, songs: list[Song]) -> None:
        ids = ",".join(song.id for song in songs if song.id)
        if not ids:
            return
        payload = await self._request_json(
            "GET",
            DETAIL_URL,
            headers=self._headers(),
            params={"ids": f"[{ids}]"},
            what="网易云歌曲详情",
        )
        covers: dict[str, str] = {}
        for raw in as_list(as_dict(payload).get("songs")):
            item = as_dict(raw)
            song_id = str(item.get("id") or "").strip()
            cover = safe_media_url(as_dict(item.get("album")).get("picUrl"))
            if song_id and cover:
                covers[song_id] = cover
        for song in songs:
            if not song.cover and song.id in covers:
                song.cover = covers[song.id]

    async def detail(self, identifier: str) -> Song:
        song_id = str(identifier or "").strip()
        if not song_id.isdigit() or int(song_id) <= 0:
            raise MusicError("网易云歌曲 id 无效，请检查分享链接")
        payload = await self._request_json(
            "GET",
            DETAIL_URL,
            headers=self._headers(),
            params={"ids": f"[{song_id}]"},
            what="网易云歌曲详情",
        )
        for song in _collect_songs(as_dict(payload).get("songs")):
            return song
        raise MusicError("未能获取这首歌的信息，可能已下架或需要会员")

    async def play(self, song: Song) -> PlayURL:
        target = self._song_or_error(song)
        try:
            playable = await self._weapi_play(target)
            if playable:
                return playable
        except MusicError:
            pass
        playable = await self._public_play(target)
        if playable:
            return playable
        try:
            outer = await self._outer_url(target.id)
        except MusicError:
            outer = ''
        if outer:
            # 外链固定 128kbps，未登录时只能拿到免费曲目
            return PlayURL(url=outer, duration=target.duration, preview=False)
        if not self._login_cookie():
            return PlayURL(message='暂未取得音频；可登录账号后重试，部分歌曲需要会员或单独购买')
        return PlayURL(message='当前账号未取得这首歌的播放权限，可能受到会员或版权限制')

    async def check_login(self) -> dict[str, str]:
        if not self._login_cookie():
            return {"status": "invalid", "message": "未配置网易云 Cookie"}
        form = weapi_form({"csrf_token": ""})
        payload = await self._request_json(
            "POST",
            WEAPI_ACCOUNT_URL,
            headers={"Referer": BASE_URL, "Cookie": self._login_cookie()},
            data=form,
            what="网易云账号状态",
        )
        body = as_dict(payload)
        account = body.get("account")
        if int_of(body.get("code"), -1) == 200 and isinstance(account, dict) and int_of(account.get('id')) > 0:
            return {"status": "valid", "message": "网易云 Cookie 有效"}
        if int_of(body.get('code'), -1) in {301, 401, -101}:
            return {"status": "invalid", "message": "网易云 Cookie 已失效，请重新登录"}
        return {"status": "unknown", "message": "暂时无法确认网易云登录状态，请稍后重试"}

    async def lyric(self, song: Song) -> str:
        """取纯文本歌词；没有歌词时返回空串。"""
        target = self._song_or_error(song)
        payload = await self._request_json(
            "GET",
            LYRIC_URL,
            headers=self._headers(),
            params={"id": target.id, "lv": 1, "kv": 1, "tv": -1},
            what="网易云歌词",
        )
        return text_of(as_dict(as_dict(payload).get("lrc")).get("lyric"))


__all__ = ["NeteaseProvider", "SONG_URL"]
