from contextlib import nullcontext
from copy import deepcopy
import json
import os
from types import SimpleNamespace

import pytest
from playwright.sync_api import Error as BrowserError, TimeoutError as BrowserTimeout

from naver_blog_archive import premium_auth as auth
from naver_blog_archive.progress import OperationCancelled, TaskControl


TARGET = 'https://contents.premium.naver.com/owner/channel'


def cookie(name='NID_AUT', domain='.naver.com', **values):
    result = dict(name=name, value='TEST_ONLY_' + name, domain=domain, path='/',
                  expires=-1, httpOnly=True, secure=True, sameSite='Lax')
    result.update(values)
    return result


def session():
    return {'cookies': [cookie(), cookie('NID_SES')],
            'origins': [{'origin': 'https://contents.premium.naver.com',
                         'localStorage': [{'name': 'setting', 'value': 'TEST_ONLY_STORAGE'}]}]}


@pytest.fixture
def private_environment(monkeypatch):
    for name in ('DEBUGP', 'DEBUG', 'PWDEBUG', 'DEBUG_FILE'):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def session_file(tmp_path, monkeypatch):
    path = tmp_path / 'auth' / 'naver-session.bin'
    monkeypatch.setattr(auth, 'session_path', lambda: path)
    return path


def test_filter_keeps_only_naver_cookies_and_premium_local_storage():
    original = session()
    original['cookies'] += [cookie('analytics', '.example.test'),
                            cookie('other', '.naver.com.example.test'),
                            cookie('settings', 'nid.naver.com')]
    original['cookies'][0]['unexpected'] = 'excluded'
    original['origins'] += [{'origin': 'https://mail.naver.com', 'localStorage': [{'name': 'mail', 'value': 'private'}]},
                            {'origin': 'https://example.test', 'localStorage': [{'name': 'private', 'value': 'private'}]}]
    original['origins'][0]['indexedDB'] = [{'private': 'not needed'}]
    original['credentials'] = ['not needed']
    before = deepcopy(original)
    filtered = auth.filter_session_state(original)
    assert original == before
    assert [item['name'] for item in filtered['cookies']] == ['NID_AUT', 'NID_SES', 'settings']
    assert set(filtered) == {'cookies', 'origins'}
    assert 'unexpected' not in filtered['cookies'][0]
    assert filtered['origins'] == session()['origins']


@pytest.mark.parametrize('invalid', [
    cookie(domain='naver.com.evil.test'), cookie(domain='..naver.com'),
    cookie(domain='evilnaver.com'), cookie(domain='user@naver.com'),
    cookie(domain='https://naver.com'), cookie(domain='naver.com/path'),
    cookie(domain='x\n.naver.com'), cookie(expires=1), cookie(expires=float('nan')),
    cookie(expires='tomorrow'), cookie(path='relative'), cookie(path='/\r\n'),
    cookie(secure='true'), cookie(sameSite='unexpected'), cookie(name=''),
])
def test_invalid_cookies_cannot_be_stored_or_count_as_login(invalid):
    assert auth.filter_session_state({'cookies': [invalid]})['cookies'] == []
    assert not auth.has_login_cookies([invalid, cookie('NID_SES')])


def test_authentication_needs_both_nonempty_unexpired_cookies():
    assert auth.has_login_cookies(session())
    assert auth.has_login_cookies(session()['cookies'])
    assert not auth.has_login_cookies([cookie()])
    assert not auth.has_login_cookies([cookie(value=''), cookie('NID_SES')])
    assert not auth.has_login_cookies({'cookies': 'invalid'})


def test_local_storage_requires_exact_https_premium_origin():
    origins = [{'origin': value, 'localStorage': [{'name': 'n', 'value': 'v'}]} for value in (
        'http://contents.premium.naver.com', 'https://contents.premium.naver.com.evil.test',
        'https://user@contents.premium.naver.com', 'https://contents.premium.naver.com:444')]
    assert auth.filter_session_state({'origins': origins})['origins'] == []


@pytest.mark.skipif(os.name != 'nt', reason='DPAPI binds storage to a Windows user')
def test_dpapi_round_trip_never_writes_plaintext_session(session_file):
    auth.save_session(session())
    encrypted = session_file.read_bytes()
    assert encrypted.startswith(auth._MAGIC)
    assert b'TEST_ONLY_' not in encrypted and b'NID_AUT' not in encrypted
    assert auth.load_session() == session()
    assert not list(session_file.parent.glob('*.tmp'))


@pytest.mark.skipif(os.name != 'nt', reason='DPAPI binds storage to a Windows user')
def test_dpapi_ciphertext_tampering_fails_with_sanitized_error(session_file):
    auth.save_session(session())
    data = bytearray(session_file.read_bytes())
    data[-10] ^= 1
    session_file.write_bytes(data)
    with pytest.raises(auth.PremiumAuthError) as error:
        auth.load_session()
    assert 'TEST_ONLY_' not in str(error.value)
    assert 'Windows 계정' in str(error.value)


