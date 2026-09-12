from io import BytesIO

import pytest
import requests
from requests.adapters import BaseAdapter

from naver_blog_archive.config import Config
from naver_blog_archive.network import Client, apply_premium_cookies


HOST = 'contents.premium.naver.com'
PREMIUM = 'https://' + HOST


def storage(path='/'):
    return {'cookies': [dict(name=name, value='TEST_ONLY_' + name, domain='.naver.com',
                              path=path, secure=True, expires=-1)
                        for name in ('NID_AUT', 'NID_SES')]}


class RecordingAdapter(BaseAdapter):
    def __init__(self, routes=None):
        self.routes = routes or {}
        self.sent = []
        self.closed = False

    def send(self, request, **kwargs):
        self.sent.append((request.copy(), kwargs))
        status, location = self.routes.get(request.url, (200, None))
        response = requests.Response()
        response.status_code = status
        response.url = request.url
        response.request = request
        response._content = b'test response'
        response._content_consumed = True
        response.raw = BytesIO(response._content)
        if location:
            response.headers['Location'] = location
        return response

    def close(self):
        self.closed = True


@pytest.fixture
def client(tmp_path):
    instance = Client(Config('premium/owner/channel', tmp_path, delay=0, retries=1))
    try:
        yield instance
    finally:
        instance.close()


@pytest.mark.parametrize('url, permitted', [
    (PREMIUM + '/owner/channel', True),
    ('https://' + HOST + ':443/owner/channel', True),
    ('http://' + HOST + '/owner/channel', False),
    ('https://child.' + HOST + '/owner/channel', False),
    ('https://' + HOST + '.example.test/owner/channel', False),
    ('https://nid.naver.com/login', False),
    ('https://download.blog.naver.com/report.pdf', False),
    ('https://example.test/image.png', False),
    ('https://' + HOST + ':444/owner/channel', False),
    ('https://user:pass@' + HOST + '/owner/channel', False),
    ('https://' + HOST + './owner/channel', False),
])
def test_prepared_cookie_header_is_limited_to_exact_https_origin(client, url, permitted):
    apply_premium_cookies(client, storage())
    prepared = client.session.prepare_request(requests.Request('GET', url))
    assert ('Cookie' in prepared.headers) is permitted
    if permitted:
        assert 'TEST_ONLY_NID_AUT' in prepared.headers['Cookie']
        assert 'TEST_ONLY_NID_SES' in prepared.headers['Cookie']


def test_browser_cookie_paths_remain_narrowed(client):
    apply_premium_cookies(client, storage(path='/members'))
    allowed = client.session.prepare_request(requests.Request('GET', PREMIUM + '/members/article'))
    other = client.session.prepare_request(requests.Request('GET', PREMIUM + '/owner/channel'))
    assert 'NID_AUT' in allowed.headers['Cookie']
    assert 'Cookie' not in other.headers


def test_cookie_scope_does_not_copy_unrelated_browser_domains(client):
    browser_state = storage()
    browser_state['cookies'] += [dict(name='unrelated', value='private', domain='.example.test', path='/'),
                                  dict(name='mail', value='private', domain='mail.naver.com', path='/')]
    apply_premium_cookies(client, browser_state)
    prepared = client.session.prepare_request(requests.Request('GET', PREMIUM))
    assert 'unrelated' not in prepared.headers['Cookie'] and 'mail' not in prepared.headers['Cookie']


@pytest.mark.parametrize('url', ['https://child.' + HOST + '/file', 'http://' + HOST + '/file',
                                'https://example.test/file', 'https://' + HOST + ':444/file'])
def test_direct_send_of_caller_prepared_request_strips_explicit_cookie_headers(client, url):
    adapter = RecordingAdapter()
    client.session.mount('https://', adapter)
    client.session.mount('http://', adapter)
    apply_premium_cookies(client, storage())
    prepared = requests.Request('GET', url, headers={
        'Cookie': 'NID_AUT=TEST_ONLY_MANUAL', 'Cookie2': 'NID_SES=TEST_ONLY_MANUAL',
        'X-Keep': 'other header',
    }).prepare()
    client.session.send(prepared)
    sent = adapter.sent[0][0]
    assert 'Cookie' not in sent.headers and 'Cookie2' not in sent.headers
    assert sent.headers['X-Keep'] == 'other header'


