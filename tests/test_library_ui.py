"""Local library interaction with controlled searches and isolated temporary files."""
from pathlib import Path
from threading import Thread, get_ident

import pytest

tk = pytest.importorskip('tkinter', reason='GUI tests require Python with tkinter')

from naver_blog_archive import catalog, library_ui, reader
from naver_blog_archive.config import Config


@pytest.fixture
def pane(desktop, tmp_path, monkeypatch):
    window = tk.Toplevel(desktop)
    window.withdraw()
    current = {'config': Config('demo_blog', tmp_path / 'first')}
    queries, pending, dialogs = [], [], []

    class ControlledThread:
        def __init__(self, *, target, args, **kwargs):
            self.target, self.args = target, args

        def start(self):
            pending.append(self)

        def finish(self):
            self.target(*self.args)

    def search(config, **kwargs):
        queries.append((config, kwargs))
        return {'items': [], 'total': 0, 'offset': kwargs['offset'], 'limit': kwargs['limit']}

    monkeypatch.setattr(library_ui, 'Thread', ControlledThread)
    monkeypatch.setattr(catalog, 'search_archive', search)
    monkeypatch.setattr(library_ui.messagebox, 'showinfo', lambda *args, **kwargs: dialogs.append(args))
    monkeypatch.setattr(library_ui.messagebox, 'showerror', lambda *args, **kwargs: dialogs.append(args))
    widget = library_ui.LibraryPane(window, lambda: current['config'])
    widget.pack()
    widget.test_current = current
    widget.test_queries = queries
    widget.test_pending = pending
    widget.test_dialogs = dialogs
    yield widget
    for callback in desktop.tk.call('after', 'info'):
        desktop.tk.call('after', 'cancel', callback)
    window.destroy()


def row(number=1, **changes):
    return {
        'archive_id': f'archive-{number}', 'post_id': str(1000 + number), 'blog_id': 'demo_blog',
        'title': f'Saved post {number}', 'path': f'posts/{number}.md', 'status': 'success',
        'published_at': '2026-08-01', 'archived_at': '2026-09-01', **changes,
    }


def result(items, *, total=None, offset=0):
    return {'items': items, 'total': len(items) if total is None else total, 'offset': offset, 'limit': 50}


def show_result(pane, items, *, total=None, offset=0):
    pane.refresh()
    pane._events.put((pane._request, pane.test_current['config'], result(items, total=total, offset=offset), None))
    pane._poll()


def select_first(pane):
    pane.tree.selection_set(pane.tree.get_children()[0])


def test_search_passes_trimmed_query_and_renders_only_after_queue_poll(pane):
    pane.query.set('  제주 여행  ')
    pane.refresh()
    assert pane._busy
    assert pane.search_button.instate(['disabled'])
    assert pane.next_button.instate(['disabled'])
    assert pane.test_queries == []
    pane.test_pending[-1].finish()
    assert pane.test_queries == [(pane.test_current['config'], {'query': '제주 여행', 'limit': 50, 'offset': 0})]
    assert pane._busy
    pane._poll()
    assert not pane._busy
    assert pane.page_note.get() == '검색 결과 없음'
    assert not pane.tree.get_children()
    assert pane.previous_button.instate(['disabled'])
    assert pane.next_button.instate(['disabled'])


def test_catalog_search_runs_in_worker_thread_without_tk_operations(pane, monkeypatch):
    threads, workers = [], []

    def thread(**kwargs):
        worker = Thread(**kwargs)
        workers.append(worker)
        return worker

    def search(config, **kwargs):
        threads.append(get_ident())
        return result([row()])

    monkeypatch.setattr(library_ui, 'Thread', thread)
    monkeypatch.setattr(catalog, 'search_archive', search)
    pane.refresh()
    workers[0].join(timeout=2)
    assert not workers[0].is_alive()
    assert threads and threads[0] != get_ident()
    assert not pane.records
    pane._poll()
    assert len(pane.records) == 1


def test_stale_search_results_and_errors_do_not_replace_new_blog_results(pane, tmp_path):
    pane.refresh()
    first_request = pane._request
    old_config = pane.test_current['config']
    new_config = Config('new_blog', tmp_path / 'new')
    pane.test_current['config'] = new_config
    pane.refresh()
    pane._events.put((pane._request, new_config, result([row(2, blog_id='new_blog')]), None))
    pane._events.put((first_request, old_config, result([row(1)]), None))
    pane._events.put((first_request, old_config, None, 'outdated error'))
    pane._poll()
    assert pane.result_config == new_config
    assert [item['archive_id'] for item in pane.records.values()] == ['archive-2']
    assert 'new_blog' in pane.summary.get()
    assert 'outdated error' not in pane.summary.get()


