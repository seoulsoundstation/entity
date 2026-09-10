from __future__ import annotations

from dataclasses import asdict
from collections import deque
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import quote, urlsplit

from .assets import Assets
from .config import Config
from .files import archive_lock, atomic_write, digest, inside
from .network import Client, Renderer, parse_post_url, post_url
from .note_paths import migrate_note_names, recover_note_moves, titled_path
from .parser import compose, extract_post, md_link
from .progress import OperationCancelled, TaskControl
from .state import State


def active(config: Config, row) -> bool:
    return row['depth'] == 0 or (config.follow_sources and row['depth'] <= config.source_depth)


def active_rows(config: Config, rows) -> list[dict]:
    """Current citation graph, with fresh shortest depths and preserved old files."""
    by_key = {(row['blog'], row['post']): dict(row) for row in rows}
    depths = {key: 0 for key, row in by_key.items() if row['depth'] == 0}
    queue = deque(depths)
    while queue:
        key = queue.popleft()
        depth = depths[key]
        row = by_key[key]
        if not config.follow_sources or depth >= config.source_depth or not row['document']:
            continue
        for source in json.loads(row['document'])['sources']:
            target = source.get('target')
            target = tuple(target) if target else None
            if target in by_key and target not in depths:
                depths[target] = depth + 1
                queue.append(target)
    return sorted((dict(by_key[key], depth=depth) for key, depth in depths.items()),
                  key=lambda row: (row['depth'], row['blog'], row['post']))


def active_assets(config: Config, rows) -> set[str]:
    if not config.download_images:
        return set()
    return {image['url'] for row in rows if active(config, row) and row['document']
            for image in json.loads(row['document'])['images']}


def scan_legacy(config: Config, state: State, control: TaskControl | None = None) -> int:
    count = 0
    for path in sorted(config.out_dir.glob('*.md')):
        if control:
            control.check()
        try:
            text = path.read_text(encoding='utf-8-sig')
            header = text.split('---', 2)[1] if text.startswith('---') and text.count('---') >= 2 else ''
            match = re.search(r'https?://(?:m\.)?blog\.naver\.com/[\w-]+/\d+', header)
            key = parse_post_url(match.group()) if match else None
            if key is None or state.get(*key):
                continue
            state.discover(*key, depth=0 if key[0] == config.blog_id else 1)
            state.update(*key, path=path.name, file_hash=digest(path), error='기존 파일 재검증 필요')
            count += 1
        except (OSError, UnicodeError):
            continue
    return count


def note_path(config: Config, row) -> str:
    if row['path']:
        return row['path']
    relative = titled_path(config, row)
    if inside(config.out_dir, relative).exists():
        raise ValueError(f'상태 기록이 없는 기존 파일을 보호했습니다: {relative}')
    return relative


def write_note(config: Config, state: State, row, content: bytes):
    relative = note_path(config, row)
    path = inside(config.out_dir, relative)
    if path.exists():
        if not row['file_hash'] or digest(path) != row['file_hash']:
            raise ValueError(f'수정된 파일을 보호했습니다: {relative}. 변경 내용을 보관한 뒤 파일을 다른 위치로 이동하고 재시도하세요.')
        if path.read_bytes() == content:
            return False
        backup = config.out_dir / 'history' / row['blog'] / row['post'] / (row['file_hash'] + '.md')
        if not backup.exists():
            atomic_write(backup, path.read_bytes())
    checksum = hashlib.sha256(content).hexdigest()
    state.update(row['blog'], row['post'], path=relative, pending_hash=checksum)
    atomic_write(path, content)
    state.update(row['blog'], row['post'], file_hash=checksum, pending_hash=None)
    return True