@pytest.mark.skipif(os.name != 'nt', reason='Windows session file operations')
def test_absent_session_load_and_forget_are_idempotent(session_file):
    assert auth.load_session() is None
    auth.forget_session()
    auth.forget_session()
    assert not session_file.exists()


@pytest.mark.skipif(os.name != 'nt', reason='Windows session file operations')
def test_forget_removes_only_session_file(session_file):
    auth.save_session(session())
    other = session_file.parent / 'other.bin'
    other.write_bytes(b'keep')
    auth.forget_session()
    assert not session_file.exists() and other.read_bytes() == b'keep'


@pytest.mark.skipif(os.name != 'nt', reason='Windows session file operations')
def test_invalid_new_session_does_not_replace_existing_login(session_file):
    auth.save_session(session())
    original = session_file.read_bytes()
    with pytest.raises(auth.PremiumAuthError):
        auth.save_session({'cookies': []})
    assert session_file.read_bytes() == original


@pytest.mark.skipif(os.name != 'nt', reason='Windows session file operations')
def test_failed_encryption_does_not_replace_existing_login(session_file, monkeypatch):
    auth.save_session(session())
    original = session_file.read_bytes()

    def broken(*args, **kwargs):
        raise RuntimeError('TEST_ONLY_SECRET_MUST_NOT_REACH_LOGS')

    monkeypatch.setattr(auth, '_crypt', broken)
    with pytest.raises(auth.PremiumAuthError) as error:
        auth.save_session(session())
    assert 'TEST_ONLY_' not in str(error.value)
    assert session_file.read_bytes() == original


@pytest.mark.skipif(os.name != 'nt', reason='Windows session file operations')
def test_plaintext_and_oversized_session_files_are_rejected(session_file):
    session_file.parent.mkdir()
    session_file.write_text(json.dumps(session()))
    with pytest.raises(auth.PremiumAuthError):
        auth.load_session()
    session_file.write_bytes(b'x' * (auth._MAX_BYTES + 1))
    with pytest.raises(auth.PremiumAuthError):
        auth.load_session()


@pytest.mark.skipif(os.name != 'nt', reason='Windows file locking')
def test_session_lock_prevents_overlapping_login_changes(session_file):
    with auth._session_lock():
        with pytest.raises(auth.PremiumAuthError, match='다른 창'):
            auth.forget_session()


def test_default_path_uses_local_app_data_and_not_repository(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, '_require_windows', lambda: None)
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    assert auth.session_path() == tmp_path / 'NaverBlogArchive' / 'naver-session.bin'


@pytest.mark.parametrize('key, value', [('DEBUGP', ''), ('DEBUG', 'pw:protocol'),
                                       ('PWDEBUG', '1'), ('DEBUG_FILE', 'trace.txt')])
def test_protocol_debug_settings_block_authenticated_browser(private_environment, monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    with pytest.raises(auth.PremiumAuthError, match='디버그 로그'):
        auth.ensure_private_browser()


class FakePage:
    def __init__(self):
        self.url = TARGET
        self.body = '채널 소개 · 구독하기'
        self.closed = False
        self.visits = []
        self.waits = 0
        self.on_wait = None
        self.goto_error = None
        self.body_reads = 0

    def goto(self, url, **kwargs):
        self.visits.append((url, kwargs))
        if self.goto_error:
            raise self.goto_error

    def is_closed(self):
        return self.closed

    def locator(self, selector):
        assert selector == 'body'
        return self

    def inner_text(self, **kwargs):
        self.body_reads += 1
        return self.body

    def wait_for_timeout(self, timeout):
        assert timeout == 500
        self.waits += 1
        if self.on_wait:
            self.on_wait()


class FakeContext:
    def __init__(self):
        self.page = FakePage()
        self.state = session()
        self.closed = False
        self.saved = 0

    def new_page(self):
        return self.page

    def cookies(self):
        return self.state['cookies']

    def storage_state(self, **kwargs):
        assert kwargs == {}, 'Never pass a plaintext storage_state path'
        self.saved += 1
        return self.state

    def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self):
        self.context = FakeContext()
        self.closed = False
        self.options = None

    def new_context(self, **kwargs):
        self.options = kwargs
        return self.context

    def is_connected(self):
        return not self.closed

    def close(self):
        self.closed = True


class FakePlaywright:
    def __init__(self):
        self.browser = FakeBrowser()
        self.chromium = self
        self.options = None
        self.exited = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.exited = True

    def launch(self, **kwargs):
        self.options = kwargs
        return self.browser


@pytest.fixture
def login_setup(monkeypatch, private_environment):
    playwright, saved, events = FakePlaywright(), [], []
    monkeypatch.setattr(auth, '_require_windows', lambda: None)
    monkeypatch.setattr(auth, '_session_lock', lambda: nullcontext())
    monkeypatch.setattr(auth, 'load_session', lambda: None)
    monkeypatch.setattr(auth, '_save_unlocked', lambda state: saved.append(deepcopy(state)))
    monkeypatch.setattr('playwright.sync_api.sync_playwright', lambda: playwright)
    config = SimpleNamespace(blog_id='premium/owner/channel', timeout=30)
    return config, playwright, saved, TaskControl(events.append), events