@pytest.mark.parametrize('redirect', [
    'https://child.' + HOST + '/elsewhere',
    'https://example.test/elsewhere',
    'http://' + HOST + '/elsewhere',
    'https://nid.naver.com/login',
    'https://' + HOST + ':444/elsewhere',
])
def test_actual_redirect_send_does_not_forward_authentication_cookies(client, redirect):
    first = PREMIUM + '/owner/channel'
    adapter = RecordingAdapter({first: (302, redirect)})
    client.session.mount('https://', adapter)
    client.session.mount('http://', adapter)
    apply_premium_cookies(client, storage())
    with client.get(first) as response:
        assert response.status_code == 200 and response.url == redirect
    assert len(adapter.sent) == 2
    assert 'TEST_ONLY_NID_AUT' in adapter.sent[0][0].headers['Cookie']
    assert 'Cookie' not in adapter.sent[1][0].headers


def test_same_origin_redirect_keeps_cookie_and_returns_from_external_host_safely(client):
    first, second, external, final = (PREMIUM + '/start', PREMIUM + '/next',
                                      'https://child.' + HOST + '/bounce', PREMIUM + '/finish')
    adapter = RecordingAdapter({first: (302, second), second: (302, external), external: (302, final)})
    client.session.mount('https://', adapter)
    apply_premium_cookies(client, storage())
    with client.get(first) as response:
        assert response.url == final
    assert [request.url for request, _ in adapter.sent] == [first, second, external, final]
    assert ['Cookie' in request.headers for request, _ in adapter.sent] == [True, True, False, True]


def test_alternate_host_header_cannot_send_premium_cookies(client):
    adapter = RecordingAdapter()
    client.session.mount('https://', adapter)
    apply_premium_cookies(client, storage())
    prepared = client.session.prepare_request(requests.Request('GET', PREMIUM, headers={'Host': 'example.test'}))
    assert 'Cookie' not in prepared.headers
    prepared.headers['Cookie'] = 'NID_AUT=TEST_ONLY_MANUAL'
    client.session.send(prepared)
    assert 'Cookie' not in adapter.sent[0][0].headers


def test_installing_scope_preserves_session_configuration_and_transport(client):
    original = client.session
    adapter = RecordingAdapter()
    original.mount('https://', adapter)
    original.headers['X-Existing'] = 'keep'
    original.proxies = {'https': 'http://proxy.example.test:8080'}
    original.verify = False
    original.cert = ('client.crt', 'client.key')
    original.trust_env = False
    original.params = {'page': '1'}
    original.max_redirects = 7
    seen = []
    original.hooks['response'].append(lambda response, **kwargs: seen.append(response.status_code))
    apply_premium_cookies(client, storage())
    assert original.cookies.get('NID_AUT') is None
    assert client.session.headers['X-Existing'] == 'keep'
    assert client.session.adapters['https://'] is adapter
    assert client.session.proxies == original.proxies
    assert client.session.verify is False and client.session.cert == original.cert
    assert client.session.trust_env is False and client.session.max_redirects == 7
    with client.get(PREMIUM + '/article') as response:
        assert response.url.endswith('/article?page=1')
    assert seen == [200]
    assert adapter.sent[0][1]['verify'] is False
    assert adapter.sent[0][1]['cert'] == original.cert


def test_applying_session_twice_keeps_same_scope_and_adapter(client):
    adapter = RecordingAdapter()
    client.session.mount('https://', adapter)
    apply_premium_cookies(client, storage())
    scoped = client.session
    apply_premium_cookies(client, storage())
    assert client.session is scoped and client.session.adapters['https://'] is adapter
    assert len(client.session.cookies) == 2


def test_unconnected_public_client_keeps_normal_cookie_behavior(client):
    client.session.cookies.set('public', 'ordinary', domain='example.test', path='/')
    prepared = client.session.prepare_request(requests.Request('GET', 'https://example.test/article'))
    assert prepared.headers['Cookie'] == 'public=ordinary'
