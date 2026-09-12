"""Exercise desktop actions with a withdrawn Tk root and an in-memory worker."""
from queue import Queue

import pytest

tk = pytest.importorskip('tkinter', reason='GUI tests require Python with tkinter')

from naver_blog_archive import gui
from naver_blog_archive.config import Config, load_config, save_config


class FakeRunner:
    def __init__(self):
        self.events = Queue()
        self.starts = []
        self.cancelled = False

    def start(self, action, config, *, refresh):
        self.starts.append((action, config, refresh))

    def cancel(self):
        self.cancelled = True


@pytest.fixture
def app(desktop, tmp_path, monkeypatch):
    root = tk.Toplevel(desktop)
    root.withdraw()
    destroy_root = root.destroy
    dialogs = []
    monkeypatch.setattr(gui.messagebox, 'showerror', lambda *args, **kwargs: dialogs.append(args))
    monkeypatch.setattr(gui.messagebox, 'showinfo', lambda *args, **kwargs: dialogs.append(args))
    application = gui.ArchiveApp(root, tmp_path / 'config.toml', runner=FakeRunner())
    application.test_dialogs = dialogs
    try:
        yield application
    finally:
        try:
            application.progress.stop()
            for callback in root.tk.call('after', 'info'):
                root.tk.call('after', 'cancel', callback)
            destroy_root()
        except tk.TclError:
            pass


def set_valid_inputs(app):
    app.blog.set('https://m.blog.naver.com/demo_blog/123456')
    app.folder.set('저장 폴더')


def test_form_normalizes_url_and_preserves_advanced_settings_and_options(app):
    set_valid_inputs(app)
    app.images.set(False)
    app.files.set(False)
    app.sources.set(False)
    app._advanced.update(delay=1.5, retries=7, source_depth=2, max_pages=321, max_file_mb=250)
    config = app.read_config()
    assert config.blog_id == 'demo_blog'
    assert config.out_dir == app.config_path.parent / '저장 폴더'
    assert not config.download_images
    assert not config.download_files
    assert config.max_file_mb == 250
    assert not config.follow_sources
    assert (config.delay, config.retries, config.source_depth, config.max_pages) == (1.5, 7, 2, 321)


@pytest.mark.parametrize(('field', 'value'), [('blog', ''), ('blog', 'https://example.com/demo'), ('folder', '  ')])
def test_invalid_inputs_show_error_and_never_save_or_start(app, field, value):
    set_valid_inputs(app)
    getattr(app, field).set(value)
    app.start('backup')
    assert app.test_dialogs
    assert app.runner.starts == []
    assert not app.running
    assert not app.config_path.exists()
    assert not app.start_button.instate(['disabled'])


def test_backup_autosaves_then_locks_settings_until_completion(app):
    set_valid_inputs(app)
    app.refresh.set(True)
    app.start('backup')
    assert len(app.runner.starts) == 1
    action, config, refresh = app.runner.starts[0]
    assert action == 'backup' and refresh is True
    assert load_config(app.config_path) == config
    assert app.profile_store.list() == [config]
    assert app.profile_choice.get() == config.blog_id
    assert app.running
    assert all(widget.instate(['disabled']) for widget in app._input_widgets + app._operation_buttons)
    assert app.profile_box.instate(['disabled'])
    assert not app.stop_button.instate(['disabled'])
    app.start('verify')
    assert len(app.runner.starts) == 1


def test_settings_write_failure_leaves_form_ready_and_does_not_start(app, monkeypatch):
    set_valid_inputs(app)

    def cannot_save(*args):
        raise OSError('disk is full')

    monkeypatch.setattr(gui, 'save_config', cannot_save)
    app.start('backup')
    assert app.runner.starts == []
    assert not app.running
    assert 'disk is full' in app.test_dialogs[-1][1]


def test_runner_start_failure_is_visible_and_does_not_lock_form(app, monkeypatch):
    set_valid_inputs(app)

    def cannot_start(*args, **kwargs):
        raise RuntimeError('worker unavailable')

    monkeypatch.setattr(app.runner, 'start', cannot_start)
    app.start('verify')
    assert not app.running
    assert not app.start_button.instate(['disabled'])
    assert app.stop_button.instate(['disabled'])
    assert 'worker unavailable' in app.test_dialogs[-1][1]


@pytest.mark.parametrize('action', ['verify', 'status', 'reformat'])
def test_inspection_uses_current_inputs_without_autosaving(app, action):
    set_valid_inputs(app)
    app.start(action)
    assert app.runner.starts[0][0] == action
    assert app.runner.starts[0][1].blog_id == 'demo_blog'
    assert not app.config_path.exists()


