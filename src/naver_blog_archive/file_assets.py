"""Download attached files without executing or unpacking their contents."""
from __future__ import annotations

from email.message import Message
from email.utils import collapse_rfc2231_value
import hashlib
import os
from pathlib import Path
import re
import tempfile
import unicodedata
from urllib.parse import unquote, urlsplit

from .files import inside
from .progress import TaskControl


def _header(response, name: str) -> str:
    headers = getattr(response, 'headers', None) or {}
    return next((str(value) for key, value in headers.items() if key.lower() == name.lower()), '')


def _disposition_name(response) -> str:
    value = _header(response, 'Content-Disposition')
    if not value:
        return ''
    message = Message()
    message['Content-Disposition'] = value
    extended = next((value for key, value in message.get_params(header='Content-Disposition')
                     if key.lower() == 'filename' and isinstance(value, tuple)), None)
    # Prefer RFC 5987's original Unicode name to an ASCII fallback. Some
    # servers use percent escapes in the older filename parameter too.
    name = collapse_rfc2231_value(extended) if extended else unquote(message.get_filename() or '')
    try:
        return name.encode('latin-1').decode('utf-8')
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name


def _units(value: str) -> int:
    return len(value.encode('utf-16-le')) // 2


def _safe_name(value: str, parent: Path) -> str:
    # Never interpret a server-provided name as a relative or absolute path.
    name = unicodedata.normalize('NFC', value.replace('\\', '/').rsplit('/', 1)[-1])
    name = ''.join(character for character in name if unicodedata.category(character) != 'Cf')
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', ' ', name)
    name = re.sub(r'\s+', ' ', name).strip(' .') or '첨부파일'
    if re.match(r'^(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:[ .]|$)', name, re.I):
        name = '_' + name
    budget = min(120, 240 - _units(str(parent)) - 1)
    if budget < 12:
        raise ValueError('첨부파일 저장 경로가 너무 깁니다. 더 짧은 저장 폴더를 선택하세요.')
    extension = Path(name).suffix
    if len(extension) > 20 or _units(extension) > budget - 4:
        extension = ''
    stem = name[:-len(extension)] if extension else name
    while _units(stem + extension) > budget:
        stem = stem[:-1]
    return (stem.rstrip(' .') or 'file') + extension


def _explicit_html(name: str) -> bool:
    return Path(name.replace('\\', '/')).suffix.lower() in ('.html', '.htm', '.xhtml')


def _is_html(prefix: bytes, content_type: str) -> bool:
    if content_type.split(';', 1)[0].strip().lower() in ('text/html', 'application/xhtml+xml'):
        return True
    encoding = 'utf-16' if prefix.startswith((b'\xff\xfe', b'\xfe\xff')) else 'utf-8-sig'
    sample = prefix.decode(encoding, errors='replace').lstrip().lower()
    sample = re.sub(r'^(?:(?:<!--.*?-->|<\?xml\b.*?\?>)\s*)+', '', sample, flags=re.S)
    return bool(re.match(r'(?:<!doctype\s+html\b|<html\b|<head\b|<body\b|<form\b)', sample))


