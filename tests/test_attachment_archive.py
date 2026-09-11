from dataclasses import replace
import json
from urllib.parse import quote, unquote

from bs4 import BeautifulSoup
import pytest
import requests

from naver_blog_archive.archive import (
    backup, file_asset_key, format_note, inspect_archive, reformat_archive, write_note,
)
from naver_blog_archive.config import Config
from naver_blog_archive.network import Listing, original_image_url, post_url
from naver_blog_archive.parser import extract_post
from naver_blog_archive.reader import create_preview
from naver_blog_archive.state import State
from test_archive import FakeClient, FakeRenderer, MAIN, PHOTO, Response, SOURCE, html, png, saved_path
from test_attachment_links import NAME, SIGNATURE, URL, modern
from test_file_assets import PDF


FRESH = URL.replace(SIGNATURE, '1234567890abcdef' * 3)
ORIGINAL_PHOTO = original_image_url(PHOTO)


class RoutedClient(FakeClient):
    def __init__(self, responses=None, ids=None):
        super().__init__(Listing(ids or [MAIN[1]], True, total=len(ids or [MAIN[1]])))
        self.responses = responses or {}

    def get(self, url, **kwargs):
        self.calls.append(url)
        response = self.responses[url]
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture
def config(tmp_path):
    return Config('demo', tmp_path / 'archive', delay=0, retries=1)


def row(config, key=MAIN):
    state = State(config.out_dir)
    try:
        return dict(state.get(*key))
    finally:
        state.close()


def attached_path(config, key=MAIN):
    state = State(config.out_dir)
    try:
        file = json.loads(state.get(*key)['document'])['files'][0]
        return config.out_dir / state.asset(file_asset_key(file))['path']
    finally:
        state.close()


def seed_cached_attachment(config):
    document = extract_post(html('보관된 원래 본문'), *MAIN)
    document.pop('files')
    document['markdown'] += f'\n\n**첨부파일**\n\n{NAME}\n\n[파일 다운로드]({URL})'
    state = State(config.out_dir)
    try:
        state.discover(*MAIN)
        state.update(*MAIN, document=json.dumps(document, ensure_ascii=False), status='success', content_ok=1)
        original = state.get(*MAIN)
        content, errors = format_note(config, state, original)
        assert not errors
        write_note(config, state, original, content)
    finally:
        state.close()
    return row(config)


def test_new_attachment_is_local_readable_searchable_and_reused(config):
    client = RoutedClient({URL: Response(PDF), ORIGINAL_PHOTO: Response(png())})
    report = backup(config, client=client, renderer=FakeRenderer({MAIN: html(
        '<p>앞 문단</p>' + modern() + f'<p>뒤 문단</p><img src="{PHOTO}">')}))
    assert report['posts'] == {'success': 1}
    assert client.calls == [ORIGINAL_PHOTO, URL]
    attachment = attached_path(config)
    assert attachment.name == NAME and attachment.read_bytes() == PDF
    note = saved_path(config)
    text = note.read_text(encoding='utf-8')
    assert '../../attachments/files/' in text
    assert quote(NAME) in text
    assert text.index('앞 문단') < text.index('첨부파일:') < text.index('뒤 문단')
    assert '[원문에서 다운로드](' + post_url(*MAIN) + ')' in text
    assert 'NBATOKEN' not in text and 'NBAFILE' not in text
    assert NAME in row(config)['body_text']

    preview = create_preview(config, dict(row(config), blog_id=MAIN[0], post_id=MAIN[1]))
    soup = BeautifulSoup(preview.read_text(encoding='utf-8'), 'html.parser')
    link = soup.find('a', string='첨부파일: ' + NAME)
    assert (preview.parent / unquote(link['href'])).resolve() == attachment.resolve()

    paths = [note, attachment, *config.out_dir.glob('attachments/*.png')]
    before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in paths]
    client, renderer = RoutedClient(), FakeRenderer({})
    report = backup(config, client=client, renderer=renderer)
    assert report['run_counts'] == {'saved': 0, 'reused': 1, 'failed': 0}
    assert client.calls == renderer.calls == []
    assert before == [(p.read_bytes(), p.stat().st_mtime_ns) for p in paths]
    assert not list(config.out_dir.glob('history/**/*.md'))