def test_diagnostics_work_before_blog_setup_and_keep_previous_report(app):
    report = {'posts': {'success': 9}}
    app._show_report(report)
    app.start('doctor')
    assert app.runner.starts == [('doctor', None, False)]
    assert app.running
    assert app.stop_button.instate(['disabled'])
    assert app.last_report == report
    assert app.counts['success'].get() == '9'
    assert app.test_dialogs == []


def test_stop_requests_cooperative_cancellation_and_waits_for_worker(app):
    set_valid_inputs(app)
    app.start('backup')
    app.stop()
    assert app.runner.cancelled
    assert app.running
    assert app.stop_button.instate(['disabled'])
    assert app.start_button.instate(['disabled'])
    phase = app.phase.get()
    detail = app.detail.get()
    app._handle_event({'kind': 'progress', 'message': 'finishing current request', 'completed': 1, 'total': 2})
    assert app.phase.get() == phase
    assert app.detail.get() == detail
    assert 'finishing current request' in app.log.get('1.0', 'end')


def test_progress_queue_updates_counts_and_percentage(app):
    set_valid_inputs(app)
    app.start('backup')
    app.runner.events.put({
        'kind': 'progress', 'message': '글 저장 중', 'completed': 3, 'total': 4,
        'counts': {'success': 2, 'partial': 1, 'pending': 2, 'running': 1},
    })
    app._poll()
    assert app.phase.get() == '글 저장 중'
    assert str(app.progress['mode']) == 'determinate'
    assert app.progress['value'] == 75
    assert {key: value.get() for key, value in app.counts.items()} == {
        'success': '2', 'partial': '1', 'failed': '0', 'pending': '3',
    }


def test_listing_phase_does_not_leave_verification_progress_at_one_hundred_percent(app):
    set_valid_inputs(app)
    app.start('backup')
    app._handle_event({'kind': 'progress', 'phase': 'verify', 'completed': 3, 'total': 3})
    assert app.progress['value'] == 100
    app._handle_event({'kind': 'progress', 'phase': 'listing', 'message': '글 목록 확인 중'})
    assert str(app.progress['mode']) == 'indeterminate'
    assert app.progress['value'] != 100


@pytest.mark.parametrize('status', ['success', 'partial', 'interrupted', 'error'])
def test_terminal_event_restores_controls_and_renders_result(app, status):
    set_valid_inputs(app)
    app.start('backup')
    report = {'posts': {'success': 3}, 'last_run': {'status': status, 'listing_complete': True}}
    app.runner.events.put({
        'kind': 'done', 'action': 'backup', 'status': status,
        'message': '작업 결과 상세', 'report': report,
    })
    app._poll()
    assert not app.running
    assert all(not widget.instate(['disabled']) for widget in app._input_widgets + app._operation_buttons)
    assert str(app.profile_box['state']) == 'readonly'
    assert app.stop_button.instate(['disabled'])
    assert app.last_report == report
    assert app.counts['success'].get() == '3'
    assert app.detail.get() == '작업 결과 상세'
    assert gui.STATUS_NAMES[status] in app.phase.get()
    assert app.progress['value'] == (100 if status == 'success' else 0)


def test_report_exposes_post_asset_verification_and_listing_failures(app):
    app._show_report({
        'posts': {'partial': 1, 'failed': 1, 'pending': 2, 'running': 1},
        'failures': [{'blog_id': 'demo', 'post_id': '123', 'status': 'partial', 'error': None}],
        'assets': [{'url': 'https://example.test/image.jpg', 'error': 'image timeout'}],
        'verification_problems': ['Markdown file missing'],
        'last_run': {'status': 'interrupted', 'listing_complete': False, 'error': 'stopped'},
    })
    rows = [app.issues.item(item, 'values') for item in app.issues.get_children()]
    assert len(rows) == 5
    assert rows[0][0] == 'demo/123'
    assert rows[0][2] == '다음 백업에서 다시 처리합니다.'
    assert rows[1][2] == 'image timeout'
    assert rows[2][2] == 'Markdown file missing'
    assert rows[3][2] == 'stopped'
    assert '끝까지' in rows[4][2]
    assert app.tabs.index(app.tabs.select()) == 0
    assert app.counts['pending'].get() == '3'
    app.issues.selection_set(app.issues.get_children()[1])
    app.show_issue()
    assert 'image timeout' in app.test_dialogs[-1][1]


