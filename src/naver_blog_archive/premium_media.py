"""Resolve media offered by the ordinary, authorized Premium video player.

Returned playback and subtitle URLs are short-lived credentials. Callers must
keep them in memory only and must not include them in logs or archive metadata.
"""
from __future__ import annotations

import math
import re
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup
import requests

from .addresses import is_premium, post_url
from .premium import PremiumAccessDenied, PremiumLoginRequired, login_state


PLAYBACK_ENDPOINT = 'https://apis.naver.com/neonplayer/vodplay/v3/playback/'
_DASH = 'urn:mpeg:dash:schema:mpd:2011'
_NAVER = 'urn:naver:vod:2020'
_NS = {'d': _DASH, 'n': _NAVER}
_LIMIT = 4 * 1024 * 1024
_VIDEO_ID = re.compile(r'[A-Za-z0-9_-]{8,128}')
_SERVICE = re.compile(r'''window\.__htVodOption\s*=\s*\{[^}]*?\bSERVICE_CODE\s*:\s*["'](\d{1,12})["']''')
_DURATION = re.compile(r'P(?:(\d+(?:\.\d+)?)D)?(?:T(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?)?')


class PremiumMediaError(ValueError):
    """A safe, credential-free explanation suitable for the archive log."""


def media_url_allowed(url: str) -> bool:
    """Only accept HTTPS media hosted on Naver's public static CDN."""
    if not isinstance(url, str) or any(ord(char) < 33 for char in url):
        return False
    try:
        parts = urlsplit(url)
        host = parts.hostname or ''
        return (parts.scheme == 'https' and parts.port in (None, 443)
                and parts.username is None and parts.password is None
                and not parts.fragment and bool(parts.path)
                and (host == 'pstatic.net' or host.endswith('.pstatic.net')))
    except ValueError:
        return False


def _check(control):
    if control:
        control.check()


def _read(client, url, *, control=None, params=None, headers=None) -> str:
    """Bounded, cancellable responses; never follow a credential-bearing URL."""
    _check(control)
    try:
        with client.get(url, params=params, headers=headers, stream=True,
                        allow_redirects=False) as response:
            if response.status_code in (301, 302, 303, 307, 308):
                raise PremiumMediaError('영상 정보를 읽는 중 다른 페이지로 이동했습니다. 로그인 상태를 확인하세요.')
            if response.status_code != 200:
                raise PremiumMediaError(f'영상 정보를 가져오지 못했습니다. HTTP {response.status_code}')
            try:
                size = int(response.headers.get('Content-Length', '0'))
            except (TypeError, ValueError):
                size = 0
            if size > _LIMIT:
                raise PremiumMediaError('영상 정보 응답이 허용 크기를 초과했습니다.')
            data = bytearray()
            for chunk in response.iter_content(64 * 1024):
                _check(control)
                data.extend(chunk)
                if len(data) > _LIMIT:
                    raise PremiumMediaError('영상 정보 응답이 허용 크기를 초과했습니다.')
            _check(control)
            return bytes(data).decode('utf-8-sig')
    except requests.RequestException as exc:
        status = getattr(getattr(exc, 'response', None), 'status_code', None)
        suffix = f' HTTP {status}' if isinstance(status, int) else ''
        raise PremiumMediaError('영상 정보를 가져오지 못했습니다. 로그인 또는 네트워크 상태를 확인하세요.' + suffix) from None
    except UnicodeError:
        raise PremiumMediaError('영상 정보의 문자 인코딩을 읽을 수 없습니다.') from None


def _duration(value):
    match = _DURATION.fullmatch(value or '')
    if not match or not any(match.groups()):
        return None
    result = sum(float(number or 0) * multiplier
                 for number, multiplier in zip(match.groups(), (86400, 3600, 60, 1)))
    return result if math.isfinite(result) and result > 0 else None


def _rank(element):
    try:
        bandwidth = float(element.get('bandwidth', 'inf'))
    except (TypeError, ValueError):
        return math.inf
    return bandwidth if math.isfinite(bandwidth) and bandwidth > 0 else math.inf


