from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Timer
from types import SimpleNamespace

import pytest

from naver_blog_archive import transcription as speech
from naver_blog_archive import _transcription_worker as worker
from naver_blog_archive.progress import OperationCancelled, TaskControl


@pytest.fixture
def local_model(tmp_path, monkeypatch):
    monkeypatch.setenv('NBA_MODEL_DIR', str(tmp_path / 'models'))
    monkeypatch.setattr(speech, '_package_available', lambda: True)
    directory = speech.model_directory()
    directory.mkdir(parents=True)
    for name in ('model.bin', 'config.json', 'tokenizer.json', 'vocabulary.json'):
        (directory / name).write_bytes(b'model-fixture')
    return directory


def test_readiness_uses_only_local_metadata(local_model, monkeypatch):
    monkeypatch.setitem(sys.modules, 'faster_whisper', None)
    speech.ensure_transcription_ready()
    (local_model / 'tokenizer.json').unlink()
    with pytest.raises(speech.TranscriptionNotReady, match='모델'):
        speech.ensure_transcription_ready()


def test_missing_optional_dependency_gives_setup_hint(monkeypatch):
    monkeypatch.setattr(speech, '_package_available', lambda: False)
    with pytest.raises(speech.TranscriptionNotReady, match='준비를 먼저 실행'):
        speech.ensure_transcription_ready()


@pytest.mark.parametrize(('version', 'available'), [('1.1.1', False), ('1.2.0', True),
                                                   ('1.3.10', True), ('2.0.0', False)])
def test_supported_package_versions(monkeypatch, version, available):
    monkeypatch.setattr(speech.metadata, 'version', lambda name: version)
    assert speech._package_available() is available


def test_empty_local_input_does_not_launch_a_worker(local_model, tmp_path, monkeypatch):
    audio = tmp_path / 'empty.mp4'
    audio.touch()
    monkeypatch.setattr(speech, '_worker', lambda *args, **kwargs: pytest.fail('worker launched'))
    with pytest.raises(speech.TranscriptionError, match='음성 파일'):
        speech.transcribe_audio(audio)


def test_transcription_preserves_timestamps_and_plain_text(local_model, tmp_path, monkeypatch):
    audio = tmp_path / '주식 강의 $(not a command).mp4'
    audio.write_bytes(b'fixture')
    def run(operation, **kwargs):
        assert operation == 'transcribe'
        assert kwargs['audio_path'] == audio.resolve()
        return {'transcript': {'duration': 10, 'language': 'ko', 'segments': [
            {'start': 1.2, 'end': 4.5, 'text': '  가격은 변합니다.  '},
            {'start': 5, 'end': 6, 'text': '<script>untrusted text</script>'},
        ]}}
    monkeypatch.setattr(speech, '_worker', run)
    events = []
    result = speech.transcribe_audio(audio, TaskControl(events.append))
    assert result == {'method': 'local_whisper', 'model': 'small', 'language': 'ko',
                      'duration': 10, 'segments': [
                          {'start': 1.2, 'end': 4.5, 'text': '가격은 변합니다.'},
                          {'start': 5, 'end': 6, 'text': '<script>untrusted text</script>'}]}
    assert events[-1]['phase'] == 'transcribing'
    assert events[-1]['seconds'] == events[-1]['total_seconds'] == 10


def test_silent_audio_is_an_empty_transcript_not_an_error(local_model, tmp_path, monkeypatch):
    audio = tmp_path / 'silence.wav'
    audio.write_bytes(b'nonempty audio file')
    monkeypatch.setattr(speech, '_worker', lambda *args, **kwargs: {
        'transcript': {'duration': 20, 'language': 'ko', 'segments': []}})
    result = speech.transcribe_audio(audio)
    assert result['segments'] == []
    assert result['duration'] == 20


@pytest.mark.parametrize('segments', [
    [{'start': float('nan'), 'end': 1, 'text': 'text'}],
    [{'start': 2, 'end': 1, 'text': 'text'}],
    [{'start': -1, 'end': 1, 'text': 'text'}],
    [{'start': 1, 'end': 2, 'text': 123}],
    [{'start': 2, 'end': 3, 'text': 'first'}, {'start': 1, 'end': 4, 'text': 'second'}],
])
def test_invalid_worker_timestamps_are_not_saved(segments):
    with pytest.raises(speech.TranscriptionError, match='형식'):
        speech._validate_transcript({'duration': 5, 'language': 'ko', 'segments': segments})