def test_load_settings_switches_active_file_and_save_uses_that_location(app, tmp_path, monkeypatch):
    target = tmp_path / 'settings' / '다른 설정.toml'
    expected = Config('another_blog', tmp_path / '보관함', delay=2, download_images=False,
                      download_files=False, max_file_mb=250)
    save_config(expected, target)
    monkeypatch.setattr(gui.filedialog, 'askopenfilename', lambda **kwargs: str(target))
    app.load_settings()
    assert app.config_path == target
    assert app.read_config() == expected
    assert str(target) in app.settings_note.get()
    app.sources.set(False)
    app.save_settings()
    assert load_config(target).follow_sources is False
    assert not (tmp_path / 'config.toml').exists()


def test_invalid_loaded_file_preserves_current_settings_and_path(app, tmp_path, monkeypatch):
    set_valid_inputs(app)
    previous = app.read_config()
    target = tmp_path / 'broken.toml'
    target.write_text('blog_id = [', encoding='utf-8')
    original_path = app.config_path
    monkeypatch.setattr(gui.filedialog, 'askopenfilename', lambda **kwargs: str(target))
    app.load_settings()
    assert app.config_path == original_path
    assert app.read_config() == previous
    assert app.test_dialogs


def test_folder_picker_and_open_use_selected_path(app, tmp_path, monkeypatch):
    selected = tmp_path / '내 백업'
    monkeypatch.setattr(gui.filedialog, 'askdirectory', lambda **kwargs: str(selected))
    app.choose_folder()
    assert app.folder.get() == str(selected)
    opened = []
    monkeypatch.setattr(gui, 'open_folder', opened.append)
    app.show_folder()
    assert opened == [selected]


def test_closing_active_backup_waits_for_worker_before_destroying_root(app, monkeypatch):
    set_valid_inputs(app)
    app.start('backup')
    destroyed = []
    monkeypatch.setattr(app.root, 'destroy', lambda: destroyed.append(True))
    monkeypatch.setattr(gui.messagebox, 'askyesno', lambda *args, **kwargs: True)
    app.on_close()
    assert app.runner.cancelled
    assert app.close_when_done
    assert destroyed == []
    app.runner.events.put({
        'kind': 'done', 'action': 'backup', 'status': 'interrupted',
        'message': 'stopped', 'report': None,
    })
    app._poll()
    assert destroyed == [True]


@pytest.mark.parametrize('selected_tab', [0, 1, 2])
def test_start_and_failure_report_preserve_users_selected_tab(app, monkeypatch, selected_tab):
    set_valid_inputs(app)
    refreshes = []
    monkeypatch.setattr(app.library, 'refresh', lambda: refreshes.append(True))
    app.tabs.select(selected_tab)
    app.start('backup')
    assert app.tabs.index(app.tabs.select()) == selected_tab
    app._handle_event({
        'kind': 'done', 'action': 'backup', 'status': 'partial', 'message': 'Some posts need attention',
        'report': {'failures': [{'blog_id': 'demo_blog', 'post_id': '123', 'status': 'failed', 'error': 'timeout'}]},
    })
    assert app.tabs.index(app.tabs.select()) == selected_tab
    assert '1' in app.tabs.tab(1, 'text')
    assert bool(refreshes) == (selected_tab == 2)


def test_profile_saves_and_restores_each_blogs_folder_and_options(app, tmp_path):
    set_valid_inputs(app)
    app.images.set(False)
    app.files.set(False)
    app.sources.set(False)
    app._advanced.update(delay=2.5, retries=6, max_file_mb=2048)
    app.refresh.set(True)
    expected = app.read_config()
    app.save_profile()
    assert app.profile_choice.get() == 'demo_blog'
    assert gui.ProfileStore(app.profile_store.path).list() == [expected]
    assert str(app.profile_box['state']) == 'readonly'
    app.blog.set('another_blog')
    app.folder.set(str(tmp_path / 'another'))
    app.images.set(True)
    app.files.set(True)
    app.sources.set(True)
    app.save_profile()
    assert set(app.profile_box['values']) == {'demo_blog', 'another_blog'}
    assert len(app.profile_store.list()) == 2
    app.profile_choice.set('demo_blog')
    app.select_profile()
    assert app.read_config() == expected
    assert not app.refresh.get()
    assert 'demo_blog' in app.phase.get()


def test_profile_selection_clears_previous_blog_results_and_refreshes_visible_library(app, monkeypatch, tmp_path):
    config = Config('saved_blog', tmp_path / 'saved')
    app.profile_store.save(config)
    app._reload_profiles()
    app._show_report({'posts': {'success': 42}, 'failures': [
        {'blog_id': 'old_blog', 'post_id': '123', 'status': 'failed', 'error': 'old error'},
    ]})
    refreshes = []
    monkeypatch.setattr(app.library, 'refresh', lambda: refreshes.append(app.read_config()))
    app.tabs.select(2)
    app.profile_choice.set('saved_blog')
    app.select_profile()
    assert app.last_report is None
    assert not app.issues.get_children()
    assert app.counts['success'].get() == '0'
    assert app.tabs.index(app.tabs.select()) == 2
    assert refreshes == [config]


