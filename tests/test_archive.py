from dataclasses import replace
from io import BytesIO
import json
import os
from pathlib import Path
from urllib.parse import quote

from PIL import Image
import pytest
import requests

from naver_blog_archive.archive import backup, inspect_archive
from naver_blog_archive.config import Config, load_config
from naver_blog_archive.files import archive_lock, atomic_write, digest
from naver_blog_archive.network import Client, Listing, original_image_url, parse_post_url
from naver_blog_archive.parser import extract_post
from naver_blog_archive.progress import OperationCancelled, TaskControl
from naver_blog_archive.state import State


MAIN = ('demo', '123456789')
SOURCE = ('source', '987654321')
PHOTO = 'https://example.test/a_b.png?type=w800&token=abc'


def html(content='<p>Hello world</p>', title='Title', body_class='se-main-container'):
    return f'<html><div class="se-title-text">{title}</div><div class="{body_class}">{content}</div></html>'


def png():
    stream = BytesIO()
    Image.new('RGB', (2, 2), 'red').save(stream, format='PNG')
    return stream.getvalue()


class Response:
    def __init__(self, data=b'', text='', status=200):
        self.data, self.text, self.status_code = data, text, status
        self.headers = {}
        self.closed = False
    def __enter__(self): return self
    def __exit__(self, *args): self.close()
    def close(self): self.closed = True
    def iter_content(self, size): yield self.data
    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


class FakeClient:
    def __init__(self, listing=None, data=None):
        self.result = listing or Listing([MAIN[1]], True, total=1)
        self.data = png() if data is None else data
        self.calls = []
        self.list_calls = []
        self.closed = False
    def listing(self, blog):
        self.list_calls.append(blog)
        return self.result
    def get(self, url, **kwargs):
        self.calls.append(url)
        if isinstance(self.data, BaseException): raise self.data
        return Response(self.data)
    def resolve_source(self, url): return 'https://blog.naver.com/source/987654321'
    def close(self): self.closed = True


class FakeRenderer:
    def __init__(self, pages=None):
        self.pages = pages if pages is not None else {MAIN: html()}
        self.calls = []
        self.closed = False
    def render(self, blog, post):
        self.calls.append((blog, post))
        page = self.pages[(blog, post)]
        if isinstance(page, BaseException): raise page
        return page
    def close(self): self.closed = True


@pytest.fixture
def config(tmp_path):
    return Config('demo', tmp_path / 'archive', delay=0, retries=1)


def run(config, pages=None, client=None, **kwargs):
    return backup(config, client=client or FakeClient(), renderer=FakeRenderer(pages), **kwargs)


def saved_path(config, key=MAIN):
    state = State(config.out_dir)
    try:
        return config.out_dir / state.get(*key)['path']
    finally:
        state.close()


def saved_link(config, origin, target):
    relative = os.path.relpath(saved_path(config, target), saved_path(config, origin).parent)
    return quote(Path(relative).as_posix(), safe='/')


def test_config_relative_to_file_not_working_directory(tmp_path, monkeypatch):
    location = tmp_path / 'settings'
    location.mkdir()
    path = location / 'config.toml'
    path.write_text('blog_id = "demo"\nout_dir = "../result"\n', encoding='utf-8')
    monkeypatch.chdir(tmp_path.parent)
    assert load_config(path).out_dir == tmp_path / 'result'


@pytest.mark.parametrize('setting', ['delay = -1', 'retries = 0', 'delay = nan', 'download_images = "false"', 'typo = 3'])
def test_invalid_config_is_rejected(tmp_path, setting):
    path = tmp_path / 'config.toml'
    path.write_text('blog_id = "demo"\n' + setting, encoding='utf-8')
    with pytest.raises(ValueError): load_config(path)


@pytest.mark.parametrize('url', [
    'https://blog.naver.com/PostView.naver?logNo=123456789&blogId=demo',
    'https://m.blog.naver.com/PostView.naver?blogId=demo&logNo=123456789',
    'https://blog.naver.com/demo/123456789',
])
def test_source_url_order_and_hosts(url):
    assert parse_post_url(url) == MAIN


def test_false_naver_host_is_not_followed():
    assert parse_post_url('https://evil.test/blog.naver.com/demo/123456789') is None


