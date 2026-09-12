"""Caption-first, local-only transcription of authorized Premium videos."""
from __future__ import annotations

from datetime import datetime, timezone
from html import unescape
import math
from pathlib import Path
import re
import tempfile
import time
import xml.etree.ElementTree as ET

from .progress import TaskControl


TRANSCRIPT_VERSION = 1
MAX_CAPTION_BYTES = 8 * 1024 * 1024
MAX_MEDIA_BYTES = 2 * 1024 * 1024 * 1024
MAX_DOWNLOAD_SECONDS = 3600


def transcript_complete(document: dict) -> bool:
    saved = document.get('video_transcript') or {}
    return (isinstance(saved, dict) and saved.get('version') == TRANSCRIPT_VERSION
            and saved.get('status') == 'success' and saved.get('video_id') == document.get('video_id')
            and isinstance(document.get('video_text'), str) and bool(document['video_text'].strip()))


def video_errors(config, document: dict) -> list[str]:
    if document.get('content_type') == 'video' and document.get('video_refresh_pending'):
        return ['영상 원문 갱신 미완료: 이전 텍스트를 보존했습니다. 다음 백업에서 원문 확인을 재시도합니다.']
    if (document.get('content_type') == 'video' and config.transcribe_videos
            and not transcript_complete(document)):
        return ['영상 텍스트화 미완료: ' + (document.get('video_error') or '백업을 시작하면 자막 또는 음성을 처리합니다.')]
    return []


def video_markdown(config, document: dict) -> str:
    if transcript_complete(document):
        return document['video_text']
    if not config.transcribe_videos:
        return '## 영상 텍스트\n\n영상 텍스트화가 꺼져 있습니다. 영상은 위 원문 링크에서 확인하세요.'
    return '## 영상 텍스트\n\n' + video_errors(config, document)[0]


def _seconds(value: str) -> float:
    value = value.strip().replace(',', '.')
    if re.fullmatch(r'\d+(?:\.\d+)?s', value):
        return float(value[:-1])
    parts = value.split(':')
    if len(parts) not in (2, 3) or not all(re.fullmatch(r'\d+(?:\.\d+)?', p) for p in parts):
        raise ValueError('자막 시간 형식을 확인할 수 없습니다.')
    result = 0.0
    for part in parts:
        result = result * 60 + float(part)
    return result


def _caption_text(value: str) -> str:
    # Text from captions never becomes active HTML or Markdown instructions.
    return ' '.join(unescape(re.sub(r'<[^>]*>', '', value)).split())


def parse_captions(content: bytes) -> list[dict]:
    """Read timed WebVTT/SRT and TTML captions, rejecting unrecognized data."""
    if len(content) > MAX_CAPTION_BYTES:
        raise ValueError('자막 크기 제한을 초과했습니다.')
    text = content.decode('utf-8-sig').replace('\r\n', '\n').replace('\r', '\n')
    result = []
    if text.lstrip().startswith('<'):
        if re.search(r'<!\s*(?:DOCTYPE|ENTITY)', text, re.I):
            raise ValueError('지원하지 않는 자막 XML입니다.')
        root = ET.fromstring(text)
        for element in root.iter():
            if element.tag.split('}')[-1] != 'p' or not element.get('begin'):
                continue
            start = _seconds(element.get('begin'))
            end = (_seconds(element.get('end')) if element.get('end') else
                   start + _seconds(element.get('dur')) if element.get('dur') else start)
            result.append({'start': start, 'end': end, 'text': _caption_text(' '.join(element.itertext()))})
    else:
        timing = re.compile(r'^\s*((?:\d+:)?\d+:\d+[.,]\d+)\s+-->\s+((?:\d+:)?\d+:\d+[.,]\d+)(?:\s+.*)?$')
        lines = text.split('\n')
        index = 0
        while index < len(lines):
            match = timing.fullmatch(lines[index])
            index += 1
            if not match:
                continue
            body = []
            while index < len(lines) and lines[index].strip():
                body.append(lines[index])
                index += 1
            result.append({'start': _seconds(match[1]), 'end': _seconds(match[2]),
                           'text': _caption_text(' '.join(body))})
    result = _validated_segments(result)
    if not result:
        raise ValueError('읽을 수 있는 자막 문장이 없습니다.')
    return result


def _validated_segments(segments) -> list[dict]:
    if not isinstance(segments, list):
        raise ValueError('영상 텍스트 결과 형식이 올바르지 않습니다.')
    result = []
    for segment in segments:
        if not isinstance(segment, dict) or not isinstance(segment.get('text'), str):
            raise ValueError('영상 텍스트 문장 형식이 올바르지 않습니다.')
        start, end = segment.get('start'), segment.get('end')
        if (type(start) not in (int, float) or type(end) not in (int, float)
                or not math.isfinite(start) or not math.isfinite(end) or start < 0 or end < start):
            raise ValueError('영상 텍스트 시간이 올바르지 않습니다.')
        sentence = ' '.join(segment['text'].split())
        if sentence:
            item = {'start': float(start), 'end': float(end), 'text': sentence}
            if not result or result[-1] != item:
                result.append(item)
    return result


def _timestamp(seconds: float) -> str:
    seconds = int(seconds)
    return f'{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}'


