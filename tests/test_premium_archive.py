from dataclasses import replace
import json
from urllib.parse import unquote

from bs4 import BeautifulSoup
import pytest

from naver_blog_archive.archive import backup, inspect_archive, reformat_archive
from naver_blog_archive.catalog import get_archived_post, search_archive
from naver_blog_archive.config import Config, save_config
from naver_blog_archive.cli import main
from naver_blog_archive.jobs import JobRunner
from naver_blog_archive.network import Listing
from naver_blog_archive.premium import PremiumLoginRequired
from naver_blog_archive.reader import create_preview
from naver_blog_archive.state import State
from test_archive import FakeClient, FakeRenderer, Response, png, saved_path
from test_attachment_archive import RoutedClient, attached_path
from test_attachment_links import modern, URL, NAME
from test_file_assets import PDF
from test_jobs import finish
from test_premium_addresses import ARTICLE, CHANNEL, KEY, POST


MAIN = KEY, POST
PHOTO = 'https://pstatic.example.test/photo.png'
NEXT = '260910072654133ab'


def page(content='구독 본문', *, post=POST, authorized=True, logged_in=True):
    return f'''<html><script>var isLogin = {str(logged_in).lower()};</script>
    <header>카테고리 구독 버튼 메뉴</header>
    <div id="_SE_VIEWER_CONTENT" data-cp-name="salarymoney" data-sub-id="moneystock"
         data-content-id="{post}" data-content-auth="{str(authorized).lower()}">
      <span class="viewer_date_text">2026.09.11. 오후 2:30</span>
      <div class="se-title-text">보관할 제목</div>
      <div class="se-main-container">{content}</div>
    </div></html>'''


@pytest.fixture
def config(tmp_path):
    return Config(KEY, tmp_path / 'premium', delay=0, retries=1)


def listing_client():
    return FakeClient(Listing([POST], True, total=1))


def record(config):
    state = State(config.out_dir)
    try:
        return dict(state.get(*MAIN))
    finally:
        state.close()


def test_premium_backup_saves_full_body_photo_attachment_and_searchable_source(config):
    client = RoutedClient({PHOTO: Response(png()), URL: Response(PDF)}, ids=[POST])
    renderer = FakeRenderer({MAIN: page('<p>원문 처음</p>' + f'<img src="{PHOTO}">' + modern() + '<p>마지막 문장</p>')})
    report = backup(config, client=client, renderer=renderer)
    assert report['posts'] == {'success': 1}
    note = saved_path(config, MAIN)
    assert note.parent.relative_to(config.out_dir).as_posix() == 'posts/' + KEY
    assert note.name == f'보관할 제목 ({POST}).md'
    text = note.read_text(encoding='utf-8')
    assert '원문 처음' in text and '마지막 문장' in text
    assert '카테고리 구독 버튼 메뉴' not in text
    assert ARTICLE in text and 'blog.naver.com/premium/' not in text
    assert attached_path(config, MAIN).read_bytes() == PDF
    assert NAME in record(config)['body_text']
    rows = search_archive(config, query='마지막 문장')['items']
    assert len(rows) == 1 and rows[0]['blog_id'] == KEY
    item = get_archived_post(config, rows[0]['archive_id'])
    assert item['source_url'] == ARTICLE
    assert item['published_at'] == '2026-09-11T14:30:00+09:00'
    preview = create_preview(config, item)
    soup = BeautifulSoup(preview.read_text(encoding='utf-8'), 'html.parser')
    assert soup.find('a', string='네이버 원문')['href'] == ARTICLE
    file_link = soup.find('a', string='첨부파일: ' + NAME)
    assert (preview.parent / unquote(file_link['href'])).resolve() == attached_path(config, MAIN)
    assert not inspect_archive(config, verify=True)['verification_problems']