def format_note(config: Config, state: State, row, download=None):
    document = json.loads(row['document'])
    replacements, errors = {}, []
    relative = row['path'] or titled_path(config, row)
    base = inside(config.out_dir, relative).parent
    for image in document['images']:
        local = None
        if config.download_images:
            if download:
                local = download.obtain(image['url'], post_url(row['blog'], row['post']))
            else:
                asset = state.asset(image['url'])
                if asset and asset['status'] == 'success' and asset['path'] and inside(config.out_dir, asset['path']).is_file():
                    local = asset['path']
            if not local:
                errors.append('이미지 저장 실패: ' + image['url'])
        target = quote(Path(os.path.relpath(inside(config.out_dir, local), base)).as_posix(), safe='/') if local else image['url']
        replacements[image['token']] = md_link(image['alt'], target, image=True) if target else '[이미지 URL 누락]'
    for source in document['sources']:
        target = source.get('target')
        href = source['url']
        follow = config.follow_sources and row['depth'] < config.source_depth
        if follow and target:
            saved = state.get(*target)
            if saved and saved['content_ok'] and saved['path'] and inside(config.out_dir, saved['path']).is_file():
                href = quote(Path(os.path.relpath(inside(config.out_dir, saved['path']), base)).as_posix(), safe='/')
            else:
                errors.append('출처 저장 미완료: ' + source['url'])
        if follow and source.get('resolve_error'):
            errors.append('출처 주소 확인 실패: ' + source['url'])
        replacements[source['token']] = '> 출처: ' + md_link(source['title'], href) + ' · ' + md_link('원문', source['url'])
    return compose(document, replacements), errors


def verify_files(config: Config, state: State, control: TaskControl | None = None) -> list[str]:
    control = control or TaskControl()
    problems = []
    rows = active_rows(config, state.posts())
    needed_assets = active_assets(config, rows)
    for asset in state.db.execute('SELECT * FROM assets').fetchall():
        control.check()
        if asset['url'] not in needed_assets:
            continue
        if not asset['path'] or not asset['file_hash']:
            continue
        try:
            path = inside(config.out_dir, asset['path'])
            good = path.is_file() and digest(path) == asset['file_hash']
        except (OSError, ValueError):
            good = False
        if not good:
            message = '이미지 파일 누락 또는 변경: ' + asset['url']
            # Retain the expected location/hash so restoring the file can pass
            # a later offline verification without another download.
            state.save_asset(asset['url'], asset['path'], asset['file_hash'], 'failed', message)
            problems.append(message)
        elif asset['status'] != 'success':
            state.save_asset(asset['url'], asset['path'], asset['file_hash'], 'success')
    for index, row in enumerate(rows, 1):
        control.check()
        control.emit('verify', f'파일 확인: {row["blog"]}/{row["post"]}',
                     completed=index - 1, total=len(rows), blog_id=row['blog'], post_id=row['post'])
        errors = []
        content_ok = bool(row['document'])
        if row['path']:
            path = inside(config.out_dir, row['path'])
            if not path.is_file():
                errors.append('Markdown 파일 누락')
                content_ok = 0
            else:
                actual = digest(path)
                if row['pending_hash'] and actual == row['pending_hash']:
                    state.update(row['blog'], row['post'], file_hash=actual, pending_hash=None)
                elif not row['file_hash'] or actual != row['file_hash']:
                    errors.append('Markdown 파일 변경 감지; 자동 덮어쓰기 금지')
                    content_ok = 0
        elif row['document'] or row['status'] == 'success':
            errors.append('Markdown 파일 경로 누락')
            content_ok = 0
        if row['document'] and config.download_images:
            for image in json.loads(row['document'])['images']:
                asset = state.asset(image['url'])
                if not asset or asset['status'] != 'success':
                    errors.append('이미지 미완료: ' + image['url'])
                    content_ok = 0
        if errors:
            state.update(row['blog'], row['post'], status='partial' if row['document'] else 'pending',
                         content_ok=content_ok, error='; '.join(errors))
            problems.extend(f'{row["blog"]}/{row["post"]}: {e}' for e in errors)
        elif row['document']:
            state.update(row['blog'], row['post'], content_ok=content_ok)
    # All own files must be checked before evaluating citation links, including
    # cycles and parents whose source files were restored since the last check.
    for row in active_rows(config, state.posts()):
        control.check()
        if row['document'] and row['content_ok']:
            content, errors = format_note(config, state, row)
            if not errors and inside(config.out_dir, row['path']).read_bytes() != content:
                errors.append('Markdown 내용 갱신 필요: backup을 실행하세요.')
            state.update(row['blog'], row['post'], status='partial' if errors else 'success', error='; '.join(errors))
            problems.extend(f'{row["blog"]}/{row["post"]}: {e}' for e in errors)
    control.emit('verify', '파일 확인 완료', completed=len(rows), total=len(rows))
    return problems