def test_search_error_clears_previous_results_and_restores_search(pane, monkeypatch):
    show_result(pane, [row()])

    def search(_config, **kwargs):
        raise ValueError('지원하지 않는 상태 DB 버전')

    monkeypatch.setattr(catalog, 'search_archive', search)
    pane.refresh()
    assert not pane.records and pane.result_config is None
    pane.test_pending[-1].finish()
    pane._poll()
    assert '지원하지 않는 상태 DB 버전' in pane.summary.get()
    assert not pane._busy
    assert not pane.search_button.instate(['disabled'])
    assert pane.next_button.instate(['disabled'])


def test_invalid_configuration_invalidates_in_flight_search(pane):
    pane.refresh()
    request, config = pane._request, pane.test_current['config']

    def invalid_config():
        raise ValueError('블로그 주소를 확인하세요')

    pane.config_provider = invalid_config
    pane.refresh()
    pane._events.put((request, config, result([row()]), None))
    pane._poll()
    assert not pane.records
    assert pane.result_config is None
    assert pane.summary.get() == '블로그 주소를 확인하세요'
    assert not pane._busy


def test_pagination_requests_next_offset_and_resets_after_new_search(pane):
    show_result(pane, [row(index) for index in range(1, 51)], total=51)
    assert pane.page_note.get() == '1–50 / 51'
    assert pane.previous_button.instate(['disabled'])
    assert not pane.next_button.instate(['disabled'])
    pane.change_page(1)
    pane.test_pending[-1].finish()
    assert pane.test_queries[-1][1]['offset'] == 50
    pane._events.put((pane._request, pane.test_current['config'], result([row(51)], total=51, offset=50), None))
    pane._poll()
    assert pane.page_note.get() == '51–51 / 51'
    assert not pane.previous_button.instate(['disabled'])
    assert pane.next_button.instate(['disabled'])
    pane.query.set('different text')
    pane.refresh()
    pane.test_pending[-1].finish()
    assert pane.test_queries[-1][1]['offset'] == 0
    assert pane.test_queries[-1][1]['query'] == 'different text'


@pytest.mark.parametrize('changed', ['query', 'config'])
def test_page_navigation_after_search_conditions_change_starts_at_first_page(pane, tmp_path, changed):
    show_result(pane, [row(index) for index in range(1, 51)], total=70)
    if changed == 'query':
        pane.query.set('new search')
    else:
        pane.test_current['config'] = Config('different_blog', tmp_path / 'different')
    pane.change_page(1)
    pane.test_pending[-1].finish()
    assert pane.test_queries[-1][1]['offset'] == 0


def test_saved_file_open_uses_result_configuration_when_form_has_changed(pane, tmp_path, monkeypatch):
    original = pane.test_current['config']
    file = original.out_dir / 'posts' / '1.md'
    file.parent.mkdir(parents=True)
    file.write_text('Saved post', encoding='utf-8')
    show_result(pane, [row()])
    pane.test_current['config'] = Config('different_blog', tmp_path / 'different')
    select_first(pane)
    opened = []
    monkeypatch.setattr(library_ui.sys, 'platform', 'win32')
    monkeypatch.setattr(library_ui.os, 'startfile', opened.append, raising=False)
    pane.open_selected()
    assert [Path(path) for path in opened] == [file]
    assert pane.test_dialogs == []


@pytest.mark.parametrize('path', ['posts/missing.md', '../outside.md'])
def test_file_open_rejects_missing_and_escaped_paths(pane, tmp_path, monkeypatch, path):
    (tmp_path / 'outside.md').write_text('Outside the configured archive', encoding='utf-8')
    show_result(pane, [row(path=path)])
    select_first(pane)
    opened = []
    monkeypatch.setattr(library_ui.sys, 'platform', 'win32')
    monkeypatch.setattr(library_ui.os, 'startfile', opened.append, raising=False)
    pane.open_selected()
    assert opened == []
    assert pane.test_dialogs[-1][0] == '파일 열기 실패'


