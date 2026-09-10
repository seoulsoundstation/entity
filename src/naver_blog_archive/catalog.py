"""Read-only, local browsing of a per-blog archive, including older databases."""
from __future__ import annotations

from contextlib import contextmanager
import sqlite3

from .config import Config
from .state import SCHEMA_VERSION, archive_id_for, document_metadata, validate_schema


_ITEM_FIELDS = 'archive_id,blog_id,post_id,title,path,status,published_at,archived_at'


@contextmanager
def _reader(config: Config):
    path = config.out_dir / 'archive_state.sqlite'
    if not path.is_file():
        raise ValueError('저장된 글 DB가 없습니다. 먼저 백업을 실행하세요.')
    # Do not instantiate State here: browsing must never migrate/write the DB
    # and must work while archive_lock is held by the backup worker.
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=3)
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        version = db.execute('PRAGMA user_version').fetchone()[0]
        if version not in (1, SCHEMA_VERSION):
            raise ValueError(f'지원하지 않는 상태 DB 버전: {version}')
        validate_schema(db, version)
        owner = db.execute('SELECT blog FROM runs ORDER BY id LIMIT 1').fetchone()
        if owner:
            matches = owner['blog'] == config.blog_id
        else:
            # An imported archive can have posts before its first recorded run.
            owners = db.execute('SELECT DISTINCT blog FROM posts WHERE depth=0').fetchall()
            matches = all(row['blog'] == config.blog_id for row in owners)
        if not matches:
            raise ValueError('이 폴더는 다른 주 블로그의 백업입니다. 별도 저장 폴더를 선택하세요.')
        if version == 1:
            db.create_function('catalog_id', 2, archive_id_for, deterministic=True)
            for field in ('title', 'body_text', 'published_at', 'archived_at'):
                db.create_function('catalog_' + field, 2,
                                   lambda document, post, key=field: document_metadata(document, post)[key],
                                   deterministic=True)
        yield db, version
    finally:
        db.close()


def _source(version: int) -> str:
    saved = "FROM posts WHERE path IS NOT NULL AND path != ''"
    if version == 1:
        return '''SELECT catalog_id(blog,post) AS archive_id,blog AS blog_id,post AS post_id,
            catalog_title(document,post) AS title,path,status,
            catalog_published_at(document,post) AS published_at,
            catalog_archived_at(document,post) AS archived_at,
            catalog_body_text(document,post) AS body_text ''' + saved
    return '''SELECT archive_id,blog AS blog_id,post AS post_id,
        COALESCE(NULLIF(title,''),post) AS title,path,status,published_at,archived_at,body_text ''' + saved


def search_archive(config: Config, query: str = '', limit: int = 100, offset: int = 0) -> dict:
    """Search saved paths by ID, title or cached body; return at most 500 rows.

    A row can remain listed after a failed refresh or a missing-file check. Its
    status is returned, and callers must check the stored path before opening it.
    Search is literal (including %, _ and quotes), with SQLite LIKE's usual
    ASCII case folding. The result and total share one read transaction.
    """
    if not isinstance(query, str) or len(query) > 1000:
        raise ValueError('검색어는 1,000자 이하의 문자열이어야 합니다.')
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError('목록 개수는 1~500 사이의 정수여야 합니다.')
    if type(offset) is not int or offset < 0:
        raise ValueError('목록 시작 위치는 0 이상의 정수여야 합니다.')
    condition, parameters = '', ()
    if query:
        literal = '%' + query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
        condition = " WHERE " + ' OR '.join(
            f"{field} LIKE ? ESCAPE '\\'" for field in ('archive_id', 'blog_id', 'post_id', 'title', 'body_text'))
        parameters = (literal,) * 5
    with _reader(config) as (db, version):
        source = 'WITH saved AS (' + _source(version) + ') '
        total = db.execute(source + 'SELECT COUNT(*) FROM saved' + condition, parameters).fetchone()[0]
        rows = db.execute(source + 'SELECT ' + _ITEM_FIELDS + ' FROM saved' + condition
                          + " ORDER BY COALESCE(archived_at,'') DESC,blog_id,post_id LIMIT ? OFFSET ?",
                          (*parameters, limit, offset)).fetchall()
    return {'items': [dict(row) for row in rows], 'total': total, 'offset': offset, 'limit': limit}


def get_archived_post(config: Config, archive_id: str) -> dict | None:
    """Return cached detail for one saved ID without reading arbitrary files."""
    if not isinstance(archive_id, str):
        raise ValueError('보관 ID는 문자열이어야 합니다.')
    with _reader(config) as (db, version):
        row = db.execute('WITH saved AS (' + _source(version) + ') SELECT ' + _ITEM_FIELDS
                         + ',body_text FROM saved WHERE archive_id=? LIMIT 1', (archive_id,)).fetchone()
    if row is None:
        return None
    result = dict(row)
    result['source_url'] = f'https://blog.naver.com/{result["blog_id"]}/{result["post_id"]}'
    return result