def _escape_text(text: str) -> str:
    return re.sub(r'([\\`*_{}\[\]<>#|+~=$!-])', r'\\\1', text)


def _read_resource(client, url: str, control: TaskControl, limit: int, output: Path | None = None):
    from .premium_media import media_url_allowed
    if not media_url_allowed(url):
        raise ValueError('안전한 영상 자원 주소를 확인할 수 없습니다.')
    size, chunks = 0, []
    started = last_notice = time.monotonic()
    stream = output.open('wb') if output else None
    try:
        with client.get(url, stream=True, allow_redirects=False) as response:
            if response.status_code != 200:
                raise ValueError(f'영상 자원 응답을 확인할 수 없습니다. 상태 {response.status_code}')
            raw_total = response.headers.get('Content-Length', '')
            total = int(raw_total) if str(raw_total).isdigit() else None
            if total and total > limit:
                raise ValueError('영상 자원 크기 제한을 초과했습니다.')
            for chunk in response.iter_content(128 * 1024):
                control.check()
                if time.monotonic() - started > MAX_DOWNLOAD_SECONDS:
                    raise ValueError('영상 자원 다운로드 시간 제한을 초과했습니다.')
                size += len(chunk)
                if size > limit:
                    raise ValueError('영상 자원 크기 제한을 초과했습니다.')
                if stream:
                    stream.write(chunk)
                else:
                    chunks.append(chunk)
                if output and time.monotonic() - last_notice >= 1:
                    control.emit('video-download', f'음성 인식용 파일 받는 중: {size / 1024 / 1024:.1f} MB',
                                 bytes=size, total_bytes=total)
                    last_notice = time.monotonic()
        if not size:
            raise ValueError('영상 자원이 비어 있습니다.')
        return b''.join(chunks) if output is None else size
    finally:
        if stream:
            stream.close()


def transcribe_video(config, client, document: dict, control: TaskControl) -> None:
    """Mutate the document only after a complete caption/transcription result."""
    if (document.get('content_type') != 'video' or not config.transcribe_videos
            or transcript_complete(document)):
        return
    from .premium_media import resolve_media
    from .transcription import ensure_transcription_ready, transcribe_audio

    control.check()
    control.emit('transcribing', '영상 자막 확인 중')
    media = resolve_media(client, document['blog_id'], document['post_id'], control=control)
    if media.get('video_id') != document.get('video_id'):
        raise ValueError('영상이 변경되었습니다. 기존 글도 최신 내용으로 갱신을 선택하세요.')
    result = None
    captions = sorted(media.get('captions') or [], key=lambda item: not str(item.get('language', '')).lower().startswith('ko'))
    for caption in captions:
        try:
            raw = _read_resource(client, caption['url'], control, MAX_CAPTION_BYTES)
            result = {'method': 'captions', 'language': caption.get('language') or 'unknown',
                      'segments': parse_captions(raw), 'duration': media.get('duration'),
                      'automatic': bool(caption.get('automatic'))}
            break
        except Exception:
            # A broken caption must not prevent the selected local fallback.
            control.emit('transcribing', '제공된 자막을 읽지 못해 다음 자막 또는 로컬 음성 인식을 확인합니다.')
    if result is None:
        ensure_transcription_ready()
        url = media.get('audio_url') or media.get('media_url')
        if not url:
            raise ValueError('음성 인식에 사용할 일반 재생 파일이 없습니다.')
        control.emit('video-download', '자막이 없어 로컬 음성 인식용 파일을 받습니다.')
        # Temporary media is never part of the archive or version history.
        with tempfile.TemporaryDirectory(prefix='naver-transcription-') as directory:
            path = Path(directory) / 'audio.mp4'
            _read_resource(client, url, control, MAX_MEDIA_BYTES, path)
            control.check()
            result = transcribe_audio(path, control=control)
    segments = _validated_segments(result.get('segments'))
    method = result.get('method')
    if method not in ('captions', 'local_whisper'):
        raise ValueError('영상 텍스트 생성 방식을 확인할 수 없습니다.')
    label = ('제공된 자동 자막' if result.get('automatic') else '제공된 자막') if method == 'captions' else '로컬 음성 인식 · small 모델'
    lines = ['## 영상 텍스트', '', f'> 생성 방식: {label}. 시간은 영상 시작 기준입니다.']
    if method == 'local_whisper' or result.get('automatic'):
        lines += ['> 자동 인식 결과에는 고유명사·숫자 등의 오류가 있을 수 있습니다.']
    lines += ['']
    if not segments:
        lines += ['인식된 음성이 없습니다.']
    else:
        for segment in segments:
            lines += [f'**[{_timestamp(segment["start"])}]** {_escape_text(segment["text"])}', '']
    control.check()
    document['video_text'] = '\n'.join(lines).strip()
    document['video_transcript'] = {'version': TRANSCRIPT_VERSION, 'video_id': document['video_id'],
        'status': 'success', 'method': method, 'model': result.get('model'),
        'automatic': bool(result.get('automatic')),
        'language': result.get('language'), 'segments': len(segments),
        'created_at': datetime.now(timezone.utc).isoformat()}
    document.pop('video_error', None)
    control.emit('transcribing', f'영상 텍스트 저장 준비 완료: {len(segments):,}개 구간')