def test_original_image_query_is_preserved():
    assert original_image_url(PHOTO) == 'https://example.test/a_b.png?token=abc'
    assert original_image_url('https://example.test/x?token=a%2Fb&type=w800&x=1') == 'https://example.test/x?token=a%2Fb&x=1'


@pytest.mark.parametrize('content', ['<html><title>Error</title></html>', html(''), html('<script>junk</script>')])
def test_missing_or_empty_body_does_not_create_success(config, content):
    report = run(config, {MAIN: content})
    assert report['last_run']['status'] == 'partial'
    assert report['posts'] == {'failed': 1}
    assert not list(config.out_dir.rglob('*.md'))


def test_old_editor_and_image_only_are_valid():
    assert extract_post('<div id="postViewArea"><p>Old text</p></div>', *MAIN)['markdown'] == 'Old text'
    assert extract_post(html('<img src="https://example.test/p.png">'), *MAIN)['images']


def test_partial_image_is_retried_without_rerender(config):
    page = html(f'<p>Body</p><img src="{PHOTO}">')
    first = run(config, {MAIN: page}, FakeClient(data=b'<html>error</html>' * 200))
    assert first['posts'] == {'partial': 1}
    assert not list(config.out_dir.glob('attachments/*'))
    renderer, client = FakeRenderer({}), FakeClient()
    second = backup(config, client=client, renderer=renderer)
    assert second['posts'] == {'success': 1}
    assert not renderer.calls
    image = next(config.out_dir.glob('attachments/*.png'))
    assert image.stat().st_size < 1000
    note = next(config.out_dir.glob('posts/demo/*.md')).read_text(encoding='utf-8')
    assert '../../attachments/' in note
    assert 'NBATOKEN' not in note
    assert not inspect_archive(config, verify=True)['verification_problems']


def test_sources_retry_without_new_main_posts_and_link_to_real_file(config):
    source = '<div class="se_sectionArea"><a href="https://blog.naver.com/source/987654321">Citation</a><p>Quoted duplicate</p></div>'
    first = run(config, {MAIN: html('<p>Main</p>' + source), SOURCE: RuntimeError('offline')})
    assert first['posts'] == {'partial': 1, 'failed': 1}
    renderer = FakeRenderer({SOURCE: html('<p>Source body</p>')})
    result = backup(config, client=FakeClient(), renderer=renderer)
    assert result['posts'] == {'success': 2}
    assert result['run_counts'] == {'saved': 2, 'reused': 0, 'failed': 0}
    assert renderer.calls == [SOURCE]
    note = saved_path(config).read_text(encoding='utf-8')
    assert saved_link(config, MAIN, SOURCE) in note
    assert note.count('Citation') == 1
    assert 'Quoted duplicate' not in note


def test_duplicate_runs_are_idempotent(config):
    run(config)
    paths = list(config.out_dir.glob('posts/**/*.md'))
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in paths}
    renderer = FakeRenderer({})
    backup(config, client=FakeClient(), renderer=renderer)
    assert not renderer.calls
    assert before == {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in config.out_dir.glob('posts/**/*.md')}


def test_verified_posts_skip_processing_and_keep_attempts_and_file_times(config, monkeypatch):
    from naver_blog_archive.assets import Assets

    run(config, {MAIN: html(f'<p>Saved body</p><img src="{PHOTO}">')})
    paths = [*config.out_dir.glob('posts/**/*.md'), *config.out_dir.glob('attachments/*')]
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}
    state = State(config.out_dir)
    attempts = state.get(*MAIN)['attempts']
    state.close()
    asset_calls = []
    monkeypatch.setattr(Assets, 'obtain', lambda *args: asset_calls.append(args))
    renderer, client, events = FakeRenderer({}), FakeClient(), []

    result = backup(config, client=client, renderer=renderer, control=TaskControl(events.append))

    assert client.list_calls == ['demo']
    assert renderer.calls == client.calls == asset_calls == []
    assert before == {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}
    assert not list(config.out_dir.glob('history/**/*.md'))
    state = State(config.out_dir)
    assert state.get(*MAIN)['attempts'] == attempts
    state.close()
    assert result['run_counts'] == {'saved': 0, 'reused': 1, 'failed': 0}
    assert not [event for event in events if event['phase'] == 'post']
    skipped = [event for event in events if event['phase'] == 'skipped']
    assert len(skipped) == 1
    assert skipped[0]['status'] == 'success'
    assert skipped[0]['outcome'] == 'reused'
    assert skipped[0]['completed'] == skipped[0]['total'] == 1
    assert skipped[0]['run_counts'] == events[-1]['run_counts'] == result['run_counts']
    assert json.loads((config.out_dir / 'report.json').read_text(encoding='utf-8'))['run_counts'] == result['run_counts']


