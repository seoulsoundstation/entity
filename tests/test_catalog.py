from dataclasses import replace
import json
import sqlite3

import pytest

from naver_blog_archive.catalog import get_archived_post, search_archive
from naver_blog_archive.config import Config
from naver_blog_archive.files import archive_lock
from naver_blog_archive.state import State, archive_id_for


@pytest.fixture
def config(tmp_path):
    return Config('demo', tmp_path / 'archive')


def document(title='한국어 제목', body='본문 검색 단어', date='2026-09-01T00:00:00+00:00'):
    return json.dumps({'title': title, 'markdown': body, 'images': [], 'sources': [],
                       'published_at': '2020-01-01', 'archived_at': date}, ensure_ascii=False)


def seed(config, posts=None):
    state = State(config.out_dir)
    run_id = state.start_run(config.blog_id, {})
    state.finish(run_id, 'success')
    for blog, post, data in posts or [('demo', '100', document())]:
        state.discover(blog, post, 0 if blog == config.blog_id else 1)
        state.update(blog, post, document=data, path=f'posts/{blog}/{post}.md',
                     file_hash='unchanged-hash', status='success', content_ok=1)
    return state


def legacy(config, docs=None):
    """Create the real version-1 shape without relying on the new State code."""
    config.out_dir.mkdir(parents=True)
    db = sqlite3.connect(config.out_dir / 'archive_state.sqlite')
    db.executescript('''
        CREATE TABLE posts (
            blog TEXT NOT NULL, post TEXT NOT NULL, depth INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', error TEXT NOT NULL DEFAULT '',
            document TEXT, path TEXT, file_hash TEXT, pending_hash TEXT,
            content_ok INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
            updated TEXT NOT NULL, PRIMARY KEY (blog,post));
        CREATE TABLE assets (url TEXT PRIMARY KEY,path TEXT,file_hash TEXT,
            status TEXT NOT NULL DEFAULT 'pending',error TEXT NOT NULL DEFAULT '');
        CREATE TABLE runs (id INTEGER PRIMARY KEY,blog TEXT NOT NULL,started TEXT NOT NULL,
            finished TEXT,status TEXT NOT NULL,listing_complete INTEGER NOT NULL DEFAULT 0,
            listed INTEGER NOT NULL DEFAULT 0,total INTEGER,error TEXT NOT NULL DEFAULT '',
            options TEXT NOT NULL DEFAULT '{}');
        INSERT INTO runs(blog,started,status) VALUES ('demo','2026-01-01','success');
        INSERT INTO assets VALUES ('https://example.test/a.png','attachments/a.png','imagehash','success','');
        PRAGMA user_version=1;
    ''')
    for post, data in docs or [('100', document())]:
        db.execute('''INSERT INTO posts(blog,post,depth,status,error,document,path,file_hash,
            pending_hash,content_ok,attempts,updated) VALUES (?,?,0,'partial','kept',?,?,?,NULL,1,4,?)''',
                   ('demo', post, data, f'posts/demo/{post}.md', 'originalhash', '2026-01-02'))
    db.commit()
    db.close()


def test_migration_keeps_existing_columns_assets_runs_and_markdown(config):
    legacy(config)
    note = config.out_dir / 'posts/demo/100.md'
    note.parent.mkdir(parents=True)
    note.write_text('User archive must remain byte-for-byte unchanged.', encoding='utf-8')
    original_file = note.read_bytes(), note.stat().st_mtime_ns
    with sqlite3.connect(config.out_dir / 'archive_state.sqlite') as db:
        columns = [row[1] for row in db.execute('PRAGMA table_info(posts)')]
        original_row = db.execute('SELECT * FROM posts').fetchone()
        original_assets = db.execute('SELECT * FROM assets').fetchall()
        original_runs = db.execute('SELECT * FROM runs').fetchall()
    state = State(config.out_dir)
    try:
        row = state.get('demo', '100')
        assert tuple(row[name] for name in columns) == original_row
        assert [tuple(row) for row in state.db.execute('SELECT * FROM assets')] == original_assets
        assert [tuple(row) for row in state.db.execute('SELECT * FROM runs')] == original_runs
        assert row['archive_id'] == archive_id_for('demo', '100')
        assert row['title'] == '한국어 제목'
        assert row['body_text'] == '본문 검색 단어'
        assert row['published_at'] == '2020-01-01'
        assert state.db.execute('PRAGMA user_version').fetchone()[0] == 2
        indexes = {row[1] for row in state.db.execute('PRAGMA index_list(posts)')}
        assert {'posts_archive_id', 'posts_saved_order'} <= indexes
    finally:
        state.close()
    assert (note.read_bytes(), note.stat().st_mtime_ns) == original_file


