"""Worker lifecycle tests: no browser, network access, or Tk event loop."""
from queue import Empty
from threading import Event, current_thread

import pytest

from naver_blog_archive import archive
from naver_blog_archive.config import Config
from naver_blog_archive.jobs import JobRunner
from naver_blog_archive.progress import OperationCancelled


def finish(runner):
    runner._thread.join(timeout=3)
    assert not runner.busy, 'archive worker did not terminate'
    events = []
    while True:
        try:
            events.append(runner.events.get_nowait())
        except Empty:
            break
    terminal = [event for event in events if event['kind'] == 'done']
    assert len(terminal) == 1
    assert events[-1] == terminal[0]
    return events, terminal[0]


@pytest.mark.parametrize('status', ['success', 'partial'])
def test_backup_runs_off_caller_thread_and_forwards_progress_and_result(tmp_path, monkeypatch, status):
    config = Config('demo', tmp_path)
    report = {'last_run': {'status': status}}
    caller = current_thread()

    def backup(actual, *, refresh, control):
        assert current_thread() is not caller
        assert actual == config
        assert refresh is True
        control.emit('posts', '글 저장 중', completed=2, total=3)
        return report

    monkeypatch.setattr(archive, 'backup', backup)
    runner = JobRunner()
    runner.start('backup', config, refresh=True)
    events, done = finish(runner)
    assert events[0] == {
        'kind': 'progress', 'phase': 'posts', 'message': '글 저장 중',
        'completed': 2, 'total': 3,
    }
    assert done['action'] == 'backup'
    assert done['status'] == status
    assert done['report'] is report


@pytest.mark.parametrize('error', [RuntimeError('network failed'), SystemExit(7)])
def test_worker_error_is_a_terminal_event_and_allows_another_job(tmp_path, monkeypatch, error):
    def failing_backup(*args, **kwargs):
        raise error

    monkeypatch.setattr(archive, 'backup', failing_backup)
    runner = JobRunner()
    runner.start('backup', Config('demo', tmp_path))
    _, done = finish(runner)
    assert done['status'] == 'error'
    assert type(error).__name__ in done['message']
    assert str(error) in done['message']
    assert done['report'] is None

    report = {'last_run': {'status': 'success'}}
    monkeypatch.setattr(archive, 'backup', lambda *args, **kwargs: report)
    runner.start('backup', Config('demo', tmp_path))
    assert finish(runner)[1]['status'] == 'success'


def test_cancel_preserves_partial_report_and_restarting_gets_fresh_control(tmp_path, monkeypatch):
    entered = Event()
    report = {'last_run': {'status': 'interrupted'}, 'posts': {'pending': 1}}

    def interrupted_backup(config, *, refresh, control):
        entered.set()
        try:
            control.wait(5)
        except OperationCancelled as exc:
            exc.report = report
            raise
        pytest.fail('cancellation did not interrupt the worker')

    monkeypatch.setattr(archive, 'backup', interrupted_backup)
    runner = JobRunner()
    config = Config('demo', tmp_path)
    runner.start('backup', config)
    try:
        assert entered.wait(2)
    finally:
        runner.cancel()
    _, done = finish(runner)
    assert done['status'] == 'interrupted'
    assert done['report'] is report
    previous_control = runner.control

    def resumed_backup(config, *, refresh, control):
        assert control is not previous_control
        control.check()
        return {'last_run': {'status': 'success'}}

    monkeypatch.setattr(archive, 'backup', resumed_backup)
    runner.start('backup', config)
    assert finish(runner)[1]['status'] == 'success'


def test_an_active_job_rejects_concurrent_operations_without_replacing_control(tmp_path, monkeypatch):
    entered, release = Event(), Event()

    def waiting_backup(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return {'last_run': {'status': 'success'}}

    monkeypatch.setattr(archive, 'backup', waiting_backup)
    runner = JobRunner()
    config = Config('demo', tmp_path)
    runner.start('backup', config)
    try:
        assert entered.wait(2)
        assert runner.busy
        control = runner.control
        with pytest.raises(RuntimeError, match='이미 작업'):
            runner.start('verify', config)
        assert runner.control is control
    finally:
        release.set()
        finish(runner)


@pytest.mark.parametrize(('action', 'config', 'message'), [
    ('unsupported', None, '지원하지 않는'),
    ('backup', None, '설정이 필요'),
    ('verify', None, '설정이 필요'),
    ('status', None, '설정이 필요'),
])
def test_invalid_job_is_rejected_before_creating_a_worker(action, config, message):
    runner = JobRunner()
    with pytest.raises(ValueError, match=message):
        runner.start(action, config)
    assert not runner.busy
    assert runner.control is None
    assert runner.events.empty()


@pytest.mark.parametrize(('action', 'changes', 'expected'), [
    ('status', {'failures': [{'status': 'failed'}]}, 'success'),
    ('verify', {}, 'success'),
    ('verify', {'verification_problems': ['missing Markdown']}, 'partial'),
    ('verify', {'failures': [{'status': 'pending'}]}, 'partial'),
    ('verify', {'last_run': {'listing_complete': False}}, 'partial'),
    ('verify', {'last_run': None}, 'partial'),
])
def test_inspection_classifies_incomplete_archives_without_starting_backup(tmp_path, monkeypatch, action, changes, expected):
    report = {'last_run': {'listing_complete': True}, **changes}
    calls = []

    def inspect(config, *, verify, control):
        control.check()
        calls.append(verify)
        return report

    def forbidden_backup(*args, **kwargs):
        pytest.fail('inspection must not start a backup')

    monkeypatch.setattr(archive, 'inspect_archive', inspect)
    monkeypatch.setattr(archive, 'backup', forbidden_backup)
    runner = JobRunner()
    runner.start(action, Config('demo', tmp_path))
    _, done = finish(runner)
    assert calls == [action == 'verify']
    assert done['status'] == expected
    assert done['report'] is report


@pytest.mark.parametrize(('exit_code', 'status'), [(0, 'success'), (2, 'error')])
def test_environment_diagnostics_need_no_blog_and_forward_console_output(monkeypatch, exit_code, status):
    from naver_blog_archive import cli

    def doctor():
        print('Browser diagnostic details')
        return exit_code

    monkeypatch.setattr(cli, 'doctor', doctor)
    runner = JobRunner()
    runner.start('doctor')
    events, done = finish(runner)
    assert events[0]['message'] == 'Browser diagnostic details'
    assert done['status'] == status
    assert done['report'] is None


@pytest.mark.parametrize('problems,failures,status', [([], [], 'success'), (['protected'], [], 'partial'),
                                                   ([], [{'status': 'partial'}], 'partial')])
def test_reformat_runs_off_thread_and_reports_changes_without_a_backup(tmp_path, monkeypatch, problems, failures, status):
    caller = current_thread()
    report = {'output_counts': {'renamed': 7, 'updated': 3, 'navigation_posts': 2, 'cards': 4},
              'output_problems': problems, 'failures': failures}
    def reformat(config, *, control):
        assert current_thread() is not caller
        control.check()
        return report
    monkeypatch.setattr(archive, 'reformat_archive', reformat)
    runner = JobRunner()
    runner.start('reformat', Config('demo', tmp_path), refresh=True)
    _, done = finish(runner)
    assert done['status'] == status
    assert done['report'] is report
    assert '7개' in done['message'] and '3회' in done['message']
    assert '블로그 메뉴 2개 글에서 제거' in done['message']
    assert '링크 카드 4개 정리' in done['message']