def test_incremental_backup_fetches_only_new_posts_and_refresh_fetches_existing(config):
    new = ('demo', '223456789')
    run(config, {MAIN: html('Original body')})
    original_path = saved_path(config)
    original = original_path.read_bytes()
    pages = {MAIN: html('Changed remote body'), new: html('New post')}
    renderer = FakeRenderer(pages)
    client = FakeClient(Listing([MAIN[1], new[1]], True, total=2))

    result = backup(config, client=client, renderer=renderer)

    assert renderer.calls == [new]
    assert saved_path(config) == original_path
    assert original_path.read_bytes() == original
    assert result['run_counts'] == {'saved': 1, 'reused': 1, 'failed': 0}
    renderer = FakeRenderer(pages)
    result = backup(config, refresh=True, client=FakeClient(client.result), renderer=renderer)
    assert renderer.calls == [MAIN, new]
    assert 'Changed remote body' in original_path.read_text(encoding='utf-8')
    assert result['run_counts'] == {'saved': 2, 'reused': 0, 'failed': 0}


def test_reused_posts_keep_source_graph_without_render_or_attempts(config):
    page = html('<div class="se_sectionArea"><a href="https://blog.naver.com/source/987654321">Source</a></div>')
    run(config, {MAIN: page, SOURCE: html('Source body')})
    renderer = FakeRenderer({})

    result = backup(config, client=FakeClient(), renderer=renderer)

    assert renderer.calls == []
    assert result['run_counts'] == {'saved': 0, 'reused': 2, 'failed': 0}
    state = State(config.out_dir)
    assert [row['attempts'] for row in state.posts()] == [1, 1]
    state.close()


def test_enabling_sources_resolves_cached_short_url_before_reuse(config):
    page = html('<div class="se_sectionArea"><a href="https://naver.me/example">Source</a></div>')
    run(replace(config, follow_sources=False), {MAIN: page})
    renderer, client = FakeRenderer({SOURCE: html('Source body')}), FakeClient()
    resolved = []
    client.resolve_source = lambda url: resolved.append(url) or 'https://blog.naver.com/source/987654321'

    result = backup(config, client=client, renderer=renderer)

    assert resolved == ['https://naver.me/example']
    assert renderer.calls == [SOURCE]
    assert result['run_counts'] == {'saved': 2, 'reused': 0, 'failed': 0}
    assert saved_link(config, MAIN, SOURCE) in saved_path(config).read_text(encoding='utf-8')


def test_increased_source_depth_resolves_previously_unfollowed_short_url(config):
    third = ('third', '333333333')
    page = html('<div class="se_sectionArea"><a href="https://blog.naver.com/source/987654321">Source</a></div>')
    source = html('<div class="se_sectionArea"><a href="https://naver.me/deeper">Deep source</a></div>')
    run(config, {MAIN: page, SOURCE: source})
    renderer, client = FakeRenderer({third: html('Deep source body')}), FakeClient()
    resolved = []
    client.resolve_source = lambda url: resolved.append(url) or 'https://blog.naver.com/third/333333333'

    result = backup(replace(config, source_depth=2), client=client, renderer=renderer)

    assert resolved == ['https://naver.me/deeper']
    assert renderer.calls == [third]
    assert result['run_counts'] == {'saved': 2, 'reused': 1, 'failed': 0}
    assert saved_link(config, SOURCE, third) in saved_path(config, SOURCE).read_text(encoding='utf-8')


def test_partial_listing_never_reports_complete(config):
    result = run(config, client=FakeClient(Listing([MAIN[1]], False, 'page 2 failed', 50)))
    assert result['last_run']['status'] == 'partial'
    assert result['last_run']['listing_complete'] == 0
    assert result['last_run']['listed'] == 1
    assert result['posts'] == {'success': 1}


