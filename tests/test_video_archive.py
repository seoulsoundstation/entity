from dataclasses import replace
import json

import pytest

from naver_blog_archive.archive import backup, inspect_archive, reformat_archive
from naver_blog_archive.config import Config
from naver_blog_archive.network import Listing
from naver_blog_archive.premium import PremiumAccessDenied, extract_premium_post
from naver_blog_archive.progress import OperationCancelled
from naver_blog_archive.state import State
from naver_blog_archive.videos import transcript_complete
from test_archive import FakeClient, FakeRenderer, saved_path


BLOG, POST = 'premium/salarymoney/moneystock', '250315075004788ap'
KEY = BLOG, POST


def video_page(*, auth='true', video='VIDEO123', description='설명<br>두 번째 문장'):
    return f'''<script>var isLogin = true;</script>
    <div class="_VOD_PLAYER_WRAP" data-type="VIDEO" data-cp-name="salarymoney"
    data-sub-id="moneystock" data-content-id="{POST}" data-content-auth="{auth}"
    data-video-id="{video}" data-inkey="PRIVATE-PLAYBACK-KEY"></div>
    <div id="_VIEWER_VIDEO_CONTENT"><h2 class="viewer_video_title">영상 제목</h2>
    <span class="viewer_video_meta_text">조회수 500</span>
    <span class="viewer_video_meta_text">2025.03.15. 오전 7:50</span>
    <div class="viewer_video_content_more"><p class="viewer_video_desc">{description}</p>
    <button>더보기</button></div><p>이웃추가</p></div>'''


@pytest.fixture
def config(tmp_path):
    return Config(BLOG, tmp_path / 'archive', delay=0, retries=1)


def record(config):
    state = State(config.out_dir)
    try:
        return dict(state.get(*KEY))
    finally:
        state.close()


def run(config, page=None, **kwargs):
    return backup(config, client=FakeClient(Listing([POST], True, total=1)),
                  renderer=FakeRenderer({KEY: page or video_page()}), **kwargs)


def completed(config, client, document, control):
    document['video_text'] = '## 영상 텍스트\n\n**[00:00:01]** 투자기준을 설명합니다.'
    document['video_transcript'] = {'version': 1, 'status': 'success', 'video_id': document['video_id'],
                                    'method': 'captions'}
    document.pop('video_error', None)


def test_video_extraction_uses_authorized_identity_and_only_description():
    document = extract_premium_post(video_page(), *KEY)
    assert document['content_type'] == 'video' and document['video_id'] == 'VIDEO123'
    assert document['title'] == '영상 제목'
    assert document['published_at'] == '2025-03-15T07:50:00+09:00'
    assert '두 번째 문장' in document['markdown']
    assert all(text not in json.dumps(document) for text in ('PRIVATE-PLAYBACK-KEY', '이웃추가', '더보기'))


def test_video_preview_and_wrong_identity_are_rejected():
    with pytest.raises(PremiumAccessDenied):
        extract_premium_post(video_page(auth='false'), *KEY)
    with pytest.raises(ValueError, match='다른 본문'):
        extract_premium_post(video_page().replace('data-sub-id="moneystock"', 'data-sub-id="other"'), *KEY)


def test_video_with_empty_description_can_still_be_transcribed():
    document = extract_premium_post(video_page(description='<br> '), *KEY)
    assert document['content_type'] == 'video'
    assert '영상 설명이 없습니다.' in document['markdown']


def test_video_text_saves_to_same_markdown_and_searchable_database(config, monkeypatch):
    monkeypatch.setattr('naver_blog_archive.archive.transcribe_video', completed)
    assert run(config)['posts'] == {'success': 1}
    row = record(config)
    text = saved_path(config, KEY).read_text(encoding='utf-8')
    assert '영상 제목' in text and '**[00:00:01]**' in text
    assert '투자기준' in row['body_text']
    assert transcript_complete(json.loads(row['document']))
    assert not inspect_archive(config, verify=True)['verification_problems']


def test_resume_reuses_complete_transcript_without_reading_video(config, monkeypatch):
    monkeypatch.setattr('naver_blog_archive.archive.transcribe_video', completed)
    run(config)
    original = record(config)
    monkeypatch.setattr('naver_blog_archive.archive.transcribe_video', lambda *a: pytest.fail('retranscription'))
    report = backup(config, client=FakeClient(Listing([POST], True)), renderer=FakeRenderer({}))
    assert report['posts'] == {'success': 1}
    assert report['run_counts']['reused'] == 1
    assert record(config)['archive_id'] == original['archive_id']
    assert record(config)['document'] == original['document']
    assert not list((config.out_dir / 'history').rglob('*.md'))