def test_setup_installs_optional_package_then_prepares_model(local_model, monkeypatch):
    state, calls = {'installed': False}, []
    monkeypatch.setattr(speech, '_package_available', lambda: state['installed'])
    def install(command, **kwargs):
        calls.append(command)
        assert kwargs['offline'] is False
        state['installed'] = True
        return 0
    monkeypatch.setattr(speech, '_run_process', install)
    monkeypatch.setattr(speech, '_worker', lambda operation, **kwargs: calls.append(operation))
    speech.prepare_transcription()
    assert calls[0][:4] == [sys.executable, '-m', 'pip', 'install']
    assert '--only-binary=:all:' in calls[0]
    assert calls[0][-1] == 'faster-whisper>=1.2,<2'
    assert calls[1] == 'prepare'


def test_install_failure_does_not_attempt_model_download(local_model, monkeypatch):
    monkeypatch.setattr(speech, '_package_available', lambda: False)
    monkeypatch.setattr(speech, '_run_process', lambda *args, **kwargs: 1)
    monkeypatch.setattr(speech, '_worker', lambda *args, **kwargs: pytest.fail('download attempted'))
    with pytest.raises(speech.TranscriptionError, match='패키지 설치'):
        speech.prepare_transcription()


def test_pre_cancelled_job_never_starts_a_process(monkeypatch):
    control = TaskControl()
    control.cancel()
    monkeypatch.setattr(subprocess, 'Popen', lambda *args, **kwargs: pytest.fail('process started'))
    with pytest.raises(OperationCancelled):
        speech._run_process([sys.executable], control=control, message='test', timeout=1, offline=True)


def test_cancelling_during_native_work_reaps_real_process(monkeypatch):
    original = subprocess.Popen
    processes = []
    control = TaskControl()
    timer = None
    def launch(command, **kwargs):
        nonlocal timer
        assert kwargs['shell'] is False
        assert kwargs['env']['HF_HUB_OFFLINE'] == '1'
        assert kwargs['env']['HF_HUB_DISABLE_TELEMETRY'] == '1'
        if os.name == 'nt':
            assert kwargs['creationflags'] == subprocess.CREATE_NO_WINDOW
        process = original(command, **kwargs)
        processes.append(process)
        timer = Timer(0.2, control.cancel)
        timer.start()
        return process
    monkeypatch.setattr(subprocess, 'Popen', launch)
    try:
        with pytest.raises(OperationCancelled):
            speech._run_process([sys.executable, '-c', 'import time; time.sleep(60)'],
                                control=control, message='test', timeout=10, offline=True)
    finally:
        if timer is not None:
            timer.cancel()
            timer.join()
    assert processes[0].poll() is not None


def test_timeout_reaps_real_process(monkeypatch):
    original = subprocess.Popen
    processes = []
    def launch(*args, **kwargs):
        processes.append(original(*args, **kwargs))
        return processes[-1]
    monkeypatch.setattr(subprocess, 'Popen', launch)
    with pytest.raises(speech.TranscriptionError, match='최대 대기 시간'):
        speech._run_process([sys.executable, '-c', 'import time; time.sleep(60)'],
                            control=TaskControl(), message='test', timeout=0.2, offline=True)
    assert processes[0].poll() is not None


def test_unresponsive_termination_escalates_to_kill():
    calls = []
    def wait(timeout=None):
        calls.append(('wait', timeout))
        if timeout == 2:
            raise subprocess.TimeoutExpired('worker', timeout)
    process = SimpleNamespace(poll=lambda: None, terminate=lambda: calls.append('terminate'),
                              kill=lambda: calls.append('kill'), wait=wait)
    speech._stop_process(process)
    assert calls == ['terminate', ('wait', 2), 'kill', ('wait', 5)]


def test_heartbeat_continues_while_model_is_loading(monkeypatch):
    now, events = [0.0], []
    class Control(TaskControl):
        def wait(self, seconds):
            assert seconds <= 0.2
            now[0] += seconds
    process = SimpleNamespace(poll=lambda: 0 if now[0] > 10.5 else None, wait=lambda: 0)
    monkeypatch.setattr(subprocess, 'Popen', lambda *args, **kwargs: process)
    monkeypatch.setattr(speech.time, 'monotonic', lambda: now[0])
    speech._run_process(['python'], control=Control(events.append), message='loading', timeout=20, offline=True)
    assert len(events) == 3
    assert all(event['phase'] == 'transcribing' for event in events)
    assert events[1]['elapsed_seconds'] >= 5
    assert events[2]['elapsed_seconds'] >= 10