def test_incremental_resume_does_not_refetch_old_premium_body_or_files(config):
    backup(config, client=listing_client(), renderer=FakeRenderer({MAIN: page()}))
    original = saved_path(config, MAIN)
    before = original.read_bytes(), original.stat().st_mtime_ns, record(config)['archive_id']
    client = FakeClient(Listing([NEXT, POST], True, total=2))
    renderer = FakeRenderer({(KEY, NEXT): page('새 글', post=NEXT)})
    report = backup(config, client=client, renderer=renderer)
    assert report['run_counts'] == {'saved': 1, 'reused': 1, 'failed': 0}
    assert renderer.calls == [(KEY, NEXT)] and client.calls == []
    assert before == (original.read_bytes(), original.stat().st_mtime_ns, record(config)['archive_id'])
    assert not list(config.out_dir.glob('history/**/*.md'))


def test_authorized_smarteditor_placeholder_uses_its_link_metadata(config):
    metadata = json.dumps({'src': PHOTO, 'originalWidth': '1509', 'originalHeight': '771'})
    content = (f'<a href="#" class="se-module-image-link" data-linktype="img" data-linkdata=\'{metadata}\'>'
               '<img class="se-image-resource" src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///w=="></a>')
    client = RoutedClient({PHOTO: Response(png())}, ids=[POST])
    report = backup(config, client=client, renderer=FakeRenderer({MAIN: page(content)}))
    assert report['posts'] == {'success': 1} and client.calls == [PHOTO]
    document = json.loads(record(config)['document'])
    assert document['images'][0]['url'] == PHOTO
    assert '../../../../attachments/' in saved_path(config, MAIN).read_text(encoding='utf-8')


@pytest.mark.parametrize('metadata', ['not json', '{"src":"javascript:alert(1)"}', '{"src":123}', '[]'])
def test_invalid_smarteditor_metadata_is_not_requested(config, metadata):
    content = f'<a data-linktype="img" data-linkdata=\'{metadata}\'><img src="data:image/gif;base64,AA=="></a>'
    client = RoutedClient(ids=[POST])
    report = backup(config, client=client, renderer=FakeRenderer({MAIN: page(content)}))
    assert report['posts'] == {'partial': 1} and client.calls == []


def test_paywalled_teaser_never_becomes_a_successful_note(config):
    report = backup(config, client=listing_client(),
                    renderer=FakeRenderer({MAIN: page('짧은 미리보기', authorized=False)}))
    assert report['posts'] == {'failed': 1}
    assert record(config)['document'] is None
    assert '열람 권한' in record(config)['error']
    assert not list(config.out_dir.rglob('*.md'))


def test_missing_login_stops_before_creating_archive_database(config, monkeypatch):
    monkeypatch.setattr('naver_blog_archive.premium_auth.load_session', lambda: None)
    with pytest.raises(PremiumLoginRequired, match='로그인'):
        backup(config)
    assert not config.out_dir.exists()


def test_session_expiry_stops_once_and_next_login_resumes_remaining_posts(config):
    client = FakeClient(Listing([POST, NEXT], True, total=2))
    renderer = FakeRenderer({(KEY, NEXT): page('새 본문', post=NEXT), MAIN: page(logged_in=False)})
    with pytest.raises(PremiumLoginRequired):
        backup(config, client=client, renderer=renderer)
    assert renderer.calls == [(KEY, NEXT), MAIN]
    assert client.closed and renderer.closed
    state = State(config.out_dir)
    assert state.latest_run(KEY)['status'] == 'failed'
    state.close()
    renderer = FakeRenderer({MAIN: page('로그인 후 전체 본문')})
    report = backup(config, client=FakeClient(Listing([POST, NEXT], True, total=2)), renderer=renderer)
    assert report['posts'] == {'success': 2} and renderer.calls == [MAIN]


