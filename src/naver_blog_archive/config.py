from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
import json
import math
import os
import re
import sys
import tempfile
from urllib.parse import parse_qs, unquote, urlsplit

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


@dataclass(frozen=True)
class Config:
    blog_id: str
    out_dir: Path
    delay: float = 0.5
    download_images: bool = True
    follow_sources: bool = True
    source_depth: int = 1
    retries: int = 3
    timeout: float = 30
    max_image_mb: int = 30
    max_pages: int = 10000
    download_files: bool = True
    max_file_mb: int = 100


def normalize_blog_id(value: str) -> str:
    """Accept a blog ID or a public Naver homepage/post URL, without networking."""
    message = '네이버 블로그 ID 또는 blog.naver.com의 블로그 주소를 입력하세요.'
    if not isinstance(value, str):
        raise ValueError(message)
    value = value.strip()
    if re.fullmatch(r'[A-Za-z0-9_-]+', value):
        return value
    if any(character.isspace() or ord(character) < 32 for character in value):
        raise ValueError(message)
    if '://' not in value:
        value = 'https://' + value
    try:
        parts = urlsplit(value)
        if (parts.scheme not in ('http', 'https')
                or parts.hostname not in ('blog.naver.com', 'm.blog.naver.com')
                or parts.username is not None or parts.password is not None
                or parts.port is not None):
            raise ValueError(message)
        segments = [unquote(part) for part in parts.path.strip('/').split('/')]
        if len(segments) == 1 and segments[0].lower() in ('postview.naver', 'postlist.naver'):
            identifiers = parse_qs(parts.query, keep_blank_values=True).get('blogId', [])
            blog_id = identifiers[0] if len(identifiers) == 1 else ''
        elif len(segments) == 1 or (len(segments) == 2 and segments[1].isdigit()):
            blog_id = segments[0]
        else:
            blog_id = ''
    except ValueError as exc:
        raise ValueError(message) from exc
    if not re.fullmatch(r'[A-Za-z0-9_-]+', blog_id):
        raise ValueError(message)
    return blog_id


def config_from_mapping(data: Mapping[str, object], base_dir: str | Path | None = None) -> Config:
    """Validate file or GUI settings identically, without mutating the caller's data."""
    data = dict(data)
    unknown = set(data) - set(Config.__dataclass_fields__)
    if unknown:
        raise ValueError(f"알 수 없는 설정: {', '.join(sorted(map(str, unknown)))}")
    blog_id = data.get('blog_id')
    if not isinstance(blog_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', blog_id):
        raise ValueError('blog_id에 유효한 블로그 ID를 지정하세요.')
    out = data.get('out_dir', 'naver_blog_backup')
    if not isinstance(out, str) or not out.strip():
        raise ValueError('out_dir는 비어 있지 않은 경로여야 합니다.')
    out_path = Path(out).expanduser()
    base = Path(base_dir).expanduser() if base_dir is not None else Path.cwd()
    data['out_dir'] = (base / out_path).resolve()
    for key in ('download_images', 'follow_sources', 'download_files'):
        if key in data and type(data[key]) is not bool:
            raise ValueError(f'{key}는 true 또는 false여야 합니다.')
    for key, low, high in (('source_depth', 0, 3), ('retries', 1, 10),
                           ('max_image_mb', 1, 500), ('max_pages', 1, 100000),
                           ('max_file_mb', 1, 2048)):
        value = data.get(key, Config.__dataclass_fields__[key].default)
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f'{key}는 {low}~{high} 범위의 정수여야 합니다.')
    for key, allow_zero in (('delay', True), ('timeout', False)):
        value = data.get(key, Config.__dataclass_fields__[key].default)
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0 or (not allow_zero and value == 0):
            raise ValueError(f'{key}에 유효한 양수를 지정하세요. delay는 0도 가능합니다.')
    return Config(**data)


def load_config(path: str | Path) -> Config:
    path = Path(path).expanduser().resolve()
    with path.open('rb') as stream:
        return config_from_mapping(tomllib.load(stream), base_dir=path.parent)


def save_config(config: Config, path: str | Path) -> None:
    """Save validated settings atomically; output paths remain stable after moving files."""
    path = Path(path).expanduser().resolve()
    data = asdict(config)
    data['out_dir'] = str(config.out_dir)
    validated = asdict(config_from_mapping(data))
    validated['out_dir'] = str(validated['out_dir'])
    lines = []
    for key, value in validated.items():
        if isinstance(value, str):
            encoded = json.dumps(value, ensure_ascii=False).replace('\x7f', '\\u007f')
        elif isinstance(value, bool):
            encoded = 'true' if value else 'false'
        else:
            encoded = str(value)
        lines.append(f'{key} = {encoded}\n')
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n',
                                         dir=path.parent, prefix=f'.{path.name}.',
                                         suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            stream.writelines(lines)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
