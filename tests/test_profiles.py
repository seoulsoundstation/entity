from dataclasses import replace
import json
import sqlite3

import pytest

from naver_blog_archive.config import Config, normalize_blog_id
from naver_blog_archive.profiles import ProfileStore


def test_missing_profiles_are_empty_without_creating_a_file(tmp_path):
    path = tmp_path / 'blog_profiles.sqlite'
    assert ProfileStore(path).list() == []
    assert not path.exists()


def test_profiles_persist_distinct_blog_settings_and_update_existing_address(tmp_path):
    path = tmp_path / 'blog_profiles.sqlite'
    store = ProfileStore(path)
    first = Config('first_blog', tmp_path / '첫 보관함', delay=1.2, retries=5, download_images=False,
                   download_files=False, max_file_mb=250, transcribe_videos=False)
    second = Config('second_blog', tmp_path / '다른 보관함', follow_sources=False, source_depth=2)
    store.save(first)
    store.save(second)
    updated = replace(first, out_dir=tmp_path / '변경한 폴더', timeout=60)
    store.save(updated)
    assert ProfileStore(path).list() == [updated, second]


def test_desktop_and_mobile_blog_addresses_share_the_same_profile(tmp_path):
    store = ProfileStore(tmp_path / 'profiles.sqlite')
    desktop = normalize_blog_id('https://blog.naver.com/demo/123')
    mobile = normalize_blog_id('https://m.blog.naver.com/demo')
    store.save(Config(desktop, tmp_path / 'old'))
    store.save(Config(mobile, tmp_path / 'new'))
    assert store.list() == [Config('demo', tmp_path / 'new')]


def test_invalid_settings_do_not_overwrite_saved_profile(tmp_path):
    store = ProfileStore(tmp_path / 'profiles.sqlite')
    original = Config('demo', tmp_path / 'archive')
    store.save(original)
    with pytest.raises(ValueError):
        store.save(replace(original, retries=0))
    assert store.list() == [original]


def test_unknown_profile_schema_is_never_overwritten(tmp_path):
    path = tmp_path / 'profiles.sqlite'
    db = sqlite3.connect(path)
    db.execute('PRAGMA user_version=200')
    db.close()
    store = ProfileStore(path)
    with pytest.raises(ValueError, match='버전'):
        store.list()
    with pytest.raises(ValueError, match='버전'):
        store.save(Config('demo', tmp_path / 'archive'))
    db = sqlite3.connect(path)
    assert db.execute('PRAGMA user_version').fetchone()[0] == 200
    db.close()


def test_old_saved_profile_gets_attachment_defaults_without_rewriting_database(tmp_path):
    path = tmp_path / 'profiles.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE profiles (blog_id TEXT PRIMARY KEY, config TEXT NOT NULL)')
        db.execute('PRAGMA user_version=1')
        db.execute('INSERT INTO profiles VALUES (?, ?)', ('demo', json.dumps({
            'blog_id': 'demo', 'out_dir': str(tmp_path / 'archive'), 'download_images': False,
        })))
    before = path.read_bytes()
    profiles = ProfileStore(path).list()
    assert len(profiles) == 1
    assert profiles[0].download_files is True
    assert profiles[0].max_file_mb == 100
    assert profiles[0].download_images is False
    assert profiles[0].transcribe_videos is True
    assert path.read_bytes() == before


def test_premium_channels_do_not_overwrite_each_other_or_same_owner_blog(tmp_path):
    store = ProfileStore(tmp_path / 'profiles.sqlite')
    configs = [
        Config('salarymoney', tmp_path / 'blog'),
        Config('premium/salarymoney/moneystock', tmp_path / 'premium'),
        Config('premium/salarymoney/other', tmp_path / 'other', download_images=False),
    ]
    for config in configs:
        store.save(config)
    updated = replace(configs[1], download_files=False, max_file_mb=250)
    store.save(updated)
    profiles = {config.blog_id: config for config in store.list()}
    assert profiles == {configs[0].blog_id: configs[0], updated.blog_id: updated,
                        configs[2].blog_id: configs[2]}
    with sqlite3.connect(store.path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 1