def test_missing_model_saves_description_as_partial_and_retry_completes(config, monkeypatch):
    from naver_blog_archive.transcription import TranscriptionNotReady
    def missing(*args):
        raise TranscriptionNotReady('로컬 음성 인식 준비를 먼저 실행하세요.')
    monkeypatch.setattr('naver_blog_archive.archive.transcribe_video', missing)
    assert run(config)['posts'] == {'partial': 1}
    before = record(config)
    assert '음성 인식 준비' in saved_path(config, KEY).read_text(encoding='utf-8')
    assert inspect_archive(config, verify=True)['posts'] == {'partial': 1}
    assert reformat_archive(config)['posts'] == {'partial': 1}
    monkeypatch.setattr('naver_blog_archive.archive.transcribe_video', completed)
    report = backup(config, client=FakeClient(Listing([POST], True)), renderer=FakeRenderer({}))
    assert report['posts'] == {'success': 1}
    assert record(config)['archive_id'] == before['archive_id']
    assert record(config)['path'] == before['path']


def test_signed_media_errors_are_not_persisted(config, monkeypatch):
    def error(*args):
        raise ValueError('HTTP failed https://media.example/?secret=HIDDENKEY')
    monkeypatch.setattr('naver_blog_archive.archive.transcribe_video', error)
    run(config)
    assert 'HIDDENKEY' not in str(record(config))


def test_off_then_on_processes_existing_video_without_reset(config, monkeypatch):
    from naver_blog_archive.videos import transcribe_video
    monkeypatch.setattr('naver_blog_archive.archive.transcribe_video', transcribe_video)
    assert run(replace(config, transcribe_videos=False))['posts'] == {'success': 1}
    before = record(config)
    monkeypatch.setattr('naver_blog_archive.archive.transcribe_video', completed)
    assert run(config)['posts'] == {'success': 1}
    assert record(config)['archive_id'] == before['archive_id']


def test_refresh_keeps_transcript_for_same_video_and_updates_description(config, monkeypatch):
    monkeypatch.setattr('naver_blog_archive.archive.transcribe_video', completed)
    run(config)
    from naver_blog_archive.videos import transcribe_video
    monkeypatch.setattr('naver_blog_archive.archive.transcribe_video', transcribe_video)
    report = run(config, video_page(description='새 설명'), refresh=True)
    assert report['posts'] == {'success': 1}
    text = saved_path(config, KEY).read_text(encoding='utf-8')
    assert '새 설명' in text and '투자기준' in text
    assert text.count('## 영상 텍스트') == 1


def test_reformat_restores_completed_video_offline_and_preserves_user_edits(config, monkeypatch):
    monkeypatch.setattr('naver_blog_archive.archive.transcribe_video', completed)
    run(config)
    path = saved_path(config, KEY)
    original = path.read_bytes()
    path.unlink()
    assert reformat_archive(config)['posts'] == {'success': 1}
    assert path.read_bytes() == original
    path.write_text('내 메모', encoding='utf-8')
    assert run(config)['posts'] == {'partial': 1}
    assert path.read_text(encoding='utf-8') == '내 메모'


def test_cancel_preserves_video_document_and_resume(config, monkeypatch):
    def cancel(*args):
        raise OperationCancelled()
    monkeypatch.setattr('naver_blog_archive.archive.transcribe_video', cancel)
    with pytest.raises(OperationCancelled):
        run(config)
    row = record(config)
    assert row['status'] == 'pending' and row['document']
    monkeypatch.setattr('naver_blog_archive.archive.transcribe_video', completed)
    assert run(config)['posts'] == {'success': 1}


def test_cancel_immediately_after_transcription_reuses_completed_result(config, monkeypatch):
    def finish_then_cancel(config, client, document, control):
        completed(config, client, document, control)
        control.cancel()
    monkeypatch.setattr('naver_blog_archive.archive.transcribe_video', finish_then_cancel)
    with pytest.raises(OperationCancelled):
        run(config)
    assert transcript_complete(json.loads(record(config)['document']))
    from naver_blog_archive.videos import transcribe_video
    monkeypatch.setattr('naver_blog_archive.archive.transcribe_video', transcribe_video)
    assert run(config)['posts'] == {'success': 1}


@pytest.mark.parametrize('error', [ValueError('temporary network error'), OperationCancelled()])
def test_failed_refresh_preserves_expensive_transcript_and_retries_metadata(config, monkeypatch, error):
    monkeypatch.setattr('naver_blog_archive.archive.transcribe_video', completed)
    run(config)
    original = json.loads(record(config)['document'])['video_text']
    if isinstance(error, OperationCancelled):
        with pytest.raises(OperationCancelled):
            run(config, error, refresh=True)
    else:
        assert run(config, error, refresh=True)['posts'] == {'partial': 1}
    document = json.loads(record(config)['document'])
    assert document['video_text'] == original and document['video_refresh_pending']
    assert inspect_archive(config, verify=True)['posts'] == {'partial': 1}
    from naver_blog_archive.videos import transcribe_video
    monkeypatch.setattr('naver_blog_archive.archive.transcribe_video', transcribe_video)
    assert run(config, video_page(description='갱신된 영상 설명'))['posts'] == {'success': 1}
    document = json.loads(record(config)['document'])
    assert document['video_text'] == original and not document.get('video_refresh_pending')
    assert '갱신된 영상 설명' in document['markdown']