def test_existing_legacy_note_is_reused_and_original_preserved(config):
    config.out_dir.mkdir()
    path = config.out_dir / 'Old title (123456789).md'
    original = '---\n본문주소: "https://m.blog.naver.com/demo/123456789"\n---\n# Old title\n'
    path.write_text(original, encoding='utf-8')
    result = run(config)
    assert result['posts'] == {'success': 1}
    assert 'Hello world' in path.read_text(encoding='utf-8')
    assert not list(config.out_dir.glob('posts/**/*.md'))
    assert next(config.out_dir.glob('history/demo/123456789/*.md')).read_text(encoding='utf-8') == original


@pytest.mark.parametrize('refresh', [False, True])
def test_user_edits_are_preserved(config, refresh):
    run(config)
    path = saved_path(config)
    path.write_text('My personal edits', encoding='utf-8')
    report = run(config, {MAIN: html('New body')}, refresh=refresh)
    assert report['last_run']['status'] == 'partial'
    assert report['run_counts'] == {'saved': 0, 'reused': 0, 'failed': 1}
    assert path.read_text(encoding='utf-8') == 'My personal edits'


def test_missing_note_and_image_recover_from_state(config):
    run(config, {MAIN: html(f'<img src="{PHOTO}">')})
    next(config.out_dir.glob('posts/**/*.md')).unlink()
    next(config.out_dir.glob('attachments/*')).unlink()
    assert inspect_archive(config, verify=True)['verification_problems']
    renderer = FakeRenderer({})
    result = backup(config, client=FakeClient(), renderer=renderer)
    assert result['posts'] == {'success': 1}
    assert result['run_counts'] == {'saved': 1, 'reused': 0, 'failed': 0}
    assert not renderer.calls
    assert not inspect_archive(config, verify=True)['verification_problems']


def test_refresh_failure_is_retried_in_next_normal_backup(config):
    run(config)
    result = run(config, {MAIN: RuntimeError('offline')}, refresh=True)
    assert result['last_run']['status'] == 'partial'
    renderer = FakeRenderer({MAIN: html('Updated text')})
    result = backup(config, client=FakeClient(), renderer=renderer)
    assert renderer.calls == [MAIN]
    assert result['posts'] == {'success': 1}
    assert 'Updated text' in saved_path(config).read_text(encoding='utf-8')


def test_keyboard_interrupt_preserves_pending_work(config):
    renderer = FakeRenderer({MAIN: KeyboardInterrupt()})
    client = FakeClient()
    with pytest.raises(KeyboardInterrupt): backup(config, client=client, renderer=renderer)
    assert renderer.closed and client.closed
    result = run(config)
    assert result['posts'] == {'success': 1}
    state = State(config.out_dir)
    assert state.db.execute('SELECT status FROM runs ORDER BY id').fetchone()[0] == 'interrupted'
    state.close()


def test_crash_after_file_replacement_is_recoverable(config, monkeypatch):
    original_update = State.update
    def crash(self, blog, post, **values):
        if 'file_hash' in values:
            raise SystemExit('crash before checksum commit')
        return original_update(self, blog, post, **values)
    with monkeypatch.context() as patch:
        patch.setattr(State, 'update', crash)
        with pytest.raises(SystemExit): run(config)
    renderer = FakeRenderer({})
    result = backup(config, client=FakeClient(), renderer=renderer)
    assert result['posts'] == {'success': 1}
    assert not renderer.calls


def test_source_cycles_stop_at_depth_and_resolve(config):
    a = html('<div class="se_sectionArea"><a href="https://blog.naver.com/source/987654321">B</a></div>')
    b = html('<div class="se_sectionArea"><a href="https://blog.naver.com/demo/123456789">A</a></div>')
    result = run(replace(config, source_depth=2), {MAIN: a, SOURCE: b})
    assert result['posts'] == {'success': 2}


def test_optional_images_and_sources_are_respected(config):
    page = html(f'<img src="{PHOTO}"><div class="se_sectionArea"><a href="https://blog.naver.com/source/987654321">S</a></div>')
    client = FakeClient(data=RuntimeError('must not fetch'))
    result = run(replace(config, download_images=False, follow_sources=False), {MAIN: page}, client)
    assert result['posts'] == {'success': 1}
    assert not client.calls