def make_report(config: Config, state: State) -> dict:
    all_rows = state.posts()
    rows = active_rows(config, all_rows)
    needed_assets = active_assets(config, rows)
    counts = {}
    for row in rows:
        counts[row['status']] = counts.get(row['status'], 0) + 1
    latest = state.latest_run(config.blog_id)
    return {'blog_id': config.blog_id, 'out_dir': str(config.out_dir), 'excluded_posts': len(all_rows) - len(rows),
            'last_run': dict(latest) if latest else None, 'posts': counts,
            'failures': [{'blog_id': r['blog'], 'post_id': r['post'], 'status': r['status'], 'error': r['error']}
                         for r in rows if r['status'] != 'success'],
            'assets': [dict(r) for r in state.db.execute("SELECT url,status,error FROM assets WHERE status != 'success'") if r['url'] in needed_assets]}


def discover_sources(config: Config, state: State, client, control: TaskControl, row, document):
    if not config.follow_sources or row['depth'] >= config.source_depth:
        return
    for source in document['sources']:
        control.check()
        source.pop('resolve_error', None)
        if not source.get('target') and urlsplit(source['url']).hostname == 'naver.me':
            try:
                source['target'] = parse_post_url(client.resolve_source(source['url']))
            except Exception as exc:
                source['resolve_error'] = str(exc)
        if source.get('target'):
            state.discover(*source['target'], depth=row['depth'] + 1)


def reusable_post(config: Config, row, document) -> bool:
    if row['status'] != 'success' or not row['content_ok'] or not row['path'] or not document:
        return False
    # A larger citation depth or newly enabled source collection can expose a
    # short URL that the preceding offline file verification cannot resolve.
    return not (config.follow_sources and row['depth'] < config.source_depth
                and any(not source.get('target') and urlsplit(source['url']).hostname == 'naver.me'
                        for source in document['sources']))


def completed_run_counts(rows, attempted: set, reused: set) -> dict[str, int]:
    counts = {'saved': 0, 'reused': 0, 'failed': 0}
    for row in rows:
        key = row['blog'], row['post']
        if key in attempted:
            outcome = 'failed' if row['status'] != 'success' else 'reused' if key in reused else 'saved'
            counts[outcome] += 1
    return counts


