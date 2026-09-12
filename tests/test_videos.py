from dataclasses import replace
from pathlib import Path

import pytest

from naver_blog_archive.config import Config
from naver_blog_archive.progress import OperationCancelled, TaskControl
from naver_blog_archive import videos
from test_archive import FakeClient, Response
from test_video_archive import video_page, KEY
from naver_blog_archive.premium import extract_premium_post


VTT = b'WEBVTT\n\n1\n00:00:01.000 --> 00:00:03.500\nHello <b>world</b> &amp; all\n\n2\n01:02:03.000 --> 01:02:04.000 align:start\nNext line\n'


def test_webvtt_preserves_times_and_decodes_plain_text():
    result = videos.parse_captions(VTT)
    assert result == [{'start': 1.0, 'end': 3.5, 'text': 'Hello world & all'},
                      {'start': 3723.0, 'end': 3724.0, 'text': 'Next line'}]


def test_srt_and_ttml_are_supported():
    assert videos.parse_captions(b'1\r\n00:01:00,123 --> 00:01:04,567\r\ntext\r\n')[0]['start'] == 60.123
    raw = b'<tt xmlns="http://www.w3.org/ns/ttml"><body><p begin="1.5s" dur="2s">hello <span>world</span></p></body></tt>'
    assert videos.parse_captions(raw) == [{'start': 1.5, 'end': 3.5, 'text': 'hello world'}]


@pytest.mark.parametrize('raw', [b'', b'<!DOCTYPE tt [<!ENTITY secret SYSTEM "file:///data">]><tt/>',
    b'<html><body>login required</body></html>', b'WEBVTT\n\n00:00:03.000 --> 00:00:01.000\nbad', b'\xff'])
def test_empty_malformed_or_unsafe_captions_are_rejected(raw):
    with pytest.raises((ValueError, UnicodeError)):
        videos.parse_captions(raw)


def context(tmp_path, monkeypatch, *, captions=True):
    config = Config(KEY[0], tmp_path, delay=0)
    document = extract_premium_post(video_page(), *KEY)
    metadata = {'video_id': document['video_id'], 'captions': [
        {'url': 'https://resources-rmcnmv.pstatic.net/test.vtt', 'language': 'ko-KR', 'automatic': True}] if captions else [],
        'audio_url': None, 'media_url': 'https://vod.pstatic.net/test.mp4'}
    import naver_blog_archive.premium_media as media
    monkeypatch.setattr(media, 'resolve_media', lambda *a, **kw: metadata)
    return config, document, metadata


def test_caption_path_never_loads_or_prepares_local_model(tmp_path, monkeypatch):
    config, document, _ = context(tmp_path, monkeypatch)
    monkeypatch.setattr('naver_blog_archive.transcription.ensure_transcription_ready', lambda: pytest.fail('model'))
    client = FakeClient(data=VTT)
    videos.transcribe_video(config, client, document, TaskControl())
    assert videos.transcript_complete(document)
    assert document['video_transcript']['method'] == 'captions'
    assert '제공된 자동 자막' in document['video_text'] and '**[01:02:03]**' in document['video_text']
    assert len(client.calls) == 1
    videos.transcribe_video(config, client, document, TaskControl())
    assert len(client.calls) == 1


def test_absent_captions_use_local_audio_and_delete_temporary_media(tmp_path, monkeypatch):
    config, document, _ = context(tmp_path, monkeypatch, captions=False)
    paths = []
    monkeypatch.setattr('naver_blog_archive.transcription.ensure_transcription_ready', lambda: None)
    def local(path, control):
        paths.append(path)
        assert path.read_bytes() == b'audio-data'
        return {'method': 'local_whisper', 'model': 'small', 'language': 'ko',
                'segments': [{'start': 0, 'end': 2, 'text': '<script> [text](file) ![img](url)'}]}
    monkeypatch.setattr('naver_blog_archive.transcription.transcribe_audio', local)
    videos.transcribe_video(config, FakeClient(data=b'audio-data'), document, TaskControl())
    assert videos.transcript_complete(document)
    assert all(not path.exists() for path in paths)
    assert '\\<script\\>' in document['video_text']
    assert '\\!\\[img\\]' in document['video_text']


