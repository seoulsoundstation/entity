"""Optional, cancellable local speech recognition with an offline runtime."""
from __future__ import annotations

from importlib import invalidate_caches, metadata
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory
import time

from .progress import TaskControl


MODEL_NAME = 'small'
_POLL_SECONDS = 0.2
_HEARTBEAT_SECONDS = 5.0
_INSTALL_TIMEOUT = 30 * 60
_PREPARE_TIMEOUT = 60 * 60
_TRANSCRIBE_TIMEOUT = 4 * 60 * 60
_MAX_RESULT_BYTES = 32 * 1024 * 1024
_PREPARE_HINT = '음성 인식 준비를 먼저 실행하세요.'


class TranscriptionError(RuntimeError):
    """A speech-recognition failure safe to display in the backup log."""


class TranscriptionNotReady(TranscriptionError):
    """The optional package or local model is not prepared yet."""


def model_directory() -> Path:
    override = os.environ.get('NBA_MODEL_DIR')
    base = Path(override).expanduser() if override else (
        Path(os.environ['LOCALAPPDATA']) / 'NaverBlogArchive' / 'models'
        if os.environ.get('LOCALAPPDATA') else Path.home() / '.cache' / 'NaverBlogArchive' / 'models')
    return base.resolve() / MODEL_NAME


def _package_available() -> bool:
    try:
        version = metadata.version('faster-whisper')
    except metadata.PackageNotFoundError:
        return False
    match = re.match(r'^(\d+)\.(\d+)(?:\.|$)', version)
    return bool(match and (1, 2) <= tuple(map(int, match.groups())) < (2, 0))


def _model_available(directory: Path) -> bool:
    required = ('model.bin', 'config.json', 'tokenizer.json')
    try:
        return (all((directory / name).is_file() and (directory / name).stat().st_size > 0
                    for name in required)
                and any((directory / name).is_file() and (directory / name).stat().st_size > 0
                        for name in ('vocabulary.json', 'vocabulary.txt')))
    except OSError:
        return False


def ensure_transcription_ready() -> None:
    """Check local prerequisites without importing the model or using the network."""
    if not _package_available():
        raise TranscriptionNotReady('로컬 음성 인식 패키지가 준비되지 않았습니다. ' + _PREPARE_HINT)
    if not _model_available(model_directory()):
        raise TranscriptionNotReady('로컬 음성 인식 모델이 준비되지 않았습니다. ' + _PREPARE_HINT)


def _environment(*, offline: bool) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(HF_HUB_OFFLINE='1' if offline else '0',
                       HF_HUB_DISABLE_TELEMETRY='1', HF_HUB_DISABLE_IMPLICIT_TOKEN='1',
                       DO_NOT_TRACK='1', TOKENIZERS_PARALLELISM='false', PYTHONUTF8='1')
    return environment


def _stop_process(process) -> None:
    if process.poll() is not None:
        process.wait()
        return
    try:
        process.terminate()
    except ProcessLookupError:
        process.wait()
        return
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _read_json(path: Path, *, max_bytes: int = _MAX_RESULT_BYTES):
    try:
        with path.open('rb') as stream:
            content = stream.read(max_bytes + 1)
        if len(content) > max_bytes:
            return None
        return json.loads(content)
    except (OSError, ValueError, UnicodeError):
        return None


def _progress_fields(value) -> dict:
    if not isinstance(value, dict):
        return {}
    result = {}
    for name in ('seconds', 'total_seconds'):
        number = value.get(name)
        if isinstance(number, (int, float)) and not isinstance(number, bool) and math.isfinite(number) and number >= 0:
            result[name] = float(number)
    return result


def _run_process(command: list[str], *, control: TaskControl, message: str,
                 timeout: float, offline: bool, progress_path: Path | None = None) -> int:
    """Keep native model loading/inference outside the GUI worker thread."""
    control.check()
    phase = 'transcribing' if offline else 'transcription-setup'
    control.emit(phase, message)
    kwargs = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                  shell=False, env=_environment(offline=offline))
    if os.name == 'nt':
        kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
    try:
        process = subprocess.Popen(command, **kwargs)
    except OSError as exc:
        raise TranscriptionError('로컬 음성 인식 작업을 시작하지 못했습니다. 실행 환경을 확인하세요.') from exc
    started = last_event = time.monotonic()
    previous = None
    try:
        while True:
            control.check()
            now = time.monotonic()
            if now - started >= timeout:
                raise TranscriptionError('로컬 음성 인식 작업의 최대 대기 시간을 초과했습니다. 중단 후 다시 시도하세요.')
            progress = _read_json(progress_path, max_bytes=4096) if progress_path else None
            fields = _progress_fields(progress)
            if progress != previous or now - last_event >= _HEARTBEAT_SECONDS:
                detail = message
                if fields.get('total_seconds', 0) > 0 and 'seconds' in fields:
                    percentage = min(100, int(100 * fields['seconds'] / fields['total_seconds']))
                    detail = f'로컬 음성 인식 중: {percentage}%'
                control.emit(phase, detail, elapsed_seconds=int(now - started), **fields)
                previous, last_event = progress, now
            code = process.poll()
            if code is not None:
                control.check()
                process.wait()
                return code
            control.wait(_POLL_SECONDS)
    finally:
        _stop_process(process)