def test_source_missing_on_disk_invalidates_parent(config):
    page = html('<div class="se_sectionArea"><a href="https://blog.naver.com/source/987654321">S</a></div>')
    run(config, {MAIN: page, SOURCE: html('Source')})
    saved_path(config, SOURCE).unlink()
    report = inspect_archive(config, verify=True)
    assert report['posts'] == {'partial': 2}


def test_lock_excludes_second_writer_and_releases(config):
    with archive_lock(config.out_dir):
        with pytest.raises(RuntimeError):
            with archive_lock(config.out_dir): pass
    with archive_lock(config.out_dir): pass


def test_other_main_blog_cannot_mix_into_archive(config):
    run(config)
    with pytest.raises(ValueError, match='다른 주 블로그'):
        run(replace(config, blog_id='different'))


@pytest.mark.parametrize('pages, complete, size', [
    ([{'result': {'items': [{'logNo': 1}], 'totalCount': 1}}, {'result': {'items': [], 'totalCount': 1}}], True, 1),
    ([{'result': {'items': [{'logNo': 1}], 'totalCount': 0}}, {'result': {'items': [{'logNo': 2}], 'totalCount': 0}}, {'result': {'items': [], 'totalCount': 0}}], True, 2),
    ([{'result': {'items': [{'logNo': 1}]}}, {'result': {'items': [{'logNo': 1}]}}], False, 1),
    ([{'result': {'items': [{'logNo': 1}], 'totalCount': 2}}, {'result': {'items': [], 'totalCount': 2}}], False, 1),
    ([{'unexpected': []}], False, 0),
])
def test_listing_completeness_and_schema(config, monkeypatch, pages, complete, size):
    client = Client(config)
    responses = iter(Response(text=json.dumps(p)) for p in pages)
    monkeypatch.setattr(client, 'get', lambda *a, **k: next(responses))
    result = client.listing('demo')
    assert result.complete == complete and len(result.ids) == size
    client.close()


def test_http_retries_429_and_closes_failed_response(config, monkeypatch):
    client = Client(replace(config, retries=2))
    failed, success = Response(status=429), Response(status=200)
    responses = iter([failed, success])
    monkeypatch.setattr(client.session, 'get', lambda *a, **k: next(responses))
    monkeypatch.setattr('naver_blog_archive.network.time.sleep', lambda _: None)
    assert client.get('https://example.test') is success
    assert failed.closed
    client.close()


def test_legacy_source_filename_special_characters_are_encoded(config):
    config.out_dir.mkdir()
    source_path = config.out_dir / 'C# [note] 100% (987654321).md'
    source_path.write_text('---\n본문주소: "https://m.blog.naver.com/source/987654321"\n---\nOld\n', encoding='utf-8')
    page = html('<div class="se_sectionArea"><a href="https://blog.naver.com/source/987654321">Source</a></div>')
    result = run(config, {MAIN: page, SOURCE: html('Source content')})
    assert result['posts'] == {'success': 2}
    note = saved_path(config).read_text(encoding='utf-8')
    assert 'C%23%20%5Bnote%5D%20100%25%20%28987654321%29.md' in note


def test_media_url_underscores_survive_conversion():
    document = extract_post(html('<iframe src="https://example.test/a_b"></iframe>'), *MAIN)
    assert '(https://example.test/a_b)' in document['markdown']


def test_disabling_sources_excludes_old_failed_jobs(config):
    page = html('<div class="se_sectionArea"><a href="https://blog.naver.com/source/987654321">S</a></div>')
    run(config, {MAIN: page, SOURCE: RuntimeError('offline')})
    disabled = replace(config, follow_sources=False)
    result = run(disabled, {})
    assert result['last_run']['status'] == 'success'
    assert result['posts'] == {'success': 1}
    assert result['excluded_posts'] == 1
    assert not result['failures']
    assert not inspect_archive(disabled, verify=True)['verification_problems']