def test_profile_cannot_switch_active_backup_configuration(app, tmp_path):
    app.profile_store.save(Config('different_blog', tmp_path / 'different'))
    app._reload_profiles()
    set_valid_inputs(app)
    expected = app.read_config()
    app.start('backup')
    app.profile_choice.set('different_blog')
    app.select_profile()
    assert app.read_config() == expected
    assert app.profile_box.instate(['disabled'])
    assert app.profile_button.instate(['disabled'])


def test_invalid_profile_is_not_saved(app):
    app.blog.set('https://example.com/not-a-blog')
    app.save_profile()
    assert app.test_dialogs[-1][0] == '블로그 저장 실패'
    assert app.profile_store.list() == []
    assert not app.profile_store.path.exists()


def test_profile_storage_failure_is_visible_and_does_not_start_worker(app, monkeypatch):
    set_valid_inputs(app)

    def cannot_save(_config):
        raise gui.sqlite3.OperationalError('database is locked')

    monkeypatch.setattr(app.profile_store, 'save', cannot_save)
    app.start('backup')
    assert app.runner.starts == []
    assert not app.running
    assert str(app.profile_box['state']) == 'readonly'
    assert 'database is locked' in app.test_dialogs[-1][1]


def test_run_summary_distinguishes_new_saves_from_reused_archive_files(app):
    app._handle_event({'kind': 'progress', 'run_counts': {'saved': 2, 'reused': 1250, 'failed': 1}})
    assert app.run_summary.get() == '이번 실행 · 저장/복구 2개 · 기존 파일 건너뜀 1,250개 · 확인 필요 1개'
    app._show_report({'posts': {'success': 1253}, 'run_counts': {'saved': 3, 'reused': 1250, 'failed': 0}})
    assert app.counts['success'].get() == '1253'
    assert app.run_summary.get() == '이번 실행 · 저장/복구 3개 · 기존 파일 건너뜀 1,250개 · 확인 필요 0개'


def test_attachment_checkbox_is_enabled_by_default_and_independent_of_images(app):
    set_valid_inputs(app)
    assert app.files.get() is True
    checkboxes = {widget['text']: widget for widget in app._input_widgets
                  if isinstance(widget, gui.ttk.Checkbutton)}
    assert len(checkboxes) == 4
    checkboxes['이미지 저장'].invoke()
    assert app.read_config().download_images is False
    assert app.read_config().download_files is True
    checkboxes['첨부파일 저장'].invoke()
    assert app.read_config().download_files is False
    assert app.sources.get() is True
    assert app.refresh.get() is False
    positions = {(widget.grid_info()['row'], widget.grid_info()['column'])
                 for widget in checkboxes.values()}
    assert positions == {(0, 0), (0, 1), (1, 0), (1, 1)}


def test_advanced_attachment_limit_validates_and_apply_does_not_cover_fields(app, monkeypatch):
    create_dialog = gui.tk.Toplevel

    def hidden_dialog(*args, **kwargs):
        dialog = create_dialog(*args, **kwargs)
        dialog.withdraw()
        return dialog

    monkeypatch.setattr(gui.tk, 'Toplevel', hidden_dialog)
    app.advanced_settings()
    dialog = next(child for child in app.root.winfo_children() if isinstance(child, create_dialog))
    form = dialog.winfo_children()[0]
    file_label = next(child for child in form.winfo_children()
                      if isinstance(child, gui.ttk.Label) and child['text'].startswith('첨부파일 최대 크기'))
    row = file_label.grid_info()['row']
    entry = form.grid_slaves(row=row, column=1)[0]
    apply_button = next(child for child in form.winfo_children() if isinstance(child, gui.ttk.Button))
    last_entry_row = max(child.grid_info()['row'] for child in form.winfo_children()
                         if isinstance(child, gui.ttk.Entry))
    assert apply_button.grid_info()['row'] > last_entry_row
    entry.delete(0, 'end')
    entry.insert(0, '2049')
    apply_button.invoke()
    assert app.test_dialogs[-1][0] == '세부 설정 오류'
    assert app._advanced['max_file_mb'] == 100
    assert dialog.winfo_exists()
    entry.delete(0, 'end')
    entry.insert(0, '2048')
    apply_button.invoke()
    assert app._advanced['max_file_mb'] == 2048
    assert not dialog.winfo_exists()


