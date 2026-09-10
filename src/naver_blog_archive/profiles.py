"""Remember a separate set of backup settings for each blog."""
from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import sqlite3

from .config import Config, config_from_mapping


class ProfileStore:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()

    def list(self) -> list[Config]:
        if not self.path.is_file():
            return []
        db = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True)
        try:
            if db.execute('PRAGMA user_version').fetchone()[0] != 1:
                raise ValueError('지원하지 않는 블로그 목록 DB 버전입니다.')
            rows = db.execute('SELECT config FROM profiles ORDER BY blog_id COLLATE NOCASE').fetchall()
        finally:
            db.close()
        return [config_from_mapping(json.loads(row[0]), base_dir=self.path.parent) for row in rows]

    def save(self, config: Config) -> None:
        data = asdict(config)
        data['out_dir'] = str(config.out_dir)
        validated = config_from_mapping(data)
        data = asdict(validated)
        data['out_dir'] = str(validated.out_dir)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=2)
        try:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 1):
                raise ValueError('지원하지 않는 블로그 목록 DB 버전입니다.')
            with db:
                db.execute('''CREATE TABLE IF NOT EXISTS profiles (
                    blog_id TEXT PRIMARY KEY, config TEXT NOT NULL)''')
                db.execute('''INSERT INTO profiles (blog_id,config) VALUES (?,?)
                    ON CONFLICT(blog_id) DO UPDATE SET config=excluded.config''',
                           (validated.blog_id, json.dumps(data, ensure_ascii=False)))
                db.execute('PRAGMA user_version=1')
        finally:
            db.close()
