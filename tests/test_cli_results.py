from dataclasses import replace
from naver_blog_archive.cli import main
from naver_blog_archive.network import Listing
import pytest


def test_cli_invalid_config_returns_error(tmp_path):
    assert main(['backup', '--config', str(tmp_path / 'missing.toml')]) == 2


def test_cli_partial_backup_exit_code(tmp_path, monkeypatch):
    path = tmp_path / 'config.toml'
    path.write_text('blog_id="demo"\n', encoding='utf-8')
    monkeypatch.setattr('naver_blog_archive.archive.backup', lambda *a, **k: {'last_run': {'status': 'partial'}})
    assert main(['backup', '--config', str(path)]) == 1


def test_cli_interruption_exit_code(tmp_path, monkeypatch):
    path = tmp_path / 'config.toml'
    path.write_text('blog_id="demo"\n', encoding='utf-8')
    def interrupt(*args, **kwargs): raise KeyboardInterrupt()
    monkeypatch.setattr('naver_blog_archive.archive.backup', interrupt)
    assert main(['backup', '--config', str(path)]) == 130


def test_doctor_reports_browser_launch_failure(monkeypatch):
    from naver_blog_archive.cli import doctor
    class MissingBrowser:
        def __enter__(self): raise RuntimeError('Browser is not installed')
        def __exit__(self, *args): pass
    monkeypatch.setattr('playwright.sync_api.sync_playwright', MissingBrowser)
    assert doctor() == 1


@pytest.mark.parametrize('problems,expected', [([], 0), (['protected file'], 1)])
def test_cli_reformat_reports_offline_output_status(tmp_path, monkeypatch, problems, expected):
    path = tmp_path / 'config.toml'
    path.write_text('blog_id="demo"\n', encoding='utf-8')
    monkeypatch.setattr('naver_blog_archive.archive.reformat_archive',
                        lambda *args, **kwargs: {'output_problems': problems, 'failures': []})
    assert main(['reformat', '--config', str(path)]) == expected