def test_ids_survive_refresh_rediscovery_reopen_and_rebuilt_database(config, tmp_path):
    state = seed(config)
    identity = state.get('demo', '100')['archive_id']
    state.update('demo', '100', document=None, status='running', content_ok=0)
    assert state.get('demo', '100')['title'] == '한국어 제목'
    state.update('demo', '100', document=document('변경한 제목', '새 본문'))
    state.discover('demo', '100')
    assert state.get('demo', '100')['archive_id'] == identity
    state.close()
    reopened = State(config.out_dir)
    assert reopened.get('demo', '100')['archive_id'] == identity
    reopened.close()
    rebuilt = seed(replace(config, out_dir=tmp_path / 'rebuilt'))
    assert rebuilt.get('demo', '100')['archive_id'] == identity
    rebuilt.discover('source', '100', 1)
    assert rebuilt.get('source', '100')['archive_id'] != identity
    with pytest.raises(sqlite3.IntegrityError):
        with rebuilt.db:
            rebuilt.db.execute('UPDATE posts SET archive_id=? WHERE blog=?', (identity, 'source'))
    rebuilt.close()


def test_migration_failure_rolls_back_all_schema_and_data_changes(config, monkeypatch):
    legacy(config, [('100', document()), ('200', document('두 번째'))])
    from naver_blog_archive import state as state_module
    original = state_module.archive_id_for

    def fail_second(blog, post):
        if post == '200':
            raise RuntimeError('simulated migration failure')
        return original(blog, post)

    monkeypatch.setattr(state_module, 'archive_id_for', fail_second)
    with pytest.raises(RuntimeError, match='simulated'):
        State(config.out_dir)
    with sqlite3.connect(config.out_dir / 'archive_state.sqlite') as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 1
        assert 'archive_id' not in {row[1] for row in db.execute('PRAGMA table_info(posts)')}
        assert db.execute('SELECT COUNT(*) FROM posts').fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM posts WHERE file_hash='originalhash'").fetchone()[0] == 2
    monkeypatch.setattr(state_module, 'archive_id_for', original)
    resumed = State(config.out_dir)
    assert len(resumed.posts()) == 2
    resumed.close()


@pytest.mark.parametrize('data', ['invalid-json', 'null', '[]', '{"title":42,"markdown":{}}', None])
def test_migration_preserves_unparseable_documents_and_saved_paths(config, data):
    legacy(config, [('100', data)])
    state = State(config.out_dir)
    assert state.get('demo', '100')['document'] == data
    state.close()
    item = search_archive(config)['items'][0]
    assert item['title'] == '100'
    assert item['path'] == 'posts/demo/100.md'


def test_catalog_searches_title_body_both_ids_and_includes_saved_sources(config):
    state = seed(config, [('demo', '100', document('첫 제목', '한국어 본문')),
                          ('source', '100', document('출처 제목', 'reference'))])
    state.discover('demo', '999')
    state.update('demo', '999', document=document('아직 저장하지 않은 글'))
    state.close()
    assert search_archive(config)['total'] == 2
    assert search_archive(config, '첫 제목')['items'][0]['blog_id'] == 'demo'
    assert search_archive(config, '한국어')['items'][0]['post_id'] == '100'
    assert search_archive(config, '100')['total'] == 2
    assert search_archive(config, 'source')['total'] == 1
    assert search_archive(config, archive_id_for('source', '100'))['items'][0]['blog_id'] == 'source'
    assert search_archive(config, '아직')['total'] == 0


@pytest.mark.parametrize('query', ['100%', 'under_score', "O'Reilly", '"인용"', r'folder\file', '%', '_'])
def test_literal_search_does_not_interpret_sql_or_like_wildcards(config, query):
    state = seed(config, [('demo', '100', document('100% under_score', 'O\'Reilly "인용" folder\\file')),
                          ('demo', '200', document('unrelated', 'ordinary'))])
    state.close()
    assert [item['post_id'] for item in search_archive(config, query)['items']] == ['100']
    assert search_archive(config, "' OR 1=1 --")['total'] == 0


def test_pagination_is_stable_bounded_and_reports_full_count(config):
    state = seed(config, [('demo', str(number), document(str(number), date=f'2026-09-0{number}'))
                          for number in range(1, 5)])
    state.close()
    result = search_archive(config, limit=2, offset=1)
    assert result['total'] == 4
    assert result['limit'] == 2 and result['offset'] == 1
    assert [row['post_id'] for row in result['items']] == ['3', '2']
    assert search_archive(config, limit=2, offset=4)['items'] == []


@pytest.mark.parametrize('options', [{'limit': 0}, {'limit': 501}, {'limit': True},
                                    {'offset': -1}, {'offset': 0.5}, {'query': None},
                                    {'query': 'x' * 1001}])
def test_catalog_rejects_unbounded_or_invalid_inputs(config, options):
    with pytest.raises(ValueError):
        search_archive(config, **options)


