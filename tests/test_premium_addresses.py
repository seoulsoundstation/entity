from uuid import NAMESPACE_URL, uuid5

import pytest
import requests

from naver_blog_archive.addresses import (
    canonical_post_url, channel_url, is_premium, normalize_blog_id, parse_post_url, post_url, valid_source_key,
)
from naver_blog_archive.config import Config
from naver_blog_archive.network import Client, apply_premium_cookies
from naver_blog_archive.state import archive_id_for


KEY = 'premium/salarymoney/moneystock'
CHANNEL = 'https://contents.premium.naver.com/salarymoney/moneystock'
POST = '260911082659513hl'
ARTICLE = CHANNEL + '/contents/' + POST


@pytest.mark.parametrize('value', [KEY, CHANNEL, CHANNEL + '/', CHANNEL + '/contents', ARTICLE,
                                  ARTICLE + '?from=news', CHANNEL.removeprefix('https://')])
def test_channel_and_article_urls_normalize_to_same_noncolliding_key(value):
    assert normalize_blog_id(value) == KEY
    assert valid_source_key(KEY) and is_premium(KEY)
    assert not is_premium('premium--salarymoney--moneystock')


@pytest.mark.parametrize('value', [
    'premium/../channel', 'premium/owner/channel/more', 'premium/owner/',
    'https://contents.premium.naver.com.evil.test/a/b',
    'https://contents.premium.naver.com/a/%2e%2e', 'https://contents.premium.naver.com/a%2fb/c',
    'https://user:password@contents.premium.naver.com/a/b', 'https://contents.premium.naver.com:443/a/b',
    'https://contents.premium.naver.com/a/b?x=has space', 'https://contents.premium.naver.com/a/b/notices',
])
def test_invalid_channel_keys_and_urls_are_rejected(value):
    with pytest.raises(ValueError):
        normalize_blog_id(value)


def test_premium_sources_round_trip_and_legacy_id_seed_does_not_change():
    assert parse_post_url(ARTICLE) == (KEY, POST)
    assert parse_post_url(CHANNEL) is None
    assert parse_post_url(CHANNEL + '/contents/../other') is None
    assert post_url(KEY, POST) == canonical_post_url(KEY, POST) == ARTICLE
    assert channel_url(KEY) == CHANNEL
    assert archive_id_for(KEY, POST) == 'NBA-' + uuid5(NAMESPACE_URL, ARTICLE).hex
    assert archive_id_for('demo', '123') == 'NBA-' + uuid5(NAMESPACE_URL, 'https://blog.naver.com/demo/123').hex
    assert post_url('demo', '123') == 'https://m.blog.naver.com/demo/123'


def test_authenticated_http_cookies_are_limited_to_https_content_host(tmp_path):
    client = Client(Config(KEY, tmp_path))
    try:
        state = {'cookies': [
            {'name': 'NID_AUT', 'value': 'test-only', 'domain': '.naver.com', 'path': '/', 'secure': False},
            {'name': 'nid-only', 'value': 'do-not-copy', 'domain': 'nid.naver.com', 'path': '/'},
            {'name': 'external', 'value': 'do-not-copy', 'domain': 'example.com', 'path': '/'},
            {'name': 'path-only', 'value': 'scoped', 'domain': '.premium.naver.com', 'path': '/salarymoney'},
        ]}
        apply_premium_cookies(client, state)
        def cookie_for(url):
            return client.session.prepare_request(requests.Request('GET', url)).headers.get('Cookie', '')
        assert 'NID_AUT=test-only' in cookie_for(ARTICLE)
        assert 'path-only=scoped' in cookie_for(ARTICLE)
        assert 'do-not-copy' not in cookie_for(ARTICLE)
        for url in ('http://contents.premium.naver.com/salarymoney', 'https://blog.naver.com/demo/123',
                    'https://nid.naver.com/', 'https://download.blog.naver.com/a.pdf',
                    'https://example.com/image.png', 'https://contents.premium.naver.com.evil.test/'):
            assert not cookie_for(url)
        assert 'path-only' not in cookie_for('https://contents.premium.naver.com/other/channel')
    finally:
        client.close()
