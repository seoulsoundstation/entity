import json

import pytest

from naver_blog_archive.archive import backup, format_note, inspect_archive, reformat_archive, write_note
from naver_blog_archive.catalog import search_archive
from naver_blog_archive.files import digest
from naver_blog_archive.state import State
from test_archive import MAIN, FakeClient, FakeRenderer, config, png, saved_path
from test_cached_navigation import BODY, HEADER, MENU, document


def seed_navigation(config, *, failed_profile=False):
    cached = document(HEADER + MENU + BODY)
    cached['post_id'] = MAIN[1]
    state = State(config.out_dir)
    try:
        state.discover(*MAIN)
        state.update(*MAIN, document=json.dumps(cached, ensure_ascii=False))
        for index, image in enumerate(cached['images']):
            relative = f'attachments/image{index}.png'
            path = config.out_dir / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(png())
            state.save_asset(image['url'], relative, digest(path),
                             'failed' if index == 0 and failed_profile else 'success')
            if index == 0 and failed_profile:
                path.unlink()
        row = state.get(*MAIN)
        content, errors = format_note(config, state, row)
        write_note(config, state, row, content)
        state.update(*MAIN, content_ok=not errors, status='partial' if errors else 'success')
        return dict(state.get(*MAIN))
    finally:
        state.close()


@pytest.mark.parametrize('failed_profile', [False, True])
def test_offline_cleanup_preserves_article_files_identity_history_and_is_idempotent(config, monkeypatch, failed_profile):
    old = seed_navigation(config, failed_profile=failed_profile)
    before = saved_path(config).read_bytes()
    photos = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (config.out_dir / 'attachments').iterdir()}

    def no_network(*args, **kwargs):
        pytest.fail('navigation cleanup must not download articles or images')

    monkeypatch.setattr('naver_blog_archive.archive.Client', no_network)
    monkeypatch.setattr('naver_blog_archive.archive.Renderer', no_network)
    result = reformat_archive(config)
    assert result['output_counts'] == {'renamed': 0, 'updated': 1, 'navigation_posts': 1}
    assert result['posts'] == {'success': 1}
    assert not result['assets'] and not result['output_problems']
    assert any(p.read_bytes() == before for p in (config.out_dir / 'history').rglob('*.md'))
    assert photos == {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (config.out_dir / 'attachments').iterdir()}
    state = State(config.out_dir)
    try:
        current = state.get(*MAIN)
        cleaned = json.loads(current['document'])
        original = json.loads(old['document'])
        assert cleaned['markdown'] == BODY
        assert cleaned['sources'] == original['sources']
        assert cleaned['images'] == original['images'][1:]
        for key in ('archive_id', 'archived_at', 'published_at', 'attempts', 'path'):
            assert current[key] == old[key]
    finally:
        state.close()
    assert search_archive(config, '/PostList.naver')['total'] == 0
    assert search_archive(config, '카테고리')['total'] == 1
    note = saved_path(config)
    after = note.read_bytes(), note.stat().st_mtime_ns
    assert reformat_archive(config)['output_counts'] == {'renamed': 0, 'updated': 0}
    assert (note.read_bytes(), note.stat().st_mtime_ns) == after
    assert not inspect_archive(config, verify=True)['verification_problems']


def test_resume_cleans_cached_navigation_without_rendering_or_fetching_assets(config):
    seed_navigation(config, failed_profile=True)
    renderer, client = FakeRenderer({}), FakeClient()
    result = backup(config, renderer=renderer, client=client)
    assert result['output_counts']['navigation_posts'] == 1
    assert result['posts'] == {'success': 1}
    assert renderer.calls == client.calls == []
    note = saved_path(config)
    after = note.read_bytes(), note.stat().st_mtime_ns
    assert backup(config, renderer=renderer, client=client)['run_counts']['reused'] == 1
    assert (note.read_bytes(), note.stat().st_mtime_ns) == after


def test_cleanup_preserves_user_edited_markdown_and_its_cached_document(config):
    old = seed_navigation(config)
    note = saved_path(config)
    edited = note.read_bytes() + b'\nMy own edits\n'
    note.write_bytes(edited)
    result = reformat_archive(config)
    assert not result['output_counts'].get('navigation_posts')
    assert result['posts'] == {'partial': 1}
    assert note.read_bytes() == edited
    state = State(config.out_dir)
    try:
        assert state.get(*MAIN)['document'] == old['document']
    finally:
        state.close()
