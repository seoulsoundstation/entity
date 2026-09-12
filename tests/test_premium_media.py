from xml.sax.saxutils import escape

import pytest
import requests

from naver_blog_archive.premium import PremiumAccessDenied, PremiumLoginRequired
from naver_blog_archive.premium_media import (
    PLAYBACK_ENDPOINT, PremiumMediaError, _parse_playback, media_url_allowed, resolve_media,
)
from naver_blog_archive.progress import OperationCancelled, TaskControl


BLOG, POST = 'premium/owner/channel', '260911082659513hl'
VID = 'TEST_VIDEO_0123456789'
CDN = 'https://b01-kr-naver-vod.pstatic.net/'
CAPTION = 'https://resources-rmcnmv.pstatic.net/test.vtt?key=TEST_ONLY'


def page(*, auth='true', logged='true', preview='', membership='true', post=POST, service=True):
    settings = '<script>window.__htVodOption = { SERVICE_CODE: "2054" };</script>' if service else ''
    return f'''<script>var isLogin = {logged};</script><div id="ct">
      <div class="_VOD_PLAYER_WRAP" data-cp-name="owner" data-sub-id="channel"
        data-content-id="{post}" data-type="VIDEO" data-content-auth="{auth}"
        data-is-membership="{membership}" data-is-preview="{preview}"
        data-video-id="{VID}" data-inkey="TEST_ONLY_KEY"></div>{settings}</div>'''


def representation(url, *, bandwidth=100, mime='video/mp4', codecs='avc1.42c015,mp4a.40.2', extra=''):
    return f'''<Representation bandwidth="{bandwidth}" mimeType="{mime}" codecs="{codecs}">
      <BaseURL>{escape(url)}</BaseURL>{extra}</Representation>'''


def captions(url=CAPTION, *, language='ko-KR', automatic=True):
    creator = '<n:Creator type="stt"/>' if automatic else '<n:Creator type="user"/>'
    return f'''<n:SubtitleSet type="text/vtt"><n:Subtitle lang="{language}">
      {creator}<n:Source type="string">{escape(url)}</n:Source>
      </n:Subtitle></n:SubtitleSet>'''


def mpd(*, tracks=None, subtitles=None, extra='', video_id=VID, duration='PT15M29.109S'):
    if tracks is None:
        tracks = representation(CDN + 'large.mp4', bandwidth=400) + representation(CDN + 'small.mp4', bandwidth=100)
    if subtitles is None:
        subtitles = captions()
    return f'''<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" xmlns:n="urn:naver:vod:2020"
       n:videoId="{video_id}" type="static" mediaPresentationDuration="{duration}">
       <Period><AdaptationSet mimeType="video/mp4">{tracks}</AdaptationSet></Period>
       {subtitles}{extra}</MPD>'''


class Response:
    def __init__(self, text, status=200, headers=None, chunks=None):
        self.content = text.encode('utf-8')
        self.status_code = status
        self.headers = headers or {}
        self.chunks = chunks
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True

    def iter_content(self, size):
        yield from self.chunks if self.chunks is not None else [self.content]


class Client:
    def __init__(self, *responses):
        self.responses = list(responses or (Response(page()), Response(mpd())))
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def test_real_player_xml_resolves_captions_and_smallest_muxed_mp4():
    client = Client()
    result = resolve_media(client, BLOG, POST)
    assert result == {'video_id': VID, 'captions': [{'url': CAPTION, 'language': 'ko-KR', 'format': 'text/vtt', 'automatic': True}],
                      'audio_url': None, 'media_url': CDN + 'small.mp4', 'duration': 929.109}
    assert client.calls[1][0] == PLAYBACK_ENDPOINT + VID
    assert client.calls[1][1]['params'] == {'key': 'TEST_ONLY_KEY', 'sid': '2054', 'meta': 'Y', 'devt': 'pc'}
    assert all(call[1]['allow_redirects'] is False for call in client.calls)


@pytest.mark.parametrize('kwargs,error', [({'logged': 'false'}, PremiumLoginRequired), ({'auth': 'false'}, PremiumAccessDenied),
                                         ({'preview': 'true'}, PremiumAccessDenied), ({'membership': 'false'}, PremiumAccessDenied),
                                         ({'post': 'other'}, PremiumMediaError), ({'service': False}, PremiumMediaError)])
def test_unavailable_or_mismatched_page_never_requests_playback(kwargs, error):
    client = Client(Response(page(**kwargs)))
    with pytest.raises(error):
        resolve_media(client, BLOG, POST)
    assert len(client.calls) == 1


def test_service_script_inside_authored_content_is_ignored():
    html = page(service=False) + '<div id="_VIEWER_VIDEO_CONTENT"><script>window.__htVodOption={SERVICE_CODE:"2054"};</script></div>'
    client = Client(Response(html))
    with pytest.raises(PremiumMediaError, match='서비스'):
        resolve_media(client, BLOG, POST)
    assert len(client.calls) == 1


@pytest.mark.parametrize('changed', [lambda html: html.replace(VID, '../../other'),
                                    lambda html: html.replace('data-inkey="TEST_ONLY_KEY"', 'data-inkey=""'),
                                    lambda html: html.replace('data-type="VIDEO"', 'data-type="TEXT"')])
def test_invalid_player_credentials_are_not_sent(changed):
    client = Client(Response(changed(page())))
    with pytest.raises(PremiumMediaError):
        resolve_media(client, BLOG, POST)
    assert len(client.calls) == 1