def test_file_open_rejects_existing_non_markdown_file(pane, monkeypatch):
    file = pane.test_current['config'].out_dir / 'posts' / '1.exe'
    file.parent.mkdir(parents=True)
    file.write_bytes(b'not a saved Markdown post')
    show_result(pane, [row(path='posts/1.exe')])
    select_first(pane)
    opened = []
    monkeypatch.setattr(library_ui.sys, 'platform', 'win32')
    monkeypatch.setattr(library_ui.os, 'startfile', opened.append, raising=False)
    pane.open_selected()
    assert opened == []
    assert pane.test_dialogs[-1][0] == '파일 열기 실패'


def test_copy_id_and_post_details_use_selected_archive_record(pane, monkeypatch):
    show_result(pane, [row(status='partial')])
    select_first(pane)
    selected = pane.tree.selection()[0]
    assert tuple(pane.tree['displaycolumns']) == ('title', 'status', 'blog', 'id')
    assert pane.tree.set(selected, 'title') == 'Saved post 1'
    assert pane.tree.set(selected, 'status') == '부분 완료'
    clipboard = []
    monkeypatch.setattr(pane, 'clipboard_clear', clipboard.clear)
    monkeypatch.setattr(pane, 'clipboard_append', clipboard.append)
    pane.more_menu.invoke('ID 복사')
    assert clipboard == ['archive-1']
    pane.more_menu.invoke('글 정보')
    title, details = pane.test_dialogs[-1]
    assert title == '저장 글 정보'
    assert all(value in details for value in ('archive-1', '1001', 'demo_blog', '부분 완료', '2026-08-01', 'posts/1.md'))


def test_post_actions_without_selection_explain_how_to_select(pane):
    pane.show_details()
    pane.open_selected()
    pane.copy_id()
    pane.preview_selected()
    assert len(pane.test_dialogs) == 4
    assert all(title == '글 선택' for title, _ in pane.test_dialogs)


def test_picture_reader_renders_in_worker_and_opens_only_after_queue_poll(pane, tmp_path, monkeypatch):
    original = pane.test_current['config']
    preview = (tmp_path / '보관함 읽기.html').resolve()
    calls, opened = [], []

    def render(config, record):
        calls.append((config, record))
        return preview

    monkeypatch.setattr(reader, 'create_preview', render)
    monkeypatch.setattr(library_ui.webbrowser, 'open', lambda url: opened.append(url) or True)
    show_result(pane, [row()])
    select_first(pane)
    pane.test_current['config'] = Config('different_blog', tmp_path / 'different')
    pane.preview_selected()
    assert pane.preview_button.instate(['disabled'])
    assert not calls and not opened
    pane.test_pending[-1].finish()
    assert calls == [(original, row())]
    assert not opened
    pane._poll()
    assert opened == [preview.as_uri()]
    assert not pane.preview_button.instate(['disabled'])
    assert not pane._preview_busy
    assert not pane.test_dialogs


def test_reader_error_is_reported_and_can_be_retried(pane, monkeypatch):
    def render(config, record):
        raise RuntimeError('이 저장 폴더를 사용하는 백업 작업이 이미 실행 중입니다.')

    monkeypatch.setattr(reader, 'create_preview', render)
    show_result(pane, [row()])
    select_first(pane)
    pane.preview_selected()
    previous_count = len(pane.test_pending)
    pane.preview_selected()
    assert len(pane.test_pending) == previous_count
    pane.test_pending[-1].finish()
    pane._poll()
    assert pane.test_dialogs[-1][0] == '읽기 화면 열기 실패'
    assert '이미 실행 중' in pane.test_dialogs[-1][1]
    assert not pane._preview_busy
    assert not pane.preview_button.instate(['disabled'])


def test_reader_browser_launch_failure_is_reported(pane, tmp_path, monkeypatch):
    monkeypatch.setattr(reader, 'create_preview', lambda *args: (tmp_path / 'preview.html').resolve())
    monkeypatch.setattr(library_ui.webbrowser, 'open', lambda url: False)
    show_result(pane, [row()])
    select_first(pane)
    pane.preview_selected()
    pane.test_pending[-1].finish()
    pane._poll()
    assert pane.test_dialogs[-1][0] == '읽기 화면 열기 실패'
    assert '브라우저' in pane.test_dialogs[-1][1]
