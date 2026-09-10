from dataclasses import replace
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
    first = Config('first_blog', tmp_path / '첫 보관함', delay=1.2, retries=5, download_images=False)
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