def test_multiple_images_in_one_photo_link_are_all_preserved(config):
    first, second = 'https://example.test/first.png', 'https://example.test/second.png'
    page = html(f'<a href="https://example.test/gallery"><img src="{first}" alt="First">'
                f'<span><img src="{second}" alt="Second"></span></a>')
    result = run(config, {MAIN: page})
    assert result['posts'] == {'success': 1}
    note = saved_path(config).read_text(encoding='utf-8')
    assert '![First](../../attachments/' in note
    assert '![Second](../../attachments/' in note
    assert 'NBATOKEN' not in note


def test_verify_recovers_restored_note_and_image_without_network(config):
    run(config, {MAIN: html(f'<img src="{PHOTO}">')})
    note = saved_path(config)
    image = next(config.out_dir.glob('attachments/*'))
    originals = {path: path.read_bytes() for path in (note, image)}
    for path in originals:
        path.unlink()
    assert inspect_archive(config, verify=True)['posts'] == {'partial': 1}
    for path, content in originals.items():
        path.write_bytes(content)
    result = inspect_archive(config, verify=True)
    assert result['verification_problems'] == []
    assert result['posts'] == {'success': 1}
    assert result['failures'] == []
    assert result['assets'] == []


def test_verify_restored_source_also_recovers_parent(config):
    page = html('<div class="se_sectionArea"><a href="https://blog.naver.com/source/987654321">S</a></div>')
    run(config, {MAIN: page, SOURCE: html('Source')})
    path = saved_path(config, SOURCE)
    original = path.read_bytes()
    path.unlink()
    assert inspect_archive(config, verify=True)['posts'] == {'partial': 2}
    path.write_bytes(original)
    result = inspect_archive(config, verify=True)
    assert result['verification_problems'] == []
    assert result['posts'] == {'success': 2}


def test_verify_does_not_approve_old_note_after_failed_refresh_write(config):
    run(config)
    path = saved_path(config)
    original = path.read_bytes()
    path.write_text('My edits', encoding='utf-8')
    run(config, {MAIN: html('Updated server body')}, refresh=True)
    path.write_bytes(original)
    result = inspect_archive(config, verify=True)
    assert result['posts'] == {'partial': 1}
    assert any('갱신 필요' in error for error in result['verification_problems'])


def test_refresh_excludes_removed_failed_citation(config):
    page = html('<div class="se_sectionArea"><a href="https://blog.naver.com/source/987654321">S</a></div>')
    run(config, {MAIN: page, SOURCE: RuntimeError('Source unavailable')})
    renderer = FakeRenderer({MAIN: html('Citation was removed')})
    result = backup(config, refresh=True, client=FakeClient(), renderer=renderer)
    assert renderer.calls == [MAIN]
    assert result['last_run']['status'] == 'success'
    assert result['posts'] == {'success': 1}
    assert result['excluded_posts'] == 1
    assert not result['failures']
    assert not inspect_archive(config, verify=True)['verification_problems']


def test_refresh_excludes_removed_source_but_keeps_its_saved_file(config):
    page = html('<div class="se_sectionArea"><a href="https://blog.naver.com/source/987654321">S</a></div>')
    run(config, {MAIN: page, SOURCE: html('Source')})
    source = saved_path(config, SOURCE)
    original = source.read_bytes()
    result = run(config, {MAIN: html('No citation')}, refresh=True)
    assert result['posts'] == {'success': 1}
    assert result['excluded_posts'] == 1
    assert source.read_bytes() == original


def test_source_depth_is_recalculated_when_shorter_citation_is_removed(config):
    third, fourth = ('third', '111111111'), ('fourth', '222222222')
    def citation(key):
        return f'<div class="se_sectionArea"><a href="https://blog.naver.com/{key[0]}/{key[1]}">Source</a></div>'
    config = replace(config, source_depth=2)
    run(config, {MAIN: html(citation(SOURCE) + citation(third)),
                 SOURCE: html(citation(third)), third: html(citation(fourth)), fourth: html('Last source')})
    renderer = FakeRenderer({MAIN: html(citation(SOURCE)), SOURCE: html(citation(third)), third: html(citation(fourth))})
    result = backup(config, refresh=True, client=FakeClient(), renderer=renderer)
    assert renderer.calls == [MAIN, SOURCE, third]
    assert result['posts'] == {'success': 3}
    assert result['excluded_posts'] == 1
    note = saved_path(config, third).read_text(encoding='utf-8')
    assert '../fourth/' not in note
    assert 'https://blog.naver.com/fourth/222222222' in note