def _worker(operation: str, *, control: TaskControl, audio_path: Path | None = None) -> dict:
    with TemporaryDirectory(prefix='nba-transcription-') as temporary:
        directory = Path(temporary)
        result_path, progress_path = directory / 'result.json', directory / 'progress.json'
        command = [sys.executable, '-m', 'naver_blog_archive._transcription_worker', operation,
                   '--model-dir', str(model_directory()), '--result', str(result_path),
                   '--progress', str(progress_path)]
        if audio_path is not None:
            command.extend(['--audio', str(audio_path)])
        preparing = operation == 'prepare'
        code = _run_process(command, control=control,
                            message=('음성 인식 모델을 다운로드하고 확인하는 중입니다.' if preparing
                                     else '로컬 음성 인식 모델을 불러오고 있습니다.'),
                            timeout=_PREPARE_TIMEOUT if preparing else _TRANSCRIBE_TIMEOUT,
                            offline=not preparing, progress_path=progress_path)
        result = _read_json(result_path)
        if code != 0 or not isinstance(result, dict) or not result.get('ok'):
            error = result.get('error') if isinstance(result, dict) else None
            messages = {
                'dependency': '로컬 음성 인식 패키지를 불러오지 못했습니다. ' + _PREPARE_HINT,
                'model': '로컬 음성 인식 모델을 불러오지 못했습니다. ' + _PREPARE_HINT,
                'download': '음성 인식 모델 다운로드에 실패했습니다. 인터넷 연결과 저장 공간을 확인한 뒤 준비를 다시 실행하세요.',
                'audio': '음성을 읽거나 텍스트로 변환하지 못했습니다. 음성 파일과 실행 환경을 확인하세요.',
            }
            raise TranscriptionError(messages.get(error, '로컬 음성 인식 작업이 정상적으로 완료되지 않았습니다.'))
        return result


def prepare_transcription(control: TaskControl | None = None) -> None:
    """Explicit one-time preparation; only packages and model weights are downloaded."""
    control = control or TaskControl()
    control.check()
    if not _package_available():
        code = _run_process(
            [sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check', '--no-input',
             '--only-binary=:all:', '--progress-bar', 'off', 'faster-whisper>=1.2,<2'],
            control=control, message='로컬 음성 인식 패키지를 설치하는 중입니다.',
            timeout=_INSTALL_TIMEOUT, offline=False)
        invalidate_caches()
        if code != 0 or not _package_available():
            raise TranscriptionError('로컬 음성 인식 패키지 설치에 실패했습니다. 인터넷 연결과 Python 실행 환경을 확인하세요.')
    _worker('prepare', control=control)
    ensure_transcription_ready()
    control.emit('transcription-setup', '로컬 음성 인식 준비를 마쳤습니다. 음성은 이 컴퓨터에서만 처리합니다.')


def transcribe_audio(path: Path, control: TaskControl | None = None) -> dict:
    """Transcribe a local audio file; never install packages or download a model here."""
    control = control or TaskControl()
    control.check()
    ensure_transcription_ready()
    try:
        path = Path(path).resolve(strict=True)
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError('empty or non-file input')
    except (OSError, ValueError) as exc:
        raise TranscriptionError('읽을 수 있는 음성 파일이 없습니다.') from exc
    data = _worker('transcribe', control=control, audio_path=path).get('transcript')
    result = _validate_transcript(data)
    control.emit('transcribing', '로컬 음성 인식을 완료했습니다.',
                 seconds=result['duration'], total_seconds=result['duration'])
    return result


def _validate_transcript(data) -> dict:
    try:
        if not isinstance(data, dict) or not isinstance(data.get('segments'), list):
            raise ValueError()
        duration = float(data['duration'])
        if not math.isfinite(duration) or duration < 0:
            raise ValueError()
        segments = []
        previous_start = 0.0
        for segment in data['segments']:
            start, end = float(segment['start']), float(segment['end'])
            text = segment['text']
            if (not math.isfinite(start) or not math.isfinite(end) or start < previous_start
                    or end < start or not isinstance(text, str)):
                raise ValueError()
            previous_start = start
            if text.strip():
                segments.append({'start': start, 'end': end, 'text': text.strip()})
        language = data.get('language')
        if not isinstance(language, str) or not re.fullmatch(r'[a-z]{2,3}', language):
            language = 'und'
        return {'method': 'local_whisper', 'model': MODEL_NAME, 'language': language,
                'segments': segments, 'duration': duration}
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise TranscriptionError('음성 인식 결과의 형식이 올바르지 않습니다.') from exc
