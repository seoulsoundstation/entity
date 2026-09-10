from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from PIL import Image

from .files import atomic_write, digest, inside
from .network import original_image_url
from .progress import TaskControl


def image_candidates(url: str) -> list[str]:
    candidates = [original_image_url(url), url]
    # Naver link previews can retain a dead thumbnail proxy while the photo
    # named in its src query remains available. Try that original only after
    # the usual proxy URLs, and only for this known Naver endpoint.
    parts = urlsplit(url)
    if parts.hostname == 'dthumb-phinf.pstatic.net':
        source = parse_qs(parts.query).get('src', [''])[0].strip()
        if len(source) >= 2 and source[0] == source[-1] and source[0] in ('"', "'"):
            source = source[1:-1].strip()
        if source.startswith('//'):
            source = 'https:' + source
        try:
            parsed_source = urlsplit(source)
            if parsed_source.scheme in ('http', 'https') and parsed_source.hostname:
                candidates.extend([original_image_url(source), source])
        except ValueError:
            pass
    return list(dict.fromkeys(candidate for candidate in candidates if candidate))


class Assets:
    def __init__(self, config, state, client, control: TaskControl | None = None):
        self.config, self.state, self.client = config, state, client
        self.control = control
        self.attempted: set[str] = set()

    def obtain(self, url: str, referer: str) -> str | None:
        if self.control:
            self.control.check()
        saved = self.state.asset(url)
        if saved and saved['status'] == 'success':
            path = inside(self.config.out_dir, saved['path'])
            if path.is_file() and digest(path) == saved['file_hash']:
                return saved['path']
        if url in self.attempted:
            return None
        self.attempted.add(url)
        if self.control:
            self.control.emit('image', '이미지 저장 중', url=url)
        error = '이미지 URL이 없습니다.'
        for candidate in image_candidates(url):
            try:
                with self.client.get(candidate, headers={'Referer': referer}, stream=True) as response:
                    size, chunks = 0, []
                    for chunk in response.iter_content(64 * 1024):
                        if self.control:
                            self.control.check()
                        size += len(chunk)
                        if size > self.config.max_image_mb * 1024 * 1024:
                            raise ValueError('이미지 크기 제한을 초과했습니다.')
                        chunks.append(chunk)
                content = b''.join(chunks)
                with Image.open(BytesIO(content)) as img:
                    extension = {'JPEG': 'jpg', 'PNG': 'png', 'GIF': 'gif', 'WEBP': 'webp',
                                 'BMP': 'bmp', 'TIFF': 'tiff', 'AVIF': 'avif'}.get(img.format)
                    if not extension:
                        raise ValueError(f'지원하지 않는 이미지 형식: {img.format}')
                    img.verify()
                checksum = hashlib.sha256(content).hexdigest()
                relative = f'attachments/{checksum}.{extension}'
                if self.control:
                    self.control.check()
                atomic_write(inside(self.config.out_dir, relative), content)
                self.state.save_asset(url, relative, checksum, 'success')
                return relative
            except Exception as exc:
                error = f'{type(exc).__name__}: {exc}'
        self.state.save_asset(url, error=error)
        return None
