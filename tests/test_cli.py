from naver_blog_archive.cli import build_parser
from pathlib import Path
import sys
from types import SimpleNamespace

from naver_blog_archive.cli import main


def test_parser_accepts_doctor_command():
    args = build_parser().parse_args(["doctor"])

    assert args.command == "doctor"


def test_gui_parser_uses_default_config_and_accepts_custom_path():
    assert build_parser().parse_args(['gui']).config == Path('config.toml')
    assert build_parser().parse_args(['gui', '--config', 'my settings.toml']).config == Path('my settings.toml')


def test_gui_launch_does_not_require_an_existing_config(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setitem(sys.modules, 'naver_blog_archive.gui',
                        SimpleNamespace(launch=lambda **kwargs: seen.append(kwargs) or 0))
    path = tmp_path / 'missing.toml'
    assert main(['gui', '--config', str(path)]) == 0
    assert seen == [{'config_path': path}]
    assert not path.exists()


def test_existing_cli_does_not_import_gui(monkeypatch):
    monkeypatch.setitem(sys.modules, 'naver_blog_archive.gui', None)
    monkeypatch.setattr('naver_blog_archive.cli.doctor', lambda: 0)
    assert main(['doctor']) == 0
