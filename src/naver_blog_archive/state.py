from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
import re
import sqlite3
from uuid import NAMESPACE_URL, uuid5

from .addresses import canonical_post_url


SCHEMA_VERSION = 2
_POST_COLUMNS_V1 = {
    'blog', 'post', 'depth', 'status', 'error', 'document', 'path', 'file_hash',
    'pending_hash', 'content_ok', 'attempts', 'updated',
}
_CATALOG_COLUMNS = {'archive_id', 'title', 'body_text', 'published_at', 'archived_at'}


def archive_id_for(blog: str, post: str) -> str:
    """The same Naver post keeps its ID even when its archive is rebuilt."""
    return 'NBA-' + uuid5(NAMESPACE_URL, canonical_post_url(blog, post)).hex


def document_metadata(document: str | None, fallback_title: str = '') -> dict:
    """Extract searchable cached text without reading or changing Markdown files."""
    try:
        data = json.loads(document) if document else {}
    except (TypeError, ValueError, RecursionError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    body = data.get('markdown')
    body = body if isinstance(body, str) else ''
    replacements = {}
    for name, label in (('images', 'alt'), ('sources', 'title'), ('files', 'name')):
        entries = data.get(name)
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                continue
            token, value = entry.get('token'), entry.get(label)
            if isinstance(token, str) and token:
                if name == 'sources' and entry.get('kind') == 'link_card':
                    value = ' '.join(entry[key] for key in ('title', 'description', 'domain')
                                     if isinstance(entry.get(key), str) and entry[key])
                replacements[token] = value if isinstance(value, str) else ''
    if replacements:
        pattern = '|'.join(re.escape(token) for token in sorted(replacements, key=len, reverse=True))
        body = re.sub(pattern, lambda match: replacements[match.group()], body)
    title = data.get('title')
    return {
        'title': title if isinstance(title, str) and title else fallback_title,
        'body_text': body,
        'published_at': data.get('published_at') if isinstance(data.get('published_at'), str) else None,
        'archived_at': data.get('archived_at') if isinstance(data.get('archived_at'), str) else None,
    }


def validate_schema(db: sqlite3.Connection, version: int) -> None:
    required = _POST_COLUMNS_V1 | (_CATALOG_COLUMNS if version == 2 else set())
    columns = {row[1] for row in db.execute('PRAGMA table_info(posts)')}
    runs = {row[1] for row in db.execute('PRAGMA table_info(runs)')}
    assets = {row[1] for row in db.execute('PRAGMA table_info(assets)')}
    if (not required <= columns or not {'id', 'blog', 'started', 'status'} <= runs
            or not {'url', 'path', 'file_hash', 'status', 'error'} <= assets):
        raise ValueError('상태 DB 구조가 올바르지 않습니다. 원본 DB를 보관하고 백업 폴더를 확인하세요.')


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class State:
    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(root / 'archive_state.sqlite')
        self.db.row_factory = sqlite3.Row
        try:
            self._initialize()
        except BaseException:
            self.db.close()
            raise

    def _initialize(self):
        version = self.db.execute('PRAGMA user_version').fetchone()[0]
        if version not in (0, 1, SCHEMA_VERSION):
            raise ValueError(f'지원하지 않는 상태 DB 버전: {version}')
        if version == SCHEMA_VERSION:
            validate_schema(self.db, version)
            return
        # SQLite DDL is transactional when executed individually. Avoid
        # executescript(), whose implicit commit could leave a partial upgrade.
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if version == 0:
                existing = self.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchone()
                if existing:
                    validate_schema(self.db, 1)
                self._create_tables()
            validate_schema(self.db, 1)
            self.db.execute("ALTER TABLE posts ADD COLUMN archive_id TEXT NOT NULL DEFAULT ''")
            self.db.execute("ALTER TABLE posts ADD COLUMN title TEXT NOT NULL DEFAULT ''")
            self.db.execute("ALTER TABLE posts ADD COLUMN body_text TEXT NOT NULL DEFAULT ''")
            self.db.execute('ALTER TABLE posts ADD COLUMN published_at TEXT')
            self.db.execute('ALTER TABLE posts ADD COLUMN archived_at TEXT')
            # Read small batches of cached documents; migration should not
            # duplicate the entire archive's text in Python memory.
            last_key = None
            while True:
                clause = ' WHERE (blog,post) > (?,?)' if last_key else ''
                batch = self.db.execute('SELECT blog,post,document FROM posts' + clause
                                        + ' ORDER BY blog,post LIMIT 100', last_key or ()).fetchall()
                if not batch:
                    break
                for row in batch:
                    metadata = document_metadata(row['document'], row['post'])
                    self.db.execute('''UPDATE posts SET archive_id=?,title=?,body_text=?,published_at=?,archived_at=?
                        WHERE blog=? AND post=?''',
                                    (archive_id_for(row['blog'], row['post']), *metadata.values(),
                                     row['blog'], row['post']))
                last_key = batch[-1]['blog'], batch[-1]['post']
            self.db.execute('CREATE UNIQUE INDEX posts_archive_id ON posts(archive_id)')
            self.db.execute('''CREATE INDEX posts_saved_order ON posts(
                COALESCE(archived_at,'') DESC,blog,post) WHERE path IS NOT NULL AND path != '' ''')
            self.db.execute('PRAGMA user_version=2')

    def _create_tables(self):
        self.db.execute('''
            CREATE TABLE IF NOT EXISTS posts (
                blog TEXT NOT NULL, post TEXT NOT NULL, depth INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', error TEXT NOT NULL DEFAULT '',
                document TEXT, path TEXT, file_hash TEXT, pending_hash TEXT, content_ok INTEGER NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0, updated TEXT NOT NULL,
                PRIMARY KEY (blog, post))''')
        self.db.execute('''
            CREATE TABLE IF NOT EXISTS assets (
                url TEXT PRIMARY KEY, path TEXT, file_hash TEXT,
                status TEXT NOT NULL DEFAULT 'pending', error TEXT NOT NULL DEFAULT '')''')
        self.db.execute('''
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY, blog TEXT NOT NULL, started TEXT NOT NULL,
                finished TEXT, status TEXT NOT NULL, listing_complete INTEGER NOT NULL DEFAULT 0,
                listed INTEGER NOT NULL DEFAULT 0, total INTEGER, error TEXT NOT NULL DEFAULT '',
                options TEXT NOT NULL DEFAULT '{}')''')

    def close(self):
        self.db.close()

    def discover(self, blog: str, post: str, depth: int = 0):
        with self.db:
            self.db.execute('''INSERT INTO posts (blog,post,depth,updated,archive_id,title) VALUES (?,?,?,?,?,?)
                ON CONFLICT(blog,post) DO UPDATE SET depth=MIN(posts.depth,excluded.depth)''',
                            (blog, post, depth, now(), archive_id_for(blog, post), post))

    def get(self, blog: str, post: str):
        return self.db.execute('SELECT * FROM posts WHERE blog=? AND post=?', (blog, post)).fetchone()

    def posts(self):
        return self.db.execute('SELECT * FROM posts ORDER BY depth,blog,post').fetchall()

    def update(self, blog: str, post: str, **values):
        allowed = {'status', 'error', 'document', 'path', 'file_hash', 'pending_hash', 'content_ok', 'attempts'}
        if not values.keys() <= allowed:
            raise ValueError('잘못된 상태 필드')
        # A refresh clears the processing document before fetching. Keep the
        # last cached catalog text so an existing saved file remains searchable
        # if that fetch fails; a later document replaces every cached field.
        if values.get('document') is not None:
            values.update(document_metadata(values['document'], post))
        values['updated'] = now()
        with self.db:
            self.db.execute('UPDATE posts SET ' + ','.join(k + '=?' for k in values) + ' WHERE blog=? AND post=?',
                            (*values.values(), blog, post))

    def asset(self, url: str):
        return self.db.execute('SELECT * FROM assets WHERE url=?', (url,)).fetchone()

    def save_asset(self, url: str, path=None, file_hash=None, status='failed', error=''):
        with self.db:
            self.db.execute('''INSERT INTO assets VALUES (?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET
                path=excluded.path,file_hash=excluded.file_hash,status=excluded.status,error=excluded.error''',
                            (url, path, file_hash, status, error))

    def start_run(self, blog: str, options: dict) -> int:
        with self.db:
            self.db.execute("UPDATE posts SET status='pending',error='이전 실행 중단' WHERE status='running'")
            self.db.execute("UPDATE runs SET status='interrupted',finished=? WHERE status='running'", (now(),))
            result = self.db.execute('INSERT INTO runs (blog,started,status,options) VALUES (?,?,?,?)',
                                     (blog, now(), 'running', json.dumps(options)))
        return result.lastrowid

    def listing(self, run_id: int, result):
        with self.db:
            self.db.execute('UPDATE runs SET listing_complete=?,listed=?,total=?,error=? WHERE id=?',
                            (result.complete, len(result.ids), result.total, result.error, run_id))

    def finish(self, run_id: int, status: str, error: str = ''):
        with self.db:
            self.db.execute('UPDATE runs SET finished=?,status=?,error=CASE WHEN ? != \'\' THEN ? ELSE error END WHERE id=?',
                            (now(), status, error, error, run_id))

    def latest_run(self, blog: str):
        return self.db.execute('SELECT * FROM runs WHERE blog=? ORDER BY id DESC LIMIT 1', (blog,)).fetchone()
