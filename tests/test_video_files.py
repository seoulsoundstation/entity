from dataclasses import replace
import hashlib
import json

import pytest

from naver_blog_archive.archive import backup, inspect_archive
from naver_blog_archive.network import Listing
from naver_blog_archive.premium import PremiumLoginRequired
from naver_blog_archive.premium_media import _parse_playback
from naver_blog_archive.state import State
from naver_blog_archive.videos import video_asset_key
from test_archive import FakeClient, FakeRenderer, saved_path
from test_premium_media import CDN, VID, mpd, representation
from test_video_archive import KEY, POST, completed, video_page


MP4 = b'\x00\x00\x00\x18ftypmp42' + b'\x00' * 12 + b'movie-data' * 100
BEST = CDN + 'best.mp4?key=SIGNED_SECRET'


@pytest.fixture
def config(tmp_path):
    from naver_blog_archive.config import Config
    return Config(KEY[0], tmp_path / 'archive', delay=0, retries=1, download_videos=True)


@pytest.fixture(autouse=True)
def transcript(monkeypatch):
    monkeypatch.setattr('naver_blog_archive.archive.transcribe_video', completed)


def media(monkeypatch, result=None):
    calls = []
    def resolve(client, blog, post, control=None):
        calls.append((blog, post))
        if isinstance(result, BaseException):
            raise result
        return result or {'video_id': 'VIDEO123', 'video_url': BEST}
    monkeypatch.setattr('naver_blog_archive.premium_media.resolve_media', resolve)
    return calls


def run(config, data=MP4, renderer=None, **kwargs):
    client = FakeClient(Listing([POST], True, total=1), data)
    report = backup(config, client=client, renderer=renderer or FakeRenderer({KEY: video_page()}), **kwargs)
    return report, client


def asset(config):
    state = State(config.out_dir)
    try:
        document = json.loads(state.get(*KEY)['document'])
        row = state.asset(video_asset_key(document))
        return dict(row) if row else None
    finally:
        state.close()


def test_best_quality_muxed_mp4_is_selected_and_unknown_bandwidth_ranks_last():
    tracks = (representation(CDN + 'small.mp4', bandwidth=100) + representation(CDN + 'large.mp4', bandwidth=900)
              + representation(CDN + 'unknown.mp4', bandwidth='') + representation(CDN + 'silent.mp4', bandwidth=5000, codecs='avc1.42c015'))
    result = _parse_playback(mpd(tracks=tracks), VID)
    assert result['video_url'] == CDN + 'large.mp4'
    assert result['media_url'] == CDN + 'small.mp4'


def test_video_file_option_is_off_by_default(config, monkeypatch):
    calls = media(monkeypatch)
    report, client = run(replace(config, download_videos=False))
    assert report['posts'] == {'success': 1}
    assert not calls and not client.calls
    assert '동영상 파일' not in saved_path(config, KEY).read_text(encoding='utf-8')
    assert not (config.out_dir / 'attachments' / 'videos').exists()


def test_video_file_is_saved_linked_and_verified(config, monkeypatch):
    media(monkeypatch)
    report, client = run(config)
    assert report['posts'] == {'success': 1}
    assert client.calls == [BEST]
    checksum = hashlib.sha256(MP4).hexdigest()
    saved = config.out_dir / 'attachments' / 'videos' / checksum / '영상 제목.mp4'
    assert saved.read_bytes() == MP4
    text = saved_path(config, KEY).read_text(encoding='utf-8')
    assert '[동영상 파일: 영상 제목.mp4](' in text
    assert f'attachments/videos/{checksum}/%EC%98%81%EC%83%81%20%EC%A0%9C%EB%AA%A9.mp4)' in text
    assert text.index('동영상 파일:') < text.index('## 영상 텍스트')
    assert asset(config)['status'] == 'success'
    assert not list((config.out_dir / 'attachments' / 'videos').glob('.download-*'))
    assert not inspect_archive(config, verify=True)['verification_problems']
    assert 'SIGNED_SECRET' not in (config.out_dir / 'archive_state.sqlite').read_bytes().decode('latin-1')


def test_resume_reuses_saved_video_without_network(config, monkeypatch):
    media(monkeypatch)
    run(config)
    calls = media(monkeypatch, AssertionError('must not resolve again'))
    report = backup(config, client=FakeClient(Listing([POST], True)), renderer=FakeRenderer({}))
    assert report['posts'] == {'success': 1}
    assert report['run_counts']['reused'] == 1
    assert not calls


def test_missing_video_file_is_reported_and_downloaded_again(config, monkeypatch):
    media(monkeypatch)
    run(config)
    path = config.out_dir / asset(config)['path']
    path.unlink()
    problems = inspect_archive(config, verify=True)['verification_problems']
    assert any('동영상 파일' in problem for problem in problems)
    report, _ = run(config)
    assert report['posts'] == {'success': 1}
    assert path.read_bytes() == MP4


def test_enabling_option_later_saves_existing_video(config, monkeypatch):
    calls = media(monkeypatch)
    run(replace(config, download_videos=False))
    assert not calls
    report, _ = run(config, renderer=FakeRenderer({}))
    assert report['posts'] == {'success': 1}
    assert calls and asset(config)['status'] == 'success'


def test_video_is_saved_even_when_transcription_is_off(config, monkeypatch):
    media(monkeypatch)
    report, _ = run(replace(config, transcribe_videos=False))
    assert report['posts'] == {'success': 1}
    assert '동영상 파일: 영상 제목.mp4' in saved_path(config, KEY).read_text(encoding='utf-8')


@pytest.mark.parametrize('data, result, message', [
    (MP4, {'video_id': 'VIDEO123', 'video_url': None}, '일반 MP4'),
    (b'<html>login</html>', None, 'MP4 동영상 대신'),
    (MP4, {'video_id': 'OTHER_VIDEO', 'video_url': BEST}, '영상이 변경'),
])
def test_unusable_video_is_partial_and_retried(config, monkeypatch, data, result, message):
    media(monkeypatch, result)
    report, _ = run(config, data)
    assert report['posts'] == {'partial': 1}
    assert message in saved_path(config, KEY).read_text(encoding='utf-8')
    assert message in asset(config)['error']
    assert not list((config.out_dir / 'attachments' / 'videos').rglob('*.mp4'))
    media(monkeypatch)
    report, _ = run(config, renderer=FakeRenderer({}))
    assert report['posts'] == {'success': 1}


def test_size_limit_rejects_large_video(config, monkeypatch):
    media(monkeypatch)
    report, _ = run(replace(config, max_video_mb=1), MP4 + b'x' * (1024 * 1024))
    assert report['posts'] == {'partial': 1}
    assert '크기 제한' in asset(config)['error']
    assert not list((config.out_dir / 'attachments' / 'videos').iterdir())


def test_signed_url_in_errors_is_not_persisted(config, monkeypatch):
    media(monkeypatch, ValueError('failed ' + BEST))
    report, _ = run(config)
    assert report['posts'] == {'partial': 1}
    assert 'SIGNED_SECRET' not in str(asset(config))
    assert 'SIGNED_SECRET' not in saved_path(config, KEY).read_text(encoding='utf-8')


def test_expired_login_stops_backup(config, monkeypatch):
    media(monkeypatch, PremiumLoginRequired('네이버 로그인이 필요합니다.'))
    with pytest.raises(PremiumLoginRequired):
        run(config)