def backup(config: Config, *, refresh: bool = False, client=None, renderer=None,
           control: TaskControl | None = None) -> dict:
    control = control or TaskControl()
    control.check()
    with archive_lock(config.out_dir):
        state = State(config.out_dir)
        client = client or Client(config, control=control)
        renderer = renderer or Renderer(config, control=control)
        run_id = None
        attempted, reused = set(), set()
        run_counts = {'saved': 0, 'reused': 0, 'failed': 0}
        try:
            owner = state.db.execute('SELECT blog FROM runs ORDER BY id LIMIT 1').fetchone()
            if owner and owner['blog'] != config.blog_id:
                raise ValueError('이 폴더는 다른 주 블로그의 백업입니다. 별도 out_dir를 지정하세요.')
            options = asdict(config)
            options['out_dir'] = str(options['out_dir'])
            options['refresh'] = refresh
            run_id = state.start_run(config.blog_id, options)
            control.emit('starting', '기존 백업 확인 중', blog_id=config.blog_id)
            move_problems = recover_note_moves(config, state, control)
            if move_problems:
                raise ValueError('; '.join(move_problems))
            scan_legacy(config, state, control)
            verify_files(config, state, control)
            control.check()
            control.emit('listing', '공개 글 목록 확인 중', listed=0)
            listing = client.listing(config.blog_id)
            control.check()
            state.listing(run_id, listing)
            for post in listing.ids:
                control.check()
                state.discover(config.blog_id, post)
            message = f'목록: {len(listing.ids)}개 / 수집 {"완료" if listing.complete else "미완료: " + listing.error}'
            print(message)
            control.emit('listing', message, listed=len(listing.ids), total=listing.total,
                         complete=listing.complete)
            assets = Assets(config, state, client, control=control)
            while True:
                control.check()
                current_rows = active_rows(config, state.posts())
                todo = [r for r in current_rows if (r['blog'], r['post']) not in attempted]
                if not todo:
                    break
                # Finish each depth before choosing the next depth. A refresh
                # can remove citations, so an old deeper source must not be
                # scheduled from the graph that existed before its parents ran.
                todo = [r for r in todo if r['depth'] == todo[0]['depth']]
                counts = {}
                for current in current_rows:
                    counts[current['status']] = counts.get(current['status'], 0) + 1
                for row in todo:
                    control.check()
                    key = row['blog'], row['post']
                    previous_status = row['status']
                    document = json.loads(row['document']) if row['document'] else None
                    title = document['title'] if document else ''
                    try:
                        if not refresh and reusable_post(config, row, document):
                            discover_sources(config, state, client, control, row, document)
                            attempted.add(key)
                            reused.add(key)
                            run_counts['reused'] += 1
                            message = f'기존 파일 재사용: {key[0]}/{key[1]} {title[:50]}'
                            print(message)
                            control.emit('skipped', message, completed=len(attempted), total=len(current_rows),
                                         blog_id=key[0], post_id=key[1], title=title, status='success',
                                         outcome='reused', counts=dict(counts), run_counts=dict(run_counts))
                            control.check()
                            continue
                        control.emit('post', f'글 처리 중: {key[0]}/{key[1]}',
                                     completed=len(attempted), total=len(current_rows),
                                     blog_id=key[0], post_id=key[1], title=title, status='running',
                                     run_counts=dict(run_counts))
                        control.check()
                        state.update(*key, status='running', attempts=row['attempts'] + 1)
                        if refresh or not row['document']:
                            if refresh:
                                state.update(*key, document=None, content_ok=0)
                            document = extract_post(renderer.render(*key), *key)
                            control.check()
                            state.update(*key, document=json.dumps(document, ensure_ascii=False), content_ok=0)
                            control.wait(config.delay)
                        discover_sources(config, state, client, control, row, document)
                        state.update(*key, document=json.dumps(document, ensure_ascii=False))
                        row = dict(state.get(*key), depth=row['depth'])
                        content, errors = format_note(config, state, row, assets)
                        control.check()
                        write_note(config, state, row, content)
                        own_errors = [e for e in errors if e.startswith('이미지')]
                        state.update(*key, content_ok=not own_errors, status='partial' if errors else 'success', error='; '.join(errors))
                        title = document['title']
                        message = f'{"부분 성공" if errors else "저장 확인"}: {key[0]}/{key[1]} {title[:50]}'
                        print(message)
                    except Exception as exc:
                        current = state.get(*key)
                        state.update(*key, status='partial' if current['document'] else 'failed', content_ok=0,
                                     error=f'{type(exc).__name__}: {exc}')
                        message = f'실패: {key[0]}/{key[1]} {exc}'
                        print(message)
                    attempted.add(key)
                    saved_status = state.get(*key)['status']
                    counts[previous_status] -= 1
                    if counts[previous_status] == 0:
                        del counts[previous_status]
                    counts[saved_status] = counts.get(saved_status, 0) + 1
                    outcome = 'saved' if saved_status == 'success' else 'failed'
                    run_counts[outcome] += 1
                    control.emit('post', message, completed=len(attempted), total=len(current_rows),
                                 blog_id=key[0], post_id=key[1], title=title,
                                 status=saved_status, outcome=outcome, counts=dict(counts), run_counts=dict(run_counts))
            control.emit('linking', '저장된 출처 연결 확인 중')
            renamed, output_problems = migrate_note_names(config, state, control, rows=active_rows(config, state.posts()))
            for row in active_rows(config, state.posts()):
                control.check()
                if row['document'] and row['content_ok']:
                    try:
                        content, errors = format_note(config, state, row)
                        write_note(config, state, row, content)
                        state.update(row['blog'], row['post'], status='partial' if errors else 'success', error='; '.join(errors))
                    except Exception as exc:
                        state.update(row['blog'], row['post'], status='partial', content_ok=0, error=str(exc))
            control.check()
            incomplete = bool(output_problems) or any(r['status'] != 'success' for r in active_rows(config, state.posts()))
            state.finish(run_id, 'partial' if incomplete or not listing.complete else 'success')
            report = make_report(config, state)
            report['output_counts'] = {'renamed': renamed}
            report['output_problems'] = output_problems
            report['run_counts'] = completed_run_counts(active_rows(config, state.posts()), attempted, reused)
            atomic_write(config.out_dir / 'report.json', json.dumps(report, ensure_ascii=False, indent=2).encode('utf-8'))
            control.emit('finished', '백업 처리 완료', status=report['last_run']['status'],
                         run_counts=report['run_counts'], report=report)
            return report
        except KeyboardInterrupt as exc:
            if run_id is not None:
                state.finish(run_id, 'interrupted', '사용자가 중단했습니다. 다음 backup에서 재개합니다.')
                with state.db:
                    state.db.execute("UPDATE posts SET status='pending', error='사용자 중단; 다음 백업에서 재개' WHERE status='running'")
                report = make_report(config, state)
                report['run_counts'] = completed_run_counts(active_rows(config, state.posts()), attempted, reused)
                atomic_write(config.out_dir / 'report.json', json.dumps(report, ensure_ascii=False, indent=2).encode('utf-8'))
                if isinstance(exc, OperationCancelled):
                    exc.report = report
                control.emit('interrupted', '백업을 중단했습니다. 저장된 작업은 다음 실행에서 이어집니다.',
                             status='interrupted', run_counts=report['run_counts'], report=report)
            raise
        except Exception as exc:
            if run_id is not None:
                state.finish(run_id, 'failed', str(exc))
            raise
        finally:
            try:
                renderer.close()
            finally:
                try:
                    client.close()
                finally:
                    state.close()


