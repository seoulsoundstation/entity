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


@pytest.mark.parametrize('value', [
    'https://contents.premium.naver.com/salarymoney/moneystock',
    'https://contents.premium.naver.com/salarymoney/moneystock/',
    'contents.premium.naver.com/salarymoney/moneystock',
    'premium/salarymoney/moneystock',
])
def test_normalize_premium_channel_address(value):
    assert normalize_blog_id(value) == 'premium/salarymoney/moneystock'


@pytest.mark.parametrize('key', [
    'premium/salarymoney', 'premium/salarymoney/moneystock/extra',
    'premium/../moneystock', 'premium/salarymoney/..',
    'premium/salarymoney/money:stock', 'premium/salarymoney/money\\stock',
    'premium/salarymoney%2Fother/moneystock',
    'https://contents.premium.naver.com/salarymoney/moneystock',
])
def test_config_rejects_invalid_premium_storage_keys(key, tmp_path):
    with pytest.raises(ValueError):
        config_from_mapping({'blog_id': key}, base_dir=tmp_path)


def test_premium_channel_config_roundtrip_preserves_existing_options(tmp_path):
    config = config_from_mapping({
        'blog_id': 'premium/salarymoney/moneystock', 'out_dir': '내 구독 보관함',
        'download_images': False, 'download_files': True, 'max_file_mb': 250,
    }, base_dir=tmp_path)
    path = tmp_path / 'premium.toml'
    save_config(config, path)
    assert load_config(path) == config
    assert config.blog_id == 'premium/salarymoney/moneystock'


def test_mapping_is_not_mutated_and_resolves_relative_directory(tmp_path):
    data = {'blog_id': 'demo', 'out_dir': '../results'}
    config = config_from_mapping(data, base_dir=tmp_path / 'settings')
    assert config.out_dir == tmp_path / 'results'
    assert data == {'blog_id': 'demo', 'out_dir': '../results'}


@pytest.mark.parametrize('setting', [
    {'delay': float('nan')}, {'timeout': 0}, {'retries': True},
    {'download_images': 'true'}, {'download_files': 'true'}, {'download_files': 1},
    {'transcribe_videos': 'true'}, {'transcribe_videos': 1},
    {'max_file_mb': 0}, {'max_file_mb': 2049}, {'max_file_mb': True},
    {'max_file_mb': 1.5}, {'source_depth': 4}, {'unexpected': 1},
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
        'download_files': False, 'max_file_mb': 2048, 'transcribe_videos': False,
    }, base_dir=tmp_path)
    target = tmp_path / 'settings' / 'saved.toml'
    save_config(config, target)
    monkeypatch.chdir(tmp_path.parent)
    assert load_config(target) == config


def test_old_config_enables_attachment_backup_with_default_limit(tmp_path):
    path = tmp_path / 'old.toml'
    path.write_text('blog_id = "demo"\ndownload_images = false\n', encoding='utf-8')
    config = load_config(path)
    assert config.download_files is True
    assert config.max_file_mb == 100
    assert config.download_images is False
    assert config.transcribe_videos is True


@pytest.mark.parametrize('limit', [1, 100, 2048])
def test_valid_attachment_size_limit(limit, tmp_path):
    config = config_from_mapping({'blog_id': 'demo', 'max_file_mb': limit}, base_dir=tmp_path)
    assert config.max_file_mb == limit


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