def _parse_playback(xml: str, video_id: str) -> dict:
    if '<!DOCTYPE' in xml.upper() or '<!ENTITY' in xml.upper():
        raise PremiumMediaError('지원하지 않는 영상 정보 XML입니다.')
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        raise PremiumMediaError('영상 재생 정보 XML을 읽을 수 없습니다.') from None
    if (root.tag != '{' + _DASH + '}MPD'
            or root.get('{' + _NAVER + '}videoId') != video_id
            or root.get('type') != 'static'):
        raise PremiumMediaError('요청한 영상과 다른 재생 정보가 반환되었습니다.')
    # Refuse protected media before selecting any otherwise usable stream.
    # No license requests or key-system negotiation are performed.
    for element in root.iter():
        if element.tag.rsplit('}', 1)[-1].lower() in (
                'contentprotection', 'drm', 'license', 'licenserequestinfo', 'encryption'):
            raise PremiumMediaError('DRM으로 보호된 영상은 텍스트화할 수 없습니다.')

    captions = []
    for group in root.findall('.//n:SubtitleSet', _NS):
        for subtitle in group.findall('n:Subtitle', _NS):
            url = (subtitle.findtext('n:Source', '', _NS) or '').strip()
            if not media_url_allowed(url):
                continue
            creator = subtitle.find('n:Creator', _NS)
            captions.append({'url': url, 'language': subtitle.get('lang', ''),
                             'format': group.get('type', ''),
                             'automatic': creator is not None and creator.get('type') == 'stt'})

    audio, media = [], []
    for group in root.findall('.//d:AdaptationSet', _NS):
        segmented = any(group.find('d:' + tag, _NS) is not None
                        for tag in ('SegmentTemplate', 'SegmentList', 'SegmentBase'))
        for representation in group.findall('d:Representation', _NS):
            if segmented or any(representation.find('d:' + tag, _NS) is not None
                                for tag in ('SegmentTemplate', 'SegmentList', 'SegmentBase')):
                continue
            mime = representation.get('mimeType') or group.get('mimeType', '')
            url = (representation.findtext('d:BaseURL', '', _NS) or '').strip()
            if not media_url_allowed(url):
                continue
            candidate = (_rank(representation), url)
            if mime in ('audio/mp4', 'audio/mpeg', 'audio/aac', 'audio/webm', 'audio/ogg'):
                audio.append(candidate)
            elif mime == 'video/mp4':
                codecs = representation.get('codecs') or group.get('codecs', '')
                # A video-only MP4 cannot be transcribed. Naver muxed MP4s
                # explicitly advertise their AAC/other audio codec.
                if any(codec in codecs.lower() for codec in ('mp4a', 'aac', 'opus', 'ac-3', 'ec-3')):
                    media.append(candidate)
            elif mime in ('text/vtt', 'application/ttml+xml'):
                captions.append({'url': url, 'language': representation.get('lang') or group.get('lang', ''),
                                 'format': mime, 'automatic': False})
    captions.sort(key=lambda caption: (not caption['language'].lower().startswith('ko'), caption['automatic']))
    result = {'video_id': video_id, 'captions': captions,
              'audio_url': min(audio)[1] if audio else None,
              'media_url': min(media)[1] if media else None,
              'duration': _duration(root.get('mediaPresentationDuration'))}
    if not captions and not audio and not media:
        raise PremiumMediaError('텍스트화할 자막이나 지원되는 음성·영상 파일이 제공되지 않습니다.')
    return result


def resolve_media(client, blog: str, post: str, control=None) -> dict:
    """Refresh permissions and obtain only media exposed to this subscription."""
    if not is_premium(blog):
        raise PremiumMediaError('프리미엄콘텐츠 채널 키가 아닙니다.')
    if not isinstance(post, str) or re.fullmatch(r'[A-Za-z0-9_-]{1,80}', post) is None:
        raise PremiumMediaError('프리미엄콘텐츠 글 번호가 올바르지 않습니다.')
    source = post_url(blog, post)
    html = _read(client, source, control=control)
    if login_state(html) is False:
        raise PremiumLoginRequired('네이버 로그인이 필요하거나 만료되었습니다. 네이버 로그인 연결 후 다시 시작하세요.')
    soup = BeautifulSoup(html, 'html.parser')
    wrappers = soup.select('._VOD_PLAYER_WRAP')
    if len(wrappers) != 1:
        raise PremiumMediaError('프리미엄콘텐츠 영상 재생 영역을 찾지 못했습니다.')
    wrapper = wrappers[0]
    _, owner, channel = blog.split('/')
    if (wrapper.get('data-cp-name') != owner or wrapper.get('data-sub-id') != channel
            or wrapper.get('data-content-id') != post or wrapper.get('data-type') != 'VIDEO'):
        raise PremiumMediaError('요청한 채널·글과 다른 영상이 반환되었습니다.')
    if (wrapper.get('data-content-auth') != 'true' or wrapper.get('data-is-membership') != 'true'
            or wrapper.get('data-is-preview') == 'true'
            or wrapper.select_one('.viewer_paywall, ._VIEWER_VIDEO_PLAYER_PAYWALL') is not None):
        raise PremiumAccessDenied('이 영상의 전체 열람 권한이 없습니다. 구독 상태를 확인하세요.')
    video_id, key = wrapper.get('data-video-id', ''), wrapper.get('data-inkey', '')
    if not _VIDEO_ID.fullmatch(video_id) or not key or len(key) > 8192 or any(ord(c) < 33 for c in key):
        raise PremiumMediaError('전체 영상 재생 정보를 확인할 수 없습니다. 로그인 상태를 확인하세요.')
    services = set()
    for script in soup.find_all('script'):
        if (script.get('src') or script.get('type', '').lower() not in ('', 'text/javascript', 'application/javascript')
                or any(parent.get('id') in ('_VIEWER_VIDEO_CONTENT', '_SE_VIEWER_CONTENT')
                       or any(name in parent.get('class', []) for name in ('_VOD_PLAYER_WRAP', 'se-main-container'))
                       for parent in script.parents)):
            continue
        services.update(_SERVICE.findall(script.string or ''))
    if len(services) != 1:
        raise PremiumMediaError('네이버 영상 플레이어의 서비스 정보를 확인할 수 없습니다.')
    xml = _read(client, PLAYBACK_ENDPOINT + video_id, control=control,
                params={'key': key, 'sid': services.pop(), 'meta': 'Y', 'devt': 'pc'},
                headers={'Accept': 'application/xml', 'Referer': source})
    return _parse_playback(xml, video_id)