def inspect_archive(config: Config, verify=False, *, control: TaskControl | None = None) -> dict:
    control = control or TaskControl()
    control.check()
    if not (config.out_dir / 'archive_state.sqlite').is_file():
        raise ValueError('백업 상태 DB가 없습니다. 먼저 backup을 실행하세요.')
    with archive_lock(config.out_dir):
        state = State(config.out_dir)
        try:
            owner = state.db.execute('SELECT blog FROM runs ORDER BY id LIMIT 1').fetchone()
            if owner and owner['blog'] != config.blog_id:
                raise ValueError('이 폴더는 다른 주 블로그의 백업입니다. 별도 out_dir를 지정하세요.')
            problems = verify_files(config, state, control) if verify else []
            control.check()
            report = make_report(config, state)
            report['verification_problems'] = problems
            if verify:
                atomic_write(config.out_dir / 'verification.json', json.dumps(report, ensure_ascii=False, indent=2).encode('utf-8'))
            control.emit('finished', '파일 검사 완료' if verify else '백업 상태 확인 완료', report=report)
            return report
        finally:
            state.close()


def reformat_archive(config: Config, *, control: TaskControl | None = None) -> dict:
    """Rebuild saved Markdown and its links from the DB without network access."""
    control = control or TaskControl()
    control.check()
    if not (config.out_dir / 'archive_state.sqlite').is_file():
        raise ValueError('백업 상태 DB가 없습니다. 먼저 백업을 실행하세요.')
    with archive_lock(config.out_dir):
        state = State(config.out_dir)
        try:
            owner = state.db.execute('SELECT blog FROM runs ORDER BY id LIMIT 1').fetchone()
            if owner and owner['blog'] != config.blog_id:
                raise ValueError('이 폴더는 다른 주 블로그의 백업입니다. 별도 저장 폴더를 선택하세요.')
            problems = recover_note_moves(config, state, control)
            if problems:
                raise ValueError('; '.join(problems))
            verify_files(config, state, control)
            renamed, problems = migrate_note_names(config, state, control, rows=active_rows(config, state.posts()))
            updated = 0
            rows = active_rows(config, state.posts())
            for index, row in enumerate(rows, 1):
                control.check()
                if not row['document']:
                    continue
                try:
                    content, errors = format_note(config, state, row)
                    updated += bool(write_note(config, state, row, content))
                    state.update(row['blog'], row['post'], content_ok=not any(e.startswith('이미지') for e in errors),
                                 status='partial' if errors else 'success', error='; '.join(errors))
                except Exception as exc:
                    state.update(row['blog'], row['post'], status='partial', content_ok=0, error=str(exc))
                    problems.append(str(exc))
                control.emit('formatting', f'저장 결과 정리: {index} / {len(rows)}', completed=index, total=len(rows))
            # Missing notes can be restored from cached documents above. Rename
            # those too, then re-link every parent using the final DB paths.
            count, more = migrate_note_names(config, state, control, rows=active_rows(config, state.posts()))
            renamed += count
            problems.extend(more)
            for row in active_rows(config, state.posts()):
                control.check()
                if row['document'] and row['content_ok']:
                    try:
                        content, errors = format_note(config, state, row)
                        updated += bool(write_note(config, state, row, content))
                        state.update(row['blog'], row['post'], status='partial' if errors else 'success', error='; '.join(errors))
                    except Exception as exc:
                        state.update(row['blog'], row['post'], status='partial', content_ok=0, error=str(exc))
                        problems.append(str(exc))
            control.check()
            report = make_report(config, state)
            report['output_counts'] = {'renamed': renamed, 'updated': updated}
            report['output_problems'] = list(dict.fromkeys(problems))
            atomic_write(config.out_dir / 'formatting.json', json.dumps(report, ensure_ascii=False, indent=2).encode('utf-8'))
            control.emit('finished', '저장 결과 정리 완료', report=report)
            return report
        finally:
            state.close()