def test_premium_cached_notes_restore_offline_without_loading_session(config, monkeypatch):
    backup(config, client=listing_client(), renderer=FakeRenderer({MAIN: page()}))
    original = record(config)
    saved_path(config, MAIN).unlink()
    monkeypatch.setattr('naver_blog_archive.premium_auth.load_session', lambda: pytest.fail('offline operation'))
    report = reformat_archive(config)
    assert report['posts'] == {'success': 1}
    assert saved_path(config, MAIN).is_file()
    assert original['archive_id'] == record(config)['archive_id']
    assert original['archived_at'] == record(config)['archived_at']


def test_edited_premium_markdown_is_preserved_on_refresh(config):
    backup(config, client=listing_client(), renderer=FakeRenderer({MAIN: page()}))
    path = saved_path(config, MAIN)
    path.write_text('내가 작성한 메모', encoding='utf-8')
    report = backup(config, refresh=True, client=listing_client(), renderer=FakeRenderer({MAIN: page('변경된 글')}))
    assert report['posts'] == {'partial': 1}
    assert path.read_text(encoding='utf-8') == '내가 작성한 메모'


def test_premium_cannot_mix_into_existing_blog_archive(config):
    blog_config = replace(config, blog_id='salarymoney')
    backup(blog_config, client=FakeClient(Listing(['123'], True)),
           renderer=FakeRenderer({('salarymoney', '123'): '<div class="se-main-container">본문</div>'}))
    with pytest.raises(ValueError, match='다른 주 블로그'):
        backup(config, client=listing_client(), renderer=FakeRenderer({}))


def test_link_card_between_premium_notes_points_to_saved_md(config):
    link = CHANNEL + '/contents/' + NEXT
    content = f'<div class="se-oglink"><a href="{link}" class="se-oglink-info"><strong class="se-oglink-title">다른 글</strong></a></div>'
    report = backup(config, client=FakeClient(Listing([POST, NEXT], True)),
                    renderer=FakeRenderer({MAIN: page(content), (KEY, NEXT): page('대상 본문', post=NEXT)}))
    assert report['posts'] == {'success': 2}
    text = saved_path(config, MAIN).read_text(encoding='utf-8')
    assert f'%28{NEXT}%29.md' in text and '보관된 글 열기' in text


@pytest.mark.parametrize('citation', [ARTICLE, 'https://naver.me/premium'])
def test_blog_premium_citation_preserves_previous_public_backup_behavior(config, citation):
    config = replace(config, blog_id='demo')
    client = FakeClient(Listing(['123'], True))
    client.resolve_source = lambda url: ARTICLE
    renderer = FakeRenderer({('demo', '123'): '<div class="se-main-container">일반 블로그 본문'
                             f'<div class="se_sectionArea"><a href="{citation}">참고 글</a></div></div>'})
    report = backup(config, client=client, renderer=renderer)
    assert report['posts'] == {'success': 1}
    assert renderer.calls == [('demo', '123')]
    assert citation in saved_path(config, ('demo', '123')).read_text(encoding='utf-8')


def test_login_worker_runs_off_thread_and_reports_success_without_archive(config, monkeypatch):
    from threading import current_thread
    caller = current_thread()
    def login(actual, control):
        assert current_thread() is not caller and actual == config
        control.emit('login', '직접 로그인해 주세요')
    monkeypatch.setattr('naver_blog_archive.premium_auth.login', login)
    runner = JobRunner()
    runner.start('login', config)
    events, done = finish(runner)
    assert done['status'] == 'success' and done['report'] is None
    assert events[0]['phase'] == 'login'
    assert not config.out_dir.exists()


def test_login_cli_uses_same_config_and_never_starts_backup(config, tmp_path, monkeypatch):
    path = tmp_path / 'premium.toml'
    save_config(config, path)
    calls = []
    monkeypatch.setattr('naver_blog_archive.premium_auth.login', lambda actual: calls.append(actual))
    assert main(['login', '--config', str(path)]) == 0
    assert calls == [config] and not config.out_dir.exists()