def test_worker_protocol_failure_does_not_leak_raw_exception(local_model, monkeypatch):
    paths = []
    def run(command, **kwargs):
        result = Path(command[command.index('--result') + 1])
        paths.append(result.parent)
        result.write_text(json.dumps({'ok': False, 'error': 'audio',
                                      'detail': 'private media information'}), encoding='utf-8')
        assert kwargs['offline'] is True
        return 1
    monkeypatch.setattr(speech, '_run_process', run)
    with pytest.raises(speech.TranscriptionError) as error:
        speech._worker('transcribe', control=TaskControl(), audio_path=Path('audio.wav'))
    assert 'private' not in str(error.value)
    assert not paths[0].exists()


def test_worker_temp_files_are_removed_on_cancellation(local_model, monkeypatch):
    paths = []
    def run(command, **kwargs):
        paths.append(Path(command[command.index('--result') + 1]).parent)
        raise OperationCancelled()
    monkeypatch.setattr(speech, '_run_process', run)
    with pytest.raises(OperationCancelled):
        speech._worker('transcribe', control=TaskControl(), audio_path=Path('audio.wav'))
    assert not paths[0].exists()


def test_worker_uses_local_cpu_model_and_lazy_segments(local_model, tmp_path, monkeypatch):
    calls = []
    class Model:
        def __init__(self, directory, **kwargs):
            assert directory == str(local_model)
            assert kwargs['device'] == 'cpu' and kwargs['compute_type'] == 'int8'
            assert kwargs['local_files_only'] is True
        def transcribe(self, path, **kwargs):
            assert kwargs == {'task': 'transcribe', 'beam_size': 5, 'vad_filter': True,
                              'condition_on_previous_text': False}
            def segments():
                calls.append('inferred')
                yield SimpleNamespace(start=1, end=3, text=' hello ')
            return segments(), SimpleNamespace(duration=4, language='en')
    monkeypatch.setitem(sys.modules, 'faster_whisper', SimpleNamespace(WhisperModel=Model))
    progress = tmp_path / 'progress.json'
    result = worker._transcribe(local_model, tmp_path / 'audio.wav', progress)
    assert calls == ['inferred']
    assert result['segments'] == [{'start': 1, 'end': 3, 'text': 'hello'}]
    assert json.loads(progress.read_text()) == {'stage': 'transcribe', 'seconds': 3, 'total_seconds': 4}


def test_prepared_model_is_reused_without_download(local_model, monkeypatch):
    monkeypatch.setitem(sys.modules, 'faster_whisper', SimpleNamespace())
    monkeypatch.setitem(sys.modules, 'faster_whisper.utils', SimpleNamespace(
        download_model=lambda *args, **kwargs: pytest.fail('model downloaded twice')))
    checked = []
    monkeypatch.setattr(worker, '_load_model', lambda path: checked.append(path))
    worker._prepare(local_model)
    assert checked == [local_model]


@pytest.mark.parametrize('operation', ['prepare', 'transcribe'])
def test_model_load_failure_has_distinct_safe_error(local_model, tmp_path, monkeypatch, operation):
    for name in ('HF_HUB_OFFLINE', 'HF_HUB_DISABLE_TELEMETRY', 'HF_HUB_DISABLE_IMPLICIT_TOKEN', 'DO_NOT_TRACK'):
        monkeypatch.setenv(name, '')
    monkeypatch.setitem(sys.modules, 'faster_whisper', SimpleNamespace())
    monkeypatch.setitem(sys.modules, 'faster_whisper.utils', SimpleNamespace(download_model=lambda *args, **kwargs: None))
    def broken_model(directory):
        raise worker._ModelLoadError('private path must not be displayed')
    monkeypatch.setattr(worker, '_load_model', broken_model)
    audio, result, progress = tmp_path / 'audio.wav', tmp_path / 'result.json', tmp_path / 'progress.json'
    audio.write_bytes(b'fixture')
    code = worker.main([operation, '--model-dir', str(local_model), '--result', str(result),
                        '--progress', str(progress), '--audio', str(audio)])
    assert code == 1
    assert json.loads(result.read_text()) == {'ok': False, 'error': 'model'}
    assert os.environ['HF_HUB_OFFLINE'] == ('0' if operation == 'prepare' else '1')