@pytest.mark.parametrize('url', ['http://resources-rmcnmv.pstatic.net/file.vtt', 'https://127.0.0.1/a',
                                'https://pstatic.net.attacker.test/a', 'https://attacker.test/pstatic.net/a',
                                'https://name:secret@resources-rmcnmv.pstatic.net/a',
                                'https://resources-rmcnmv.pstatic.net:444/a', 'file:///C:/secret',
                                'https://resources-rmcnmv.pstatic.net/a\nb', 'https://resources-rmcnmv.pstatic.net/a#token'])
def test_media_host_and_scheme_validation(url):
    assert not media_url_allowed(url)
    with pytest.raises(PremiumMediaError, match='제공되지'):
        _parse_playback(mpd(tracks=representation(url), subtitles=captions(url)), VID)


@pytest.mark.parametrize('marker', ['<ContentProtection/>', '<n:DRM/>', '<n:License/>'])
def test_protected_media_is_rejected_even_with_clear_alternative(marker):
    with pytest.raises(PremiumMediaError, match='DRM'):
        _parse_playback(mpd(extra=marker), VID)


def test_different_video_and_dynamic_stream_are_rejected():
    for xml in (mpd(video_id='ANOTHER_VIDEO'), mpd().replace('type="static"', 'type="dynamic"')):
        with pytest.raises(PremiumMediaError, match='다른 재생 정보'):
            _parse_playback(xml, VID)


def test_audio_only_is_available_independently_of_video():
    audio = representation(CDN + 'audio.m4a', bandwidth=64, mime='audio/mp4', codecs='mp4a.40.2')
    result = _parse_playback(mpd(tracks=audio, subtitles=''), VID)
    assert result['audio_url'] == CDN + 'audio.m4a'
    assert result['media_url'] is None


def test_segmented_or_video_only_tracks_are_not_returned_as_audio_files():
    tracks = representation(CDN + 'silent.mp4', codecs='avc1.42c015')
    tracks += representation(CDN + 'chunks/', extra='<SegmentTemplate media="$Number$.m4s"/>')
    with pytest.raises(PremiumMediaError, match='제공되지'):
        _parse_playback(mpd(tracks=tracks, subtitles=''), VID)


def test_caption_order_prefers_korean_then_human_captions():
    subtitles = captions(language='en-US') + captions(automatic=True) + captions(automatic=False)
    result = _parse_playback(mpd(subtitles=subtitles), VID)
    assert [(c['language'], c['automatic']) for c in result['captions']] == [('ko-KR', False), ('ko-KR', True), ('en-US', True)]


def test_auth_is_refreshed_on_each_call():
    client = Client(Response(page()), Response(mpd()), Response(page(auth='false')))
    resolve_media(client, BLOG, POST)
    with pytest.raises(PremiumAccessDenied):
        resolve_media(client, BLOG, POST)
    assert len(client.calls) == 3


@pytest.mark.parametrize('post', ['../elsewhere', POST + '?key=unexpected', '', None])
def test_invalid_article_id_never_requests_a_page(post):
    client = Client()
    with pytest.raises(PremiumMediaError, match='글 번호'):
        resolve_media(client, BLOG, post)
    assert client.calls == []


def test_signed_url_is_removed_from_network_error():
    error = requests.RequestException('https://apis.naver.com/anything?key=TEST_SECRET')
    client = Client(Response(page()), error)
    with pytest.raises(PremiumMediaError) as failure:
        resolve_media(client, BLOG, POST)
    assert 'TEST_SECRET' not in str(failure.value)
    assert failure.value.__suppress_context__ is True


def test_redirect_response_is_closed_and_not_followed():
    response = Response('', 302, {'Location': 'https://attacker.test/?key=TEST_ONLY'})
    client = Client(Response(page()), response)
    with pytest.raises(PremiumMediaError, match='이동'):
        resolve_media(client, BLOG, POST)
    assert response.closed
    assert len(client.calls) == 2


@pytest.mark.parametrize('declared', [True, False])
def test_metadata_limit_is_enforced_before_or_during_read(declared):
    response = Response('x' * (4 * 1024 * 1024 + 1), headers={'Content-Length': str(4 * 1024 * 1024 + 1)} if declared else {})
    with pytest.raises(PremiumMediaError, match='크기'):
        resolve_media(Client(response), BLOG, POST)
    assert response.closed


def test_cancellation_during_stream_closes_response():
    control = TaskControl()

    def chunks():
        yield b'<html>'
        control.cancel()
        yield b'</html>'

    response = Response('', chunks=chunks())
    with pytest.raises(OperationCancelled):
        resolve_media(Client(response), BLOG, POST, control)
    assert response.closed


@pytest.mark.parametrize('xml', ['not xml', '<!DOCTYPE x [<!ENTITY a "hello">]><MPD/>'])
def test_invalid_and_entity_xml_are_rejected(xml):
    with pytest.raises(PremiumMediaError):
        _parse_playback(xml, VID)


@pytest.mark.parametrize('duration,expected', [('PT1H2M3.5S', 3723.5), ('P1DT1S', 86401), ('PT0S', None), ('PT', None), ('invalid', None)])
def test_duration_parsing(duration, expected):
    assert _parse_playback(mpd(duration=duration), VID)['duration'] == expected
