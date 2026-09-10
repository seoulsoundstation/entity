from dataclasses import replace
import json
from pathlib import Path
from urllib.parse import unquote

import pytest

from naver_blog_archive.archive import backup, format_note, inspect_archive, reformat_archive, write_note
from naver_blog_archive.note_paths import titled_path
from naver_blog_archive.state import State, document_metadata
from test_archive import MAIN, SOURCE, PHOTO, FakeClient, FakeRenderer, html, run, saved_path, saved_link, config
from naver_blog_archive.network import Listing


def titled(body, title):
    return html(body).replace('>Title</div>', f'>{title}</div>')


def numeric_names(config):
    state = State(config.out_dir)
    originals = {}
    try:
        for row in state.posts():
            if not row['path']:
                continue
            source = config.out_dir / row['path']
            target = config.out_dir / f'posts/{row["blog"]}/{row["post"]}.md'
            source.rename(target)
            state.update(row['blog'], row['post'], path=target.relative_to(config.out_dir).as_posix())
            originals[row['blog'], row['post']] = dict(row)
        for row in state.posts():
            if row['path'] and row['document']:
                content, _ = format_note(config, state, row)
                write_note(config, state, row, content)
    finally:
        state.close()
    return originals


def test_new_filenames_contain_korean_title_and_post_number(config):
    run(config, {MAIN: titled(f'<p>본문</p><img src="{PHOTO}">', '나의 여행: 사진 / 기록?')})
    note = saved_path(config)
    assert note.name == '나의 여행 사진 기록 (123456789).md'
    assert '../../attachments/' in note.read_text(encoding='utf-8')
    assert not inspect_archive(config, verify=True)['verification_problems']


@pytest.mark.parametrize('title', ['CON', 'CON.txt', 'LPT1', 'aux', 'COM¹', '..', '  ', '\x00:*?<>|/\\'])
def test_windows_reserved_and_empty_titles_are_safe(config, title):
    path = titled_path(config, dict(blog='demo', post='123', document=json.dumps({'title': title})))
    name = Path(path).name
    assert name.endswith(' (123).md')
    assert not any(char in name for char in '<>:"/\\|?*\x00')
    assert name.split('.')[0].upper() not in {'CON', 'PRN', 'AUX', 'NUL', 'LPT1', 'COM¹'}


def test_long_title_is_truncated_without_splitting_unicode_and_ids_avoid_collisions(config):
    title = '여행😀' * 100
    a = titled_path(config, dict(blog='demo', post='123', document=json.dumps({'title': title})))
    b = titled_path(config, dict(blog='demo', post='124', document=json.dumps({'title': title})))
    assert a != b
    assert len(str(config.out_dir / a).encode('utf-16-le')) // 2 <= 240
    assert len(Path(a).name.encode('utf-16-le')) // 2 <= 129


def test_same_titles_keep_separate_posts_and_existing_title_path_is_stable(config):
    second = ('demo', '223456789')
    client = FakeClient(Listing([MAIN[1], second[1]], True, total=2))
    run(config, {MAIN: titled('One', '같은 제목'), second: titled('Two', '같은 제목')}, client)
    old = saved_path(config)
    assert old.name == '같은 제목 (123456789).md'
    assert saved_path(config, second).name == '같은 제목 (223456789).md'
    run(config, {MAIN: titled('New content', '바뀐 제목')}, refresh=True)
    assert saved_path(config) == old
    assert '# 바뀐 제목' in old.read_text(encoding='utf-8')


def test_reformat_renames_numeric_notes_and_relinks_sources_without_network(config, monkeypatch):
    citation = '<div class="se_sectionArea"><a href="https://blog.naver.com/source/987654321">출처</a></div>'
    run(config, {MAIN: titled(citation + f'<img src="{PHOTO}">', '내 글'),
                 SOURCE: titled('원문', '출처 # 100% [사진]')})
    originals = numeric_names(config)
    images = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (config.out_dir / 'attachments').iterdir()}
    def no_network(*args, **kwargs):
        pytest.fail('offline formatting must not create a network client or renderer')
    monkeypatch.setattr('naver_blog_archive.archive.Client', no_network)
    monkeypatch.setattr('naver_blog_archive.archive.Renderer', no_network)
    report = reformat_archive(config)
    assert report['output_counts']['renamed'] == 2
    assert report['posts'] == {'success': 2}
    assert not report['output_problems']
    note = saved_path(config)
    assert note.name == '내 글 (123456789).md'
    assert saved_link(config, MAIN, SOURCE) in note.read_text(encoding='utf-8')
    assert '# 100% [사진]' in unquote(saved_link(config, MAIN, SOURCE))
    assert not (config.out_dir / 'posts/demo/123456789.md').exists()
    assert images == {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in images}
    state = State(config.out_dir)
    try:
        for key, old in originals.items():
            current = state.get(*key)
            assert current['archive_id'] == old['archive_id']
            assert current['attempts'] == old['attempts']
    finally:
        state.close()
    assert inspect_archive(config, verify=True)['verification_problems'] == []
    before = {path: path.stat().st_mtime_ns for path in config.out_dir.glob('posts/**/*.md')}
    assert reformat_archive(config)['output_counts'] == {'renamed': 0, 'updated': 0}
    assert before == {path: path.stat().st_mtime_ns for path in before}


def test_normal_resume_migrates_numeric_notes_without_rendering(config):
    run(config)
    numeric_names(config)
    renderer, client = FakeRenderer({}), FakeClient()
    result = backup(config, client=client, renderer=renderer)
    assert not renderer.calls and not client.calls
    assert result['run_counts'] == {'saved': 0, 'reused': 1, 'failed': 0}
    assert result['output_counts']['renamed'] == 1
    assert saved_path(config).name == 'Title (123456789).md'


