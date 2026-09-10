"""Readable Windows filenames and recoverable migration of numeric filenames."""
from __future__ import annotations

import json
from pathlib import Path
import re
import unicodedata
from urllib.parse import unquote, urlsplit

from .files import atomic_write, digest, inside
from .progress import TaskControl
from .state import archive_id_for


def _units(value: str) -> int:
    return len(value.encode('utf-16-le')) // 2


def titled_path(config, row) -> str:
    document = json.loads(row['document']) if row['document'] else {}
    title = unicodedata.normalize('NFC', document.get('title') or '제목 없음')
    title = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', ' ', title)
    title = re.sub(r'\s+', ' ', title).strip(' .') or '제목 없음'
    if re.match(r'^(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\.|$)', title, re.I):
        title = '_' + title
    parent = f'posts/{row["blog"]}'
    suffix = f' ({row["post"]}).md'
    # Leave space under the traditional Windows path limit, including UTF-16
    # surrogate pairs. The post number distinguishes duplicate/truncated titles.
    budget = min(120, 240 - _units(str(inside(config.out_dir, parent))) - 1 - _units(suffix))
    if budget < 8:
        raise ValueError('저장 경로가 너무 깁니다. 더 짧은 저장 폴더를 선택하세요.')
    while _units(title) > budget:
        title = title[:-1]
    return f'{parent}/{title.rstrip(" .")}{suffix}'


def _finish_move(config, state, journal: Path):
    entry = json.loads(journal.read_text(encoding='utf-8'))
    blog, post = entry['blog'], entry['post']
    row = state.get(blog, post)
    old, new, checksum = entry['old'], entry['new'], entry['hash']
    if (not row or old != f'posts/{blog}/{post}.md'
            or Path(new).parent.as_posix() != f'posts/{blog}'
            or not new.endswith(f' ({post}).md') or row['path'] not in (old, new)):
        raise ValueError('파일명 변경 기록을 확인해야 합니다: ' + journal.name)
    source, target = inside(config.out_dir, old), inside(config.out_dir, new)
    if source.exists() and (not source.is_file() or digest(source) != checksum):
        raise ValueError('수정된 파일을 보호했습니다: ' + old)
    if target.exists():
        if not target.is_file() or digest(target) != checksum:
            raise ValueError('같은 제목 경로의 기존 파일을 보호했습니다: ' + new)
    elif source.is_file():
        atomic_write(target, source.read_bytes())
    else:
        raise ValueError('파일명 변경 중 원본 파일이 누락되었습니다: ' + old)
    # The original remains available until the new file and its DB path are
    # committed. The journal recovers a stop at any point, including DB commit.
    state.update(blog, post, path=new, file_hash=checksum, pending_hash=None)
    if source.exists():
        if digest(source) != checksum:
            raise ValueError('수정된 파일을 보호했습니다: ' + old)
        source.unlink()
    journal.unlink()


def recover_note_moves(config, state, control=None) -> list[str]:
    control = control or TaskControl()
    problems = []
    for journal in sorted((config.out_dir / '.note-moves').glob('*.json')):
        control.check()
        try:
            _finish_move(config, state, journal)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            problems.append(str(exc))
    return problems


def migrate_note_names(config, state, control=None, *, rows=None) -> tuple[int, list[str]]:
    control = control or TaskControl()
    renamed, problems = 0, []
    all_rows = state.posts()
    rows = list(rows) if rows is not None else all_rows
    selected = {(row['blog'], row['post']) for row in rows}
    blocked = set()
    by_path = {inside(config.out_dir, row['path']): (row['blog'], row['post'])
               for row in all_rows if row['path']}
    for parent in all_rows:
        control.check()
        path = inside(config.out_dir, parent['path']) if parent['path'] else None
        writable = (parent['blog'], parent['post']) in selected and parent['document']
        writable = writable and path and path.is_file() and parent['file_hash'] and digest(path) == parent['file_hash']
        if writable:
            continue
        # A parent excluded by settings or protected from rewriting must keep
        # its existing local citation links. Defer the referenced file's rename.
        if parent['document']:
            for source in json.loads(parent['document']).get('sources', []):
                if source.get('target'):
                    blocked.add(tuple(source['target']))
        if path and path.is_file():
            for href in re.findall(r'\]\(([^)]+)\)', path.read_text(encoding='utf-8-sig', errors='replace')):
                parts = urlsplit(href)
                if not parts.scheme and not parts.netloc:
                    target = (path.parent / unquote(parts.path)).resolve()
                    if target in by_path:
                        blocked.add(by_path[target])
    for row in rows:
        control.check()
        old = f'posts/{row["blog"]}/{row["post"]}.md'
        # Keep user-named legacy files and already assigned title paths stable,
        # even when the author changes the title during a later refresh.
        if row['path'] != old or not row['document']:
            continue
        if (row['blog'], row['post']) in blocked:
            problems.append('보존된 글의 링크 유지를 위해 파일명 변경 보류: ' + old)
            continue
        journal = config.out_dir / '.note-moves' / (archive_id_for(row['blog'], row['post']) + '.json')
        try:
            source = inside(config.out_dir, old)
            if not source.is_file():
                continue
            if not row['file_hash'] or digest(source) != row['file_hash']:
                raise ValueError('수정된 파일을 보호했습니다: ' + old)
            new = titled_path(config, row)
            target = inside(config.out_dir, new)
            if target.exists():
                raise ValueError('상태 기록이 없는 기존 파일을 보호했습니다: ' + new)
            if journal.exists():
                raise ValueError('완료되지 않은 파일명 변경 기록이 있습니다: ' + old)
            entry = dict(blog=row['blog'], post=row['post'], old=old, new=new, hash=row['file_hash'])
            atomic_write(journal, json.dumps(entry, ensure_ascii=False).encode('utf-8'))
            _finish_move(config, state, journal)
            renamed += 1
            control.emit('formatting', '제목으로 파일명 변경: ' + new)
        except Exception as exc:
            message = str(exc)
            state.update(row['blog'], row['post'], status='partial', content_ok=0, error=message)
            problems.append(message)
            if journal.exists():
                # Never recompose a target while its move still records the
                # old checksum. Recovery must finish before any later write.
                raise RuntimeError('파일명 변경을 마치지 못했습니다. 저장 결과 정리를 다시 실행하세요: ' + message) from exc
    return renamed, problems