def test_login_uses_separate_visible_browser_and_saves_only_after_channel_returns(login_setup):
    config, playwright, saved, control, events = login_setup
    auth.login(config, control)
    assert playwright.options['headless'] is False
    assert playwright.browser.options == {'storage_state': None, 'accept_downloads': False}
    assert playwright.browser.context.page.visits[0][0] == (
        'https://nid.naver.com/nidlogin.login?url=https%3A%2F%2Fcontents.premium.naver.com%2Fowner%2Fchannel')
    assert saved == [session()]
    assert playwright.browser.context.closed and playwright.browser.closed and playwright.exited
    assert all('TEST_ONLY_' not in json.dumps(event) for event in events)
    assert '구독 글의 열람 권한은 백업할 때' in events[-1]['message']


def test_login_can_reuse_existing_encrypted_state(login_setup, monkeypatch):
    config, playwright, saved, control, events = login_setup
    monkeypatch.setattr(auth, 'load_session', session)
    auth.login(config, control)
    assert playwright.browser.options['storage_state'] == session()


def test_login_can_replace_unreadable_session_after_user_login(login_setup, monkeypatch):
    config, playwright, saved, control, events = login_setup

    def unreadable():
        raise auth.PremiumAuthError('old Windows account')

    monkeypatch.setattr(auth, 'load_session', unreadable)
    auth.login(config, control)
    assert playwright.browser.options['storage_state'] is None and saved == [session()]


@pytest.mark.parametrize('page_url', ['https://nid.naver.com/nidlogin.login',
                                     'https://contents.premium.naver.com.evil.test/owner/channel',
                                     'https://contents.premium.naver.com/owner/other',
                                     'http://contents.premium.naver.com/owner/channel'])
def test_login_does_not_save_cookies_without_return_to_expected_channel(login_setup, page_url):
    config, playwright, saved, control, events = login_setup
    page = playwright.browser.context.page
    page.url = page_url
    page.on_wait = control.cancel
    with pytest.raises(OperationCancelled):
        auth.login(config, control)
    assert saved == [] and page.body_reads == 0
    assert playwright.browser.closed and playwright.browser.context.closed


def test_login_waits_for_both_authentication_cookies(login_setup):
    config, playwright, saved, control, events = login_setup
    context = playwright.browser.context
    context.state['cookies'] = [cookie()]
    context.page.on_wait = lambda: context.state['cookies'].append(cookie('NID_SES'))
    auth.login(config, control)
    assert context.page.waits == 1 and len(saved) == 1


def test_channel_login_error_message_does_not_confirm_login(login_setup):
    config, playwright, saved, control, events = login_setup
    page = playwright.browser.context.page
    page.body = '로그인이 필요한 서비스입니다'
    page.on_wait = control.cancel
    with pytest.raises(OperationCancelled):
        auth.login(config, control)
    assert saved == []


def test_closing_login_window_cancels_without_replacing_session(login_setup):
    config, playwright, saved, control, events = login_setup
    playwright.browser.context.page.closed = True
    with pytest.raises(OperationCancelled, match='로그인 창을 닫았습니다'):
        auth.login(config, control)
    assert saved == [] and playwright.browser.closed and playwright.exited


def test_login_timeout_closes_owned_browser_and_keeps_old_session(login_setup, monkeypatch):
    config, playwright, saved, control, events = login_setup
    monkeypatch.setattr(auth, '_LOGIN_TIMEOUT', 0)
    with pytest.raises(auth.PremiumAuthError, match='대기 시간'):
        auth.login(config, control)
    assert saved == [] and playwright.browser.context.closed and playwright.browser.closed


def test_initial_navigation_timeout_can_still_complete_manual_login(login_setup):
    config, playwright, saved, control, events = login_setup
    playwright.browser.context.page.goto_error = BrowserTimeout('slow login page')
    auth.login(config, control)
    assert saved == [session()]


def test_browser_failure_is_sanitized_and_owned_resources_are_closed(login_setup):
    config, playwright, saved, control, events = login_setup
    playwright.browser.context.page.goto_error = BrowserError('TEST_ONLY_SECRET_MUST_NOT_REACH_LOGS')
    with pytest.raises(auth.PremiumAuthError) as error:
        auth.login(config, control)
    assert 'TEST_ONLY_' not in str(error.value)
    assert saved == [] and playwright.browser.context.closed and playwright.browser.closed


def test_login_cancel_before_start_does_not_open_browser(login_setup):
    config, playwright, saved, control, events = login_setup
    control.cancel()
    with pytest.raises(OperationCancelled):
        auth.login(config, control)
    assert playwright.options is None and saved == []


def test_login_refuses_protocol_debug_before_launch(login_setup, monkeypatch):
    config, playwright, saved, control, events = login_setup
    monkeypatch.setenv('DEBUGP', '')
    with pytest.raises(auth.PremiumAuthError, match='디버그 로그'):
        auth.login(config, control)
    assert playwright.options is None and saved == []