class FileAssets:
    def __init__(self, config, state, client, control: TaskControl | None = None):
        self.config, self.state, self.client = config, state, client
        self.control = control
        self.attempted: set[tuple[str, str]] = set()

    def _check(self):
        if self.control:
            self.control.check()

    def _verified(self, relative: str | None, checksum: str | None) -> bool:
        if not relative or not checksum:
            return False
        try:
            path = inside(self.config.out_dir, relative)
            if not path.is_file():
                return False
            value = hashlib.sha256()
            with path.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    self._check()
                    value.update(chunk)
            return value.hexdigest() == checksum
        except (OSError, ValueError):
            return False

    def _existing_content(self, checksum: str) -> str | None:
        rows = self.state.db.execute(
            "SELECT path FROM assets WHERE file_hash=? AND status='success' ORDER BY path", (checksum,))
        for row in rows:
            if self._verified(row['path'], checksum):
                return row['path']
        return None

    def _publish(self, temporary: Path, relative: str, checksum: str) -> None:
        target = inside(self.config.out_dir, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        # A hard link publishes the complete temporary file atomically and
        # refuses to overwrite an unrelated file that appeared at this path.
        try:
            os.link(temporary, target)
        except FileExistsError:
            if not self._verified(relative, checksum):
                raise ValueError('같은 첨부파일 경로의 기존 파일을 보호했습니다: ' + relative)
        except OSError:
            if os.name != 'nt':
                raise
            # FAT/exFAT do not support hard links. On Windows rename also
            # fails if the destination exists, preserving the same guarantee.
            try:
                os.rename(temporary, target)
            except FileExistsError:
                if not self._verified(relative, checksum):
                    raise ValueError('같은 첨부파일 경로의 기존 파일을 보호했습니다: ' + relative)

    def obtain(self, url: str, referer: str, name: str, *, asset_key: str | None = None) -> str | None:
        self._check()
        if not self.config.download_files:
            return None
        key = asset_key or url
        saved = self.state.asset(key)
        if saved and self._verified(saved['path'], saved['file_hash']):
            if saved['status'] != 'success':
                self.state.save_asset(key, saved['path'], saved['file_hash'], 'success')
            return saved['path']
        attempt = (key, url)
        if attempt in self.attempted:
            return None
        self.attempted.add(attempt)
        temporary = None
        try:
            parts = urlsplit(url)
            if parts.scheme not in ('http', 'https') or not parts.hostname:
                raise ValueError('HTTP(S) 첨부파일 URL만 다운로드할 수 있습니다.')
            if self.control:
                self.control.emit('file', '첨부파일 저장 중: ' + (name or '첨부파일'), url=url, name=name)
            limit = self.config.max_file_mb * 1024 * 1024
            with self.client.get(url, headers={'Referer': referer}, stream=True) as response:
                self._check()
                status = getattr(response, 'status_code', 200)
                if status >= 400:
                    raise ValueError(f'첨부파일 응답 HTTP {status}')
                declared = _header(response, 'Content-Length').strip()
                declared_size = int(declared) if declared.isdigit() else None
                if declared_size is not None and declared_size > limit:
                    raise ValueError('첨부파일 크기 제한을 초과했습니다.')
                disposition_name = _disposition_name(response)
                filename = disposition_name or name or unquote(parts.path.rsplit('/', 1)[-1]) or '첨부파일'
                allow_html = _explicit_html(disposition_name) or _explicit_html(name)
                root = inside(self.config.out_dir, 'attachments/files')
                root.mkdir(parents=True, exist_ok=True)
                fd, temporary_name = tempfile.mkstemp(prefix='.download-', suffix='.tmp', dir=root)
                temporary = Path(temporary_name)
                checksum, size, prefix = hashlib.sha256(), 0, b''
                with os.fdopen(fd, 'wb') as stream:
                    for chunk in response.iter_content(64 * 1024):
                        self._check()
                        if not chunk:
                            continue
                        size += len(chunk)
                        if size > limit:
                            raise ValueError('첨부파일 크기 제한을 초과했습니다.')
                        if len(prefix) < 4096:
                            prefix += chunk[:4096 - len(prefix)]
                        checksum.update(chunk)
                        stream.write(chunk)
                    if not size:
                        raise ValueError('첨부파일 응답이 비어 있습니다.')
                    if not allow_html and _is_html(prefix, _header(response, 'Content-Type')):
                        raise ValueError('첨부파일 대신 HTML 로그인 또는 오류 페이지가 반환되었습니다.')
                    encoding = _header(response, 'Content-Encoding').strip().lower()
                    if (declared_size is not None and encoding in ('', 'identity')
                            and declared_size != size):
                        raise ValueError('첨부파일 응답 크기가 일치하지 않습니다. 다운로드가 불완전합니다.')
                    self._check()
                    stream.flush()
                    os.fsync(stream.fileno())
            self._check()
            file_hash = checksum.hexdigest()
            relative = self._existing_content(file_hash)
            if relative is None:
                parent = f'attachments/files/{file_hash}'
                relative = parent + '/' + _safe_name(filename, inside(self.config.out_dir, parent))
                self._check()
                self._publish(temporary, relative, file_hash)
            self.state.save_asset(key, relative, file_hash, 'success')
            return relative
        except Exception as exc:
            # Keep the last known path and checksum even when repair fails.
            # A user can put the original file back and verify it offline.
            self.state.save_asset(key, saved['path'] if saved else None,
                                  saved['file_hash'] if saved else None,
                                  error=f'{type(exc).__name__}: {exc}')
            return None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