def test_metadata_sync_removes_stale_fields_and_retains_failed_refresh_detail(config):
    state = seed(config)
    state.update('demo', '100', document=None, content_ok=0, status='failed')
    item = get_archived_post(config, archive_id_for('demo', '100'))
    assert item['title'] == '한국어 제목' and item['status'] == 'failed'
    assert item['source_url'] == 'https://blog.naver.com/demo/100'
    assert item['body_text'] == '본문 검색 단어'
    state.update('demo', '100', document=json.dumps({'title': 'new', 'markdown': 'new body'}))
    new = get_archived_post(config, item['archive_id'])
    assert new['published_at'] is None and new['archived_at'] is None
    assert new['title'] == 'new' and new['body_text'] == 'new body'
    assert search_archive(config, '본문')['total'] == 0
    assert get_archived_post(config, 'not-an-id') is None
    state.close()


def test_cached_body_replaces_internal_tokens_with_human_text(config):
    data = json.dumps({'title': 'Photo', 'markdown': 'before IMAGE_TOKEN SOURCE_TOKEN after',
                       'images': [{'token': 'IMAGE_TOKEN', 'alt': '바닷가 사진'}],
                       'sources': [{'token': 'SOURCE_TOKEN', 'title': '원문 인용'}]})
    state = seed(config, [('demo', '100', data)])
    state.close()
    assert search_archive(config, '바닷가')['total'] == 1
    assert search_archive(config, '원문 인용')['total'] == 1
    assert search_archive(config, 'IMAGE_TOKEN')['total'] == 0


def test_read_only_catalog_works_while_backup_lock_and_uncommitted_writer_are_active(config):
    state = seed(config)
    try:
        with archive_lock(config.out_dir):
            state.db.execute('BEGIN IMMEDIATE')
            state.db.execute("UPDATE posts SET title='not yet committed'")
            result = search_archive(config)
            assert result['items'][0]['title'] == '한국어 제목'
            state.db.rollback()
    finally:
        state.close()


def test_version_one_catalog_is_read_only_and_matches_after_migration(config):
    legacy(config)
    db_path = config.out_dir / 'archive_state.sqlite'
    before = db_path.read_bytes(), db_path.stat().st_mtime_ns
    with archive_lock(config.out_dir):
        old = search_archive(config, '본문')
        detail = get_archived_post(config, old['items'][0]['archive_id'])
    assert detail['body_text'] == '본문 검색 단어'
    assert before == (db_path.read_bytes(), db_path.stat().st_mtime_ns)
    state = State(config.out_dir)
    state.close()
    assert search_archive(config, '본문') == old


def test_catalog_rejects_other_blog_and_uses_depth_zero_when_no_runs(config):
    state = seed(config)
    with pytest.raises(ValueError, match='다른 주 블로그'):
        search_archive(replace(config, blog_id='other'))
    with state.db:
        state.db.execute('DELETE FROM runs')
    with pytest.raises(ValueError, match='다른 주 블로그'):
        search_archive(replace(config, blog_id='other'))
    assert search_archive(config)['total'] == 1
    state.close()


def test_missing_database_does_not_create_files(config):
    with pytest.raises(ValueError, match='DB가 없습니다'):
        search_archive(config)
    assert not config.out_dir.exists()


def test_unknown_version_is_rejected_without_modification(config):
    state = seed(config)
    state.db.execute('PRAGMA user_version=999')
    state.close()
    db_path = config.out_dir / 'archive_state.sqlite'
    before = db_path.read_bytes()
    with pytest.raises(ValueError, match='지원하지 않는'):
        search_archive(config)
    with pytest.raises(ValueError, match='지원하지 않는'):
        State(config.out_dir)
    assert db_path.read_bytes() == before


def test_invalid_version_two_shape_is_rejected(config):
    legacy(config)
    with sqlite3.connect(config.out_dir / 'archive_state.sqlite') as db:
        db.execute('PRAGMA user_version=2')
    with pytest.raises(ValueError, match='구조'):
        search_archive(config)
    with pytest.raises(ValueError, match='구조'):
        State(config.out_dir)


def test_unrelated_unversioned_database_is_not_modified(config):
    config.out_dir.mkdir()
    db_path = config.out_dir / 'archive_state.sqlite'
    with sqlite3.connect(db_path) as db:
        db.execute('CREATE TABLE unrelated(value TEXT)')
        db.execute("INSERT INTO unrelated VALUES ('keep me')")
    before = db_path.read_bytes()
    with pytest.raises(ValueError, match='구조'):
        State(config.out_dir)
    assert db_path.read_bytes() == before


def test_migration_backfills_every_batch(config):
    legacy(config, [(str(number), document(str(number))) for number in range(203)])
    state = State(config.out_dir)
    try:
        rows = state.posts()
        assert len(rows) == 203
        assert all(row['archive_id'] == archive_id_for(row['blog'], row['post']) for row in rows)
        assert all(row['title'] == row['post'] for row in rows)
    finally:
        state.close()
