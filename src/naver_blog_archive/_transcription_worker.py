"""Private subprocess entry point; all audio/model processing stays on this PC."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import time


class _ModelLoadError(RuntimeError):
    pass


def _write_json(path: Path, value) -> None:
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    # On Windows, an overlapping short read can temporarily prevent replacement.
    for attempt in range(10):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.02)


def _load_model(directory: Path):
    from faster_whisper import WhisperModel
    try:
        return WhisperModel(str(directory), device='cpu', compute_type='int8',
                            cpu_threads=max(1, min(8, (os.cpu_count() or 2) // 2)),
                            num_workers=1, local_files_only=True)
    except Exception as exc:
        raise _ModelLoadError() from exc


def _prepare(directory: Path) -> None:
    from .transcription import MODEL_NAME, _model_available
    from faster_whisper.utils import download_model
    directory.mkdir(parents=True, exist_ok=True)
    if not _model_available(directory):
        download_model(MODEL_NAME, output_dir=str(directory), use_auth_token=False)
    # Successful download alone does not prove that native libraries/model load.
    _load_model(directory)


def _transcribe(directory: Path, audio_path: Path, progress_path: Path) -> dict:
    model = _load_model(directory)
    _write_json(progress_path, {'stage': 'decode'})
    segments, information = model.transcribe(str(audio_path), task='transcribe',
                                            beam_size=5, vad_filter=True,
                                            condition_on_previous_text=False)
    duration = float(information.duration)
    _write_json(progress_path, {'stage': 'transcribe', 'total_seconds': duration, 'seconds': 0})
    result = []
    for segment in segments:
        text = segment.text.strip()
        if text:
            result.append({'start': float(segment.start), 'end': float(segment.end), 'text': text})
        _write_json(progress_path, {'stage': 'transcribe', 'total_seconds': duration,
                                   'seconds': min(duration, max(0, float(segment.end)))})
    return {'language': information.language, 'segments': result, 'duration': duration}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=('prepare', 'transcribe'))
    parser.add_argument('--model-dir', type=Path, required=True)
    parser.add_argument('--result', type=Path, required=True)
    parser.add_argument('--progress', type=Path, required=True)
    parser.add_argument('--audio', type=Path)
    arguments = parser.parse_args(argv)
    # Applied before importing any package that could access a model hub.
    os.environ.update(HF_HUB_OFFLINE='0' if arguments.operation == 'prepare' else '1',
                      HF_HUB_DISABLE_TELEMETRY='1', HF_HUB_DISABLE_IMPLICIT_TOKEN='1',
                      DO_NOT_TRACK='1')
    try:
        import faster_whisper  # noqa: F401 -- lazy import confined to this process
    except Exception:
        _write_json(arguments.result, {'ok': False, 'error': 'dependency'})
        return 1
    try:
        if arguments.operation == 'prepare':
            _write_json(arguments.progress, {'stage': 'prepare'})
            _prepare(arguments.model_dir)
            _write_json(arguments.result, {'ok': True})
        else:
            if arguments.audio is None or not arguments.audio.is_file() or arguments.audio.stat().st_size == 0:
                raise ValueError('missing audio')
            from .transcription import _model_available, _validate_transcript
            if not _model_available(arguments.model_dir):
                _write_json(arguments.result, {'ok': False, 'error': 'model'})
                return 1
            _write_json(arguments.progress, {'stage': 'load'})
            result = _validate_transcript(_transcribe(arguments.model_dir, arguments.audio, arguments.progress))
            _write_json(arguments.result, {'ok': True, 'transcript': result})
        return 0
    except _ModelLoadError:
        _write_json(arguments.result, {'ok': False, 'error': 'model'})
        return 1
    except Exception:
        # Third-party exceptions can contain paths, URLs or media metadata; emit
        # a fixed error code rather than persisting them into the archive log.
        _write_json(arguments.result, {'ok': False,
                                       'error': 'download' if arguments.operation == 'prepare' else 'audio'})
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