def test_old_cache_renews_only_file_metadata_and_preserves_body_id_and_date(config):
    original = seed_cached_attachment(config)
    note = saved_path(config)
    before = note.read_bytes()
    client = RoutedClient({post_url(*MAIN): Response(text=html('<p>수정된 원격 본문</p>' + modern(FRESH))),
                           FRESH: Response(PDF)})
    renderer = FakeRenderer({})
    report = backup(config, client=client, renderer=renderer)
    assert report['posts'] == {'success': 1}
    assert report['output_counts']['files'] == 1
    assert client.calls == [post_url(*MAIN), FRESH]
    assert renderer.calls == []
    current = row(config)
    assert current['archive_id'] == original['archive_id']
    assert current['archived_at'] == original['archived_at']
    assert '보관된 원래 본문' in note.read_text(encoding='utf-8')
    assert '수정된 원격 본문' not in note.read_text(encoding='utf-8')
    assert json.loads(current['document'])['files'][0]['url'] == FRESH
    assert attached_path(config).read_bytes() == PDF
    history = list(config.out_dir.glob('history/**/*.md'))
    assert len(history) == 1 and history[0].read_bytes() == before
    client = RoutedClient()
    backup(config, client=client, renderer=FakeRenderer({}))
    assert not client.calls
    assert list(config.out_dir.glob('history/**/*.md')) == history


def test_new_signature_does_not_redownload_a_verified_attachment(config):
    backup(config, client=RoutedClient({URL: Response(PDF)}), renderer=FakeRenderer({MAIN: html(modern())}))
    attachment = attached_path(config)
    before = attachment.stat().st_mtime_ns
    client = RoutedClient()
    report = backup(config, refresh=True, client=client, renderer=FakeRenderer({MAIN: html(modern(FRESH))}))
    assert report['posts'] == {'success': 1}
    assert not client.calls
    assert attached_path(config) == attachment and attachment.stat().st_mtime_ns == before


def test_failed_file_is_partial_and_retried_from_cache(config):
    report = backup(config, client=RoutedClient({URL: requests.HTTPError('403 expired')}),
                    renderer=FakeRenderer({MAIN: html('<p>읽을 수 있는 본문</p>' + modern())}))
    assert report['posts'] == {'partial': 1}
    assert report['assets'] == [{'url': URL, 'status': 'failed', 'error': 'HTTPError: 403 expired',
                                  'kind': 'file', 'name': NAME}]
    assert '첨부파일 저장 미완료' in saved_path(config).read_text(encoding='utf-8')
    client = RoutedClient({post_url(*MAIN): Response(text=html(modern(FRESH))), FRESH: Response(PDF)})
    renderer = FakeRenderer({})
    report = backup(config, client=client, renderer=renderer)
    assert report['posts'] == {'success': 1} and report['assets'] == []
    assert not renderer.calls
    assert not inspect_archive(config, verify=True)['verification_problems']


@pytest.mark.parametrize('metadata', [requests.ConnectionError('offline'), Response(text=html('첨부 제거됨'))])
def test_metadata_failure_retains_cached_link_and_readable_body(config, metadata):
    seed_cached_attachment(config)
    client = RoutedClient({post_url(*MAIN): metadata, URL: requests.HTTPError('403')})
    report = backup(config, client=client, renderer=FakeRenderer({}))
    assert report['posts'] == {'partial': 1}
    document = json.loads(row(config)['document'])
    assert document['files'][0]['url'] == URL
    assert '보관된 원래 본문' in saved_path(config).read_text(encoding='utf-8')


def test_disabling_files_is_independent_of_images_and_can_be_enabled_later(config):
    disabled = replace(config, download_files=False)
    client = RoutedClient({ORIGINAL_PHOTO: Response(png())})
    report = backup(disabled, client=client, renderer=FakeRenderer({MAIN: html(modern() + f'<img src="{PHOTO}">')}))
    assert report['posts'] == {'success': 1} and client.calls == [ORIGINAL_PHOTO]
    assert not list(config.out_dir.glob('attachments/files/**/*'))
    client = RoutedClient({post_url(*MAIN): Response(text=html(modern(FRESH))), FRESH: Response(PDF)})
    report = backup(config, client=client, renderer=FakeRenderer({}))
    assert report['posts'] == {'success': 1} and client.calls == [post_url(*MAIN), FRESH]
    assert attached_path(config).read_bytes() == PDF