def test_missing_model_does_not_download_large_media(tmp_path, monkeypatch):
    from naver_blog_archive.transcription import TranscriptionNotReady
    config, document, _ = context(tmp_path, monkeypatch, captions=False)
    def missing():
        raise TranscriptionNotReady('준비 필요')
    monkeypatch.setattr('naver_blog_archive.transcription.ensure_transcription_ready', missing)
    client = FakeClient()
    with pytest.raises(TranscriptionNotReady):
        videos.transcribe_video(config, client, document, TaskControl())
    assert client.calls == [] and not videos.transcript_complete(document)


def test_bad_caption_falls_back_to_local_and_empty_audio_is_explicit(tmp_path, monkeypatch):
    config, document, _ = context(tmp_path, monkeypatch)
    monkeypatch.setattr('naver_blog_archive.transcription.ensure_transcription_ready', lambda: None)
    monkeypatch.setattr('naver_blog_archive.transcription.transcribe_audio',
                        lambda *a, **kw: {'method': 'local_whisper', 'model': 'small', 'segments': []})
    client = FakeClient(data=b'not-valid-vtt')
    videos.transcribe_video(config, client, document, TaskControl())
    assert len(client.calls) == 2
    assert '인식된 음성이 없습니다.' in document['video_text']


def test_cancellation_does_not_mark_done_and_removes_media(tmp_path, monkeypatch):
    config, document, _ = context(tmp_path, monkeypatch, captions=False)
    monkeypatch.setattr('naver_blog_archive.transcription.ensure_transcription_ready', lambda: None)
    paths = []
    def cancel(path, control):
        paths.append(path)
        raise OperationCancelled()
    monkeypatch.setattr('naver_blog_archive.transcription.transcribe_audio', cancel)
    with pytest.raises(OperationCancelled):
        videos.transcribe_video(config, FakeClient(data=b'audio'), document, TaskControl())
    assert not videos.transcript_complete(document)
    assert paths and not paths[0].exists()


def test_cancellation_while_reading_caption_does_not_start_fallback(tmp_path, monkeypatch):
    config, document, _ = context(tmp_path, monkeypatch)
    control = TaskControl()
    class Cancelling(FakeClient):
        def get(self, *args, **kwargs):
            control.cancel()
            return Response(data=VTT)
    monkeypatch.setattr('naver_blog_archive.transcription.ensure_transcription_ready', lambda: pytest.fail('fallback'))
    with pytest.raises(OperationCancelled):
        videos.transcribe_video(config, Cancelling(), document, control)


def test_changed_video_identity_and_disabled_option_do_not_download(tmp_path, monkeypatch):
    config, document, metadata = context(tmp_path, monkeypatch)
    metadata['video_id'] = 'CHANGED'
    client = FakeClient()
    with pytest.raises(ValueError, match='영상이 변경'):
        videos.transcribe_video(config, client, document, TaskControl())
    videos.transcribe_video(replace(config, transcribe_videos=False), client, document, TaskControl())
    assert not client.calls


def test_resource_size_and_empty_response_limits():
    with pytest.raises(ValueError, match='크기 제한'):
        videos._read_resource(FakeClient(data=b'12345'), 'https://resources-rmcnmv.pstatic.net/a', TaskControl(), 4)
    with pytest.raises(ValueError, match='비어'):
        videos._read_resource(FakeClient(data=b''), 'https://resources-rmcnmv.pstatic.net/a', TaskControl(), 4)


def test_complete_flag_requires_matching_id_and_saved_text():
    document = {'video_id': 'one', 'video_transcript': {'version': 1, 'status': 'success', 'video_id': 'one'},
                'video_text': 'text'}
    assert videos.transcript_complete(document)
    document['video_id'] = 'two'
    assert not videos.transcript_complete(document)
    document['video_id'], document['video_text'] = 'one', ''
    assert not videos.transcript_complete(document)