def test_report_distinguishes_attachment_failures_and_cached_link_updates(app):
    app._show_report({
        'assets': [
            {'url': 'https://example.test/photo.jpg', 'error': 'image timeout'},
            {'url': 'https://example.test/report.pdf', 'kind': 'file', 'error': None},
        ],
        'output_counts': {'renamed': 0, 'updated': 1, 'cards': 2, 'files': 3},
    })
    rows = [app.issues.item(item, 'values') for item in app.issues.get_children()]
    assert rows[0][1:] == ('이미지', 'image timeout')
    assert rows[1][1:] == ('첨부파일', '첨부파일 저장 미완료')
    assert '링크 카드 2개 정리' in app.run_summary.get()
    assert '첨부 링크 3개 정리' in app.run_summary.get()


def test_premium_address_enables_login_and_incomplete_or_blog_input_disables_it(app):
    assert app.login_button.instate(['disabled'])
    app.blog.set('https://contents.premium.naver.com/salarymoney')
    assert app.login_button.instate(['disabled'])
    assert not app.test_dialogs
    app.blog.set('https://contents.premium.naver.com/salarymoney/moneystock')
    assert not app.login_button.instate(['disabled'])
    assert app.read_config().blog_id == 'premium/salarymoney/moneystock'
    assert '전용 브라우저' in app.login_note.get()
    app.blog.set('salarymoney')
    assert app.login_button.instate(['disabled'])


def test_premium_login_keeps_backup_settings_and_previous_result(app, monkeypatch):
    app.blog.set('https://contents.premium.naver.com/salarymoney/moneystock')
    app.refresh.set(True)
    report = {'posts': {'success': 42}}
    app._show_report(report)
    app.tabs.select(2)
    app.library.query.set('기존 검색어')
    refreshes = []
    monkeypatch.setattr(app.library, 'refresh', lambda: refreshes.append(True))
    app.login_button.invoke()
    assert len(app.runner.starts) == 1
    action, config, refresh = app.runner.starts[0]
    assert action == 'login'
    assert config.blog_id == 'premium/salarymoney/moneystock'
    assert refresh is False
    assert app.running
    assert app.login_button.instate(['disabled'])
    assert not app.stop_button.instate(['disabled'])
    assert app.last_report == report
    assert app.counts['success'].get() == '42'
    assert app.tabs.index(app.tabs.select()) == 2
    assert app.library.query.get() == '기존 검색어'
    assert not refreshes
    assert not app.config_path.exists()
    assert not app.profile_store.path.exists()
    app._handle_event({'kind': 'done', 'action': 'login', 'status': 'success',
                       'message': '로그인 정보를 저장했습니다.', 'report': None})
    assert not app.running
    assert not app.login_button.instate(['disabled'])
    assert app.last_report == report
    assert app.counts['success'].get() == '42'
    assert app.tabs.index(app.tabs.select()) == 2
    assert app.library.query.get() == '기존 검색어'
    assert not refreshes


def test_blog_cannot_start_premium_login(app):
    set_valid_inputs(app)
    app.start('login')
    assert not app.running
    assert app.runner.starts == []
    assert '프리미엄 채널 주소' in app.test_dialogs[-1][1]


def test_premium_profile_restores_readable_channel_address_and_options(app, tmp_path):
    address = 'https://contents.premium.naver.com/salarymoney/moneystock'
    app.blog.set(address)
    app.folder.set(str(tmp_path / 'premium'))
    app.files.set(False)
    app.save_profile()
    expected = app.read_config()
    assert app.profile_choice.get() == address
    assert app.profile_store.list() == [expected]
    app.blog.set('salarymoney')
    app.folder.set(str(tmp_path / 'blog'))
    app.files.set(True)
    app.save_profile()
    assert set(app.profile_box['values']) == {'salarymoney', address}
    app.profile_choice.set(address)
    app.select_profile()
    assert app.blog.get() == address
    assert app.read_config() == expected
    assert not app.login_button.instate(['disabled'])
    assert app.files.get() is False


def test_premium_backup_disables_login_until_worker_finishes(app):
    app.blog.set('https://contents.premium.naver.com/salarymoney/moneystock')
    app.start('backup')
    assert app.login_button.instate(['disabled'])
    assert len(app.runner.starts) == 1
    app.login_button.invoke()
    assert len(app.runner.starts) == 1
    app._handle_event({'kind': 'done', 'action': 'backup', 'status': 'success',
                       'message': '백업 완료', 'report': None})
    assert not app.login_button.instate(['disabled'])
