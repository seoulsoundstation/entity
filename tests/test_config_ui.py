from dataclasses import replace
from pathlib import Path

import pytest

from naver_blog_archive.config import (
    config_from_mapping,
    load_config,
    normalize_blog_id,
    save_config,
)


@pytest.mark.parametrize('value', [
    'demo_blog-1',
    ' demo_blog-1 ',
    'https://blog.naver.com/demo_blog-1',
    'blog.naver.com/demo_blog-1/',
    'https://m.blog.naver.com/demo_blog-1/123456789?ref=menu',
    'https://blog.naver.com/PostView.naver?blogId=demo_blog-1&logNo=123456789',
    'https://m.blog.naver.com/PostList.naver?blogId=demo_blog-1',
])
def test_normalize_supported_blog_addresses(value):
    assert normalize_blog_id(value) == 'demo_blog-1'


@pytest.mark.parametrize('value', [
    '', None, 'demo blog',
    'https://example.com/demo',
    'https://blog.naver.com.example.com/demo',
    'https://blog.naver.com@evil.example/demo',
    'https://evil@blog.naver.com/demo',
    'ftp://blog.naver.com/demo',
    'https://blog.naver.com:9999/demo',
    'https://blog.naver.com/',
    'https://blog.naver.com/PostView.naver?blogId=one&blogId=two',
    'https://blog.naver.com/demo/not-a-post',
    'https://blog.naver.com/demo/more/123',
    'https://blog.naver.com/de%2Fmo',
    'https://blog.naver.com/de\nmo',
])
def test_normalize_rejects_unsupported_or_ambiguous_addresses(value):
    with pytest.raises(ValueError):
        normalize_blog_id(value)


def test_mapping_is_not_mutated_and_resolves_relative_directory(tmp_path):
    data = {'blog_id': 'demo', 'out_dir': '../results'}
    config = config_from_mapping(data, base_dir=tmp_path / 'settings')
    assert config.out_dir == tmp_path / 'results'
    assert data == {'blog_id': 'demo', 'out_dir': '../results'}


@pytest.mark.parametrize('setting', [
    {'delay': float('nan')}, {'timeout': 0}, {'retries': True},
    {'download_images': 'true'}, {'source_depth': 4}, {'unexpected': 1},
    {'blog_id': 'https://blog.naver.com/demo'},
])
def test_gui_mapping_uses_strict_file_validation(setting, tmp_path):
    with pytest.raises(ValueError):
        config_from_mapping({'blog_id': 'demo', **setting}, base_dir=tmp_path)


def test_save_roundtrip_preserves_unicode_spaces_options_and_absolute_directory(tmp_path, monkeypatch):
    config = config_from_mapping({
        'blog_id': 'demo', 'out_dir': '한글 폴더 📚', 'download_images': False,
        'follow_sources': False, 'source_depth': 3, 'retries': 8,
        'delay': 1.25, 'timeout': 25, 'max_image_mb': 99, 'max_pages': 15,
    }, base_dir=tmp_path)
    target = tmp_path / 'settings' / 'saved.toml'
    save_config(config, target)
    monkeypatch.chdir(tmp_path.parent)
    assert load_config(target) == config


def test_failed_replace_preserves_previous_config_and_removes_temporary_file(tmp_path, monkeypatch):
    config = config_from_mapping({'blog_id': 'demo'}, base_dir=tmp_path)
    target = tmp_path / 'config.toml'
    save_config(config, target)
    original = target.read_bytes()

    def fail_replace(*args):
        raise OSError('replace failed')

    monkeypatch.setattr('naver_blog_archive.config.os.replace', fail_replace)
    with pytest.raises(OSError, match='replace failed'):
        save_config(replace(config, delay=5), target)
    assert target.read_bytes() == original
    assert list(tmp_path.iterdir()) == [target]


def test_invalid_config_cannot_overwrite_existing_file(tmp_path):
    config = config_from_mapping({'blog_id': 'demo'}, base_dir=tmp_path)
    target = tmp_path / 'config.toml'
    save_config(config, target)
    original = target.read_bytes()
    with pytest.raises(ValueError):
        save_config(replace(config, retries=False), target)
    assert target.read_bytes() == original