def test_file_downloads_with_images_disabled(config):
    client = RoutedClient({URL: Response(PDF)})
    report = backup(replace(config, download_images=False), client=client,
                    renderer=FakeRenderer({MAIN: html(modern() + f'<img src="{PHOTO}">')}))
    assert report['posts'] == {'success': 1} and client.calls == [URL]


def test_reformat_is_offline_then_backup_downloads_only_pending_attachment(config, monkeypatch):
    seed_cached_attachment(config)
    with monkeypatch.context() as patch:
        patch.setattr('naver_blog_archive.archive.Client', lambda *a, **k: pytest.fail('offline operation'))
        report = reformat_archive(config)
    assert report['output_counts']['files'] == 1
    assert report['posts'] == {'partial': 1}
    assert '첨부파일 저장 미완료' in saved_path(config).read_text(encoding='utf-8')
    client = RoutedClient({post_url(*MAIN): Response(text=html(modern(FRESH))), FRESH: Response(PDF)})
    report = backup(config, client=client, renderer=FakeRenderer({}))
    assert report['posts'] == {'success': 1}
    assert client.calls == [post_url(*MAIN), FRESH]


def test_missing_attachment_is_detected_and_restored_without_body_refresh(config):
    backup(config, client=RoutedClient({URL: Response(PDF)}), renderer=FakeRenderer({MAIN: html(modern())}))
    attachment = attached_path(config)
    attachment.unlink()
    report = inspect_archive(config, verify=True)
    assert report['posts'] == {'partial': 1}
    assert any('첨부파일 누락' in message for message in report['verification_problems'])
    client = RoutedClient({post_url(*MAIN): Response(text=html(modern(FRESH))), FRESH: Response(PDF)})
    report = backup(config, client=client, renderer=FakeRenderer({}))
    assert report['posts'] == {'success': 1}
    assert attachment.read_bytes() == PDF


def test_manual_file_restore_passes_offline_verification(config):
    backup(config, client=RoutedClient({URL: Response(PDF)}), renderer=FakeRenderer({MAIN: html(modern())}))
    attachment = attached_path(config)
    attachment.write_bytes(b'changed by user')
    assert inspect_archive(config, verify=True)['verification_problems']
    attachment.write_bytes(PDF)
    assert not inspect_archive(config, verify=True)['verification_problems']


def test_manual_file_restore_after_download_failure_passes_offline_verification(config):
    backup(config, client=RoutedClient({URL: Response(PDF)}), renderer=FakeRenderer({MAIN: html(modern())}))
    attachment = attached_path(config)
    attachment.unlink()
    client = RoutedClient({post_url(*MAIN): Response(text=html(modern(FRESH))),
                           FRESH: requests.ConnectionError('offline')})
    assert backup(config, client=client, renderer=FakeRenderer({}))['posts'] == {'partial': 1}
    attachment.write_bytes(PDF)
    # The saved note was changed to the failure link; reformat restores its
    # local link from the verified attachment, without a network request.
    report = reformat_archive(config)
    assert report['posts'] == {'success': 1}
    assert not inspect_archive(config, verify=True)['verification_problems']


def test_edited_markdown_prevents_cached_attachment_upgrade(config):
    original = seed_cached_attachment(config)
    saved_path(config).write_text('내가 직접 수정한 글', encoding='utf-8')
    client = RoutedClient()
    report = backup(config, client=client, renderer=FakeRenderer({}))
    assert report['posts'] == {'partial': 1} and client.calls == []
    assert row(config)['document'] == original['document']
    assert saved_path(config).read_text(encoding='utf-8') == '내가 직접 수정한 글'


def test_disabled_sources_do_not_download_or_report_their_attachments(config):
    source = '<div class="se_sectionArea"><a href="https://blog.naver.com/source/987654321">출처</a></div>'
    backup(config, client=RoutedClient({URL: Response(PDF)}),
           renderer=FakeRenderer({MAIN: html(source), SOURCE: html(modern())}))
    attached_path(config, SOURCE).unlink()
    client = RoutedClient()
    report = backup(replace(config, follow_sources=False), client=client, renderer=FakeRenderer({}))
    assert report['posts'] == {'success': 1} and report['assets'] == []
    assert report['excluded_posts'] == 1 and client.calls == []