def test_inspection_rejects_another_main_blog(config):
    run(config)
    with pytest.raises(ValueError, match='다른 주 블로그'):
        inspect_archive(replace(config, blog_id='different'), verify=True)


def test_cooperative_stop_persists_report_and_resumes(config):
    events = []
    control = TaskControl()
    def receive(event):
        events.append(event)
        if event['phase'] == 'post' and event['status'] == 'running':
            control.cancel()
    control.on_event = receive
    renderer, client = FakeRenderer(), FakeClient()
    with pytest.raises(OperationCancelled) as stopped:
        backup(config, client=client, renderer=renderer, control=control)
    assert renderer.closed and client.closed and renderer.calls == []
    assert stopped.value.report['last_run']['status'] == 'interrupted'
    report = json.loads((config.out_dir / 'report.json').read_text(encoding='utf-8'))
    assert report['last_run']['status'] == 'interrupted'
    assert report['posts'] == {'pending': 1}
    assert events[-1]['phase'] == 'interrupted'
    assert run(config)['posts'] == {'success': 1}


def test_stop_after_reused_post_preserves_run_counts_and_new_pending_post(config):
    new = ('demo', '223456789')
    run(config)
    control = TaskControl()
    control.on_event = lambda event: control.cancel() if event['phase'] == 'skipped' else None
    renderer = FakeRenderer({new: html('New post')})
    listing = Listing([MAIN[1], new[1]], True, total=2)

    with pytest.raises(OperationCancelled) as stopped:
        backup(config, client=FakeClient(listing), renderer=renderer, control=control)

    assert renderer.calls == []
    assert stopped.value.report['last_run']['status'] == 'interrupted'
    assert stopped.value.report['posts'] == {'success': 1, 'pending': 1}
    assert stopped.value.report['run_counts'] == {'saved': 0, 'reused': 1, 'failed': 0}
    renderer = FakeRenderer({new: html('New post')})
    result = backup(config, client=FakeClient(listing), renderer=renderer)
    assert renderer.calls == [new]
    assert result['run_counts'] == {'saved': 1, 'reused': 1, 'failed': 0}


def test_stop_during_image_stream_keeps_cached_body_for_resume(config):
    control = TaskControl()
    class InterruptedImage(Response):
        def iter_content(self, size):
            yield png()[:10]
            control.cancel()
            yield png()[10:]
    response = InterruptedImage()
    client = FakeClient()
    client.get = lambda *args, **kwargs: response
    with pytest.raises(OperationCancelled):
        backup(config, client=client, renderer=FakeRenderer({MAIN: html(f'<img src="{PHOTO}">')}), control=control)
    assert response.closed
    assert not list(config.out_dir.glob('attachments/*'))
    renderer = FakeRenderer({})
    result = backup(config, client=FakeClient(), renderer=renderer)
    assert result['posts'] == {'success': 1}
    assert renderer.calls == []


def test_stop_during_verify_releases_archive_lock(config):
    run(config)
    control = TaskControl()
    control.on_event = lambda event: control.cancel() if event['phase'] == 'verify' else None
    with pytest.raises(OperationCancelled):
        inspect_archive(config, verify=True, control=control)
    with archive_lock(config.out_dir):
        pass


def test_progress_reports_posts_and_final_result(config):
    events = []
    result = run(config, control=TaskControl(events.append))
    post_events = [event for event in events if event['phase'] == 'post']
    assert post_events[0]['completed'] == 0
    assert post_events[-1]['completed'] == post_events[-1]['total'] == 1
    assert post_events[-1]['counts'] == {'success': 1}
    assert events[-1]['report'] == result


def test_control_wait_can_be_interrupted():
    from threading import Event, Thread
    waiting, stopped = Event(), Event()
    control = TaskControl()
    def worker():
        waiting.set()
        try:
            control.wait(60)
        except OperationCancelled:
            stopped.set()
    thread = Thread(target=worker, daemon=True)
    thread.start()
    assert waiting.wait(1)
    control.cancel()
    thread.join(1)
    assert not thread.is_alive()
    assert stopped.is_set()
