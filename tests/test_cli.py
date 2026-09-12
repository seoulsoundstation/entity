from naver_blog_archive.cli import build_parser
from pathlib import Path
import sys
from types import SimpleNamespace

from naver_blog_archive.cli import main
from naver_blog_archive.progress import OperationCancelled


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


def test_preparation_command_needs_no_config_or_gui(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(sys.modules, 'naver_blog_archive.gui', None)

    def prepare(*, control):
        control.emit('transcription-setup', '모델 준비 중')

    monkeypatch.setitem(sys.modules, 'naver_blog_archive.transcription',
                        SimpleNamespace(prepare_transcription=prepare))
    assert main(['prepare-transcription']) == 0
    output = capsys.readouterr().out
    assert '모델 준비 중' in output and '준비가 끝났습니다' in output
    assert not (tmp_path / 'config.toml').exists()


def test_preparation_cancel_exit_code_explains_retry_command(monkeypatch, capsys):
    def cancelled(*, control):
        raise OperationCancelled()

    monkeypatch.setitem(sys.modules, 'naver_blog_archive.transcription',
                        SimpleNamespace(prepare_transcription=cancelled))
    assert main(['prepare-transcription']) == 130
    assert 'prepare-transcription 명령' in capsys.readouterr().err


def test_preparation_failure_has_error_exit_code(monkeypatch, capsys):
    def failing(*, control):
        raise RuntimeError('모델 다운로드 실패')

    monkeypatch.setitem(sys.modules, 'naver_blog_archive.transcription',
                        SimpleNamespace(prepare_transcription=failing))
    assert main(['prepare-transcription']) == 2
    assert '모델 다운로드 실패' in capsys.readouterr().err