@pytest.mark.parametrize('failure_point', ['before_db', 'after_db'])
def test_title_migration_recovers_after_interruption_without_losing_original(config, monkeypatch, failure_point):
    run(config)
    numeric_names(config)
    old = saved_path(config)
    content = old.read_bytes()
    original_update, original_unlink = State.update, Path.unlink
    def update(self, blog, post, **values):
        if failure_point == 'before_db' and values.get('path', '').endswith(' (123456789).md'):
            raise KeyboardInterrupt('stopped after writing new file')
        return original_update(self, blog, post, **values)
    def unlink(path, *args, **kwargs):
        if failure_point == 'after_db' and path == old:
            raise KeyboardInterrupt('stopped after DB commit')
        return original_unlink(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(State, 'update', update)
        patch.setattr(Path, 'unlink', unlink)
        with pytest.raises(KeyboardInterrupt):
            reformat_archive(config)
    assert old.read_bytes() == content
    assert list((config.out_dir / '.note-moves').glob('*.json'))
    result = reformat_archive(config)
    assert not result['output_problems']
    assert saved_path(config).read_bytes() == content
    assert not old.exists()
    assert not list((config.out_dir / '.note-moves').glob('*.json'))


@pytest.mark.parametrize('edited', ['original', 'destination'])
def test_reformat_preserves_edited_or_colliding_files(config, edited):
    run(config)
    numeric_names(config)
    original = saved_path(config)
    destination = original.with_name('Title (123456789).md')
    protected = original if edited == 'original' else destination
    protected.write_text('My personal notes', encoding='utf-8')
    result = reformat_archive(config)
    assert result['output_problems']
    assert protected.read_text(encoding='utf-8') == 'My personal notes'
    assert original.is_file()
    assert saved_path(config) == original


def test_reformat_can_restore_missing_markdown_but_does_not_redownload_images(config):
    run(config, {MAIN: html(f'<img src="{PHOTO}">')})
    numeric_names(config)
    saved_path(config).unlink()
    for image in (config.out_dir / 'attachments').iterdir():
        image.unlink()
    result = reformat_archive(config)
    assert saved_path(config).name == 'Title (123456789).md'
    assert saved_path(config).is_file()
    assert result['posts'] == {'partial': 1}
    assert result['assets']
    assert not list((config.out_dir / 'attachments').iterdir())


def test_reformat_rejects_other_blog_and_missing_db(config):
    with pytest.raises(ValueError, match='상태 DB'):
        reformat_archive(config)
    run(config)
    with pytest.raises(ValueError, match='다른 주 블로그'):
        reformat_archive(replace(config, blog_id='other'))


def test_catalog_image_tokens_do_not_overlap():
    doc = {'title': 'Photos', 'markdown': 'TOKENIMAGE1 TOKENIMAGE10 TOKENIMAGE11',
           'images': [{'token': f'TOKENIMAGE{i}', 'alt': f'Photo {i}'} for i in range(12)]}
    assert document_metadata(json.dumps(doc))['body_text'] == 'Photo 1 Photo 10 Photo 11'


def test_unfinished_move_is_not_rewritten_until_journal_cleanup_succeeds(config, monkeypatch):
    run(config)
    numeric_names(config)
    old = saved_path(config)
    old_bytes = old.read_bytes()
    state = State(config.out_dir)
    document = json.loads(state.get(*MAIN)['document'])
    document['markdown'] = 'New cached content requiring output regeneration'
    state.update(*MAIN, document=json.dumps(document))
    state.close()
    unlink = Path.unlink
    def deny_journal_cleanup(path, *args, **kwargs):
        if path.parent.name == '.note-moves':
            raise PermissionError('journal temporarily busy')
        return unlink(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'unlink', deny_journal_cleanup)
        with pytest.raises(RuntimeError, match='파일명 변경을 마치지'):
            reformat_archive(config)
    assert saved_path(config).read_bytes() == old_bytes
    assert reformat_archive(config)['output_problems'] == []
    assert document['markdown'] in saved_path(config).read_text(encoding='utf-8')
    assert not list((config.out_dir / '.note-moves').glob('*.json'))


def test_excluded_source_files_and_their_links_keep_their_names(config):
    third = ('deep', '333333333')
    def cite(key):
        return f'<div class="se_sectionArea"><a href="https://blog.naver.com/{key[0]}/{key[1]}">S</a></div>'
    config = replace(config, source_depth=2)
    run(config, {MAIN: html(cite(SOURCE)), SOURCE: html(cite(third)), third: html('Deep source')})
    numeric_names(config)
    preserved = {saved_path(config, key): saved_path(config, key).read_bytes() for key in (SOURCE, third)}
    report = reformat_archive(replace(config, follow_sources=False))
    assert report['output_counts']['renamed'] == 1
    assert preserved == {path: path.read_bytes() for path in preserved}
    assert '../deep/333333333.md' in saved_path(config, SOURCE).read_text(encoding='utf-8')


def test_edited_parents_keep_working_numeric_source_links(config):
    citation = '<div class="se_sectionArea"><a href="https://blog.naver.com/source/987654321">Source</a></div>'
    run(config, {MAIN: html(citation), SOURCE: html('Source')})
    numeric_names(config)
    parent, source = saved_path(config), saved_path(config, SOURCE)
    edited = parent.read_text(encoding='utf-8') + '\nMy edits\n'
    parent.write_text(edited, encoding='utf-8')
    report = reformat_archive(config)
    assert any('링크 유지를 위해' in error for error in report['output_problems'])
    assert parent.read_text(encoding='utf-8') == edited
    assert source.is_file()
    assert '../source/987654321.md' in edited
