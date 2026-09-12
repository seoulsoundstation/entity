"""User-driven Naver login with a Windows user-bound encrypted session."""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
import json
import math
import os
from pathlib import Path
import time
from urllib.parse import quote, urlsplit

from .files import atomic_write
from .progress import OperationCancelled, TaskControl


_MAGIC = b'NBA-DPAPI\x01'
_MAX_BYTES = 4 * 1024 * 1024
_PREMIUM_ORIGIN = 'https://contents.premium.naver.com'
_LOGIN_TIMEOUT = 10 * 60


class PremiumAuthError(RuntimeError):
    """An authentication error safe to show without browser or session data."""


def _require_windows() -> None:
    if os.name != 'nt':
        raise PremiumAuthError('네이버 로그인 저장은 Windows에서만 지원합니다.')


def session_path() -> Path:
    _require_windows()
    local = os.environ.get('LOCALAPPDATA', '')
    if not local:
        raise PremiumAuthError('Windows 사용자 저장 폴더를 찾지 못했습니다.')
    return Path(local) / 'NaverBlogArchive' / 'naver-session.bin'


def ensure_private_browser() -> None:
    # Playwright's Python transport checks DEBUGP on every message, and its
    # driver inherits DEBUG before browser.launch(env=...) is even called.
    # Do not alter the application's global environment from a worker thread.
    if ('DEBUGP' in os.environ or any(os.environ.get(key) for key in
                                     ('DEBUG', 'PWDEBUG', 'DEBUG_FILE'))):
        raise PremiumAuthError('브라우저 디버그 로그 설정을 끈 뒤 네이버 로그인을 다시 실행하세요.')


def _naver_domain(value) -> bool:
    if not isinstance(value, str) or not value or value.startswith('..'):
        return False
    domain = value.removeprefix('.').lower()
    return (domain == 'naver.com' or domain.endswith('.naver.com')) and all(
        part and all(character.isascii() and (character.isalnum() or character == '-')
                     for character in part) for part in domain.split('.'))


def _filtered_cookie(cookie) -> dict | None:
    if not isinstance(cookie, dict) or not _naver_domain(cookie.get('domain')):
        return None
    name, value, path = cookie.get('name'), cookie.get('value'), cookie.get('path', '/')
    if (not isinstance(name, str) or not name or not isinstance(value, str)
            or not isinstance(path, str) or not path.startswith('/')
            or any(ord(character) < 32 or ord(character) == 127 for character in name + path)):
        return None
    expires = cookie.get('expires', -1)
    if (type(expires) not in (int, float) or not math.isfinite(expires)
            or (expires != -1 and expires <= time.time())):
        return None
    same_site = cookie.get('sameSite', 'Lax')
    if same_site not in ('Strict', 'Lax', 'None'):
        return None
    if any(type(cookie.get(field, False)) is not bool for field in ('secure', 'httpOnly')):
        return None
    return {'name': name, 'value': value, 'domain': cookie['domain'].lower(),
            'path': path, 'expires': expires, 'httpOnly': cookie.get('httpOnly', False),
            'secure': cookie.get('secure', False), 'sameSite': same_site}


def filter_session_state(state: dict) -> dict:
    """Retain Naver cookies and Premium local storage, never other sites."""
    if not isinstance(state, dict):
        raise PremiumAuthError('저장된 로그인 정보의 형식을 확인할 수 없습니다. 다시 로그인하세요.')
    cookies, origins = [], []
    for item in state.get('cookies', []) if isinstance(state.get('cookies', []), list) else []:
        cookie = _filtered_cookie(item)
        if cookie is not None:
            cookies.append(cookie)
    for item in state.get('origins', []) if isinstance(state.get('origins', []), list) else []:
        if not isinstance(item, dict) or item.get('origin') != _PREMIUM_ORIGIN:
            continue
        entries = item.get('localStorage', [])
        if not isinstance(entries, list):
            continue
        values = [{'name': entry['name'], 'value': entry['value']} for entry in entries
                  if isinstance(entry, dict) and isinstance(entry.get('name'), str)
                  and isinstance(entry.get('value'), str)]
        origins.append({'origin': _PREMIUM_ORIGIN, 'localStorage': values})
    return {'cookies': cookies, 'origins': origins}


def has_login_cookies(state: dict | list) -> bool:
    cookies = state.get('cookies', []) if isinstance(state, dict) else state
    if not isinstance(cookies, list):
        return False
    names = {cookie['name'] for item in cookies
             if (cookie := _filtered_cookie(item)) is not None and cookie['value']}
    return {'NID_AUT', 'NID_SES'} <= names


def _crypt(data: bytes, *, decrypt: bool) -> bytes:
    _require_windows()
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_ubyte))]

    crypt32 = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    function = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p if decrypt else wintypes.LPCWSTR,
                         ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    buffer = ctypes.create_string_buffer(data, len(data))
    incoming = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    outgoing = Blob()
    try:
        # UI_FORBIDDEN only: never LOCAL_MACHINE, which would allow other
        # Windows accounts on this computer to decrypt the session.
        if not function(ctypes.byref(incoming), None if decrypt else 'Naver Blog Archive session',
                        None, None, None, 1, ctypes.byref(outgoing)):
            raise OSError(ctypes.get_last_error())
        return ctypes.string_at(outgoing.pbData, outgoing.cbData)
    finally:
        ctypes.memset(buffer, 0, len(data))
        if outgoing.pbData:
            ctypes.memset(outgoing.pbData, 0, outgoing.cbData)
            kernel32.LocalFree(outgoing.pbData)


@contextmanager
def _session_lock():
    _require_windows()
    import msvcrt

    path = session_path().with_suffix('.lock')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        if stream.tell() == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise PremiumAuthError('다른 창에서 네이버 로그인을 변경하고 있습니다. 완료 후 다시 시도하세요.') from None
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def load_session() -> dict | None:
    _require_windows()
    try:
        path = session_path()
        if not path.exists():
            return None
        if path.stat().st_size > _MAX_BYTES:
            raise ValueError('size')
        encrypted = path.read_bytes()
        if not encrypted.startswith(_MAGIC):
            raise ValueError('format')
        payload = json.loads(_crypt(encrypted[len(_MAGIC):], decrypt=True))
        if not isinstance(payload, dict) or payload.get('version') != 1:
            raise ValueError('version')
        return filter_session_state(payload.get('state'))
    except PremiumAuthError:
        raise
    except Exception:
        raise PremiumAuthError('로그인 정보를 읽지 못했습니다. 현재 Windows 계정에서 다시 로그인하세요.') from None


def _save_unlocked(state: dict) -> None:
    filtered = filter_session_state(state)
    if not has_login_cookies(filtered):
        raise PremiumAuthError('네이버 로그인이 확인되지 않았습니다. 로그인 완료 후 다시 시도하세요.')
    payload = json.dumps({'version': 1, 'state': filtered}, ensure_ascii=False).encode('utf-8')
    if len(payload) > _MAX_BYTES - 4096:
        raise PremiumAuthError('로그인 정보가 너무 큽니다. 전용 로그인 창에서 다시 로그인하세요.')
    atomic_write(session_path(), _MAGIC + _crypt(payload, decrypt=False))


def save_session(state: dict) -> None:
    _require_windows()
    try:
        with _session_lock():
            _save_unlocked(state)
    except PremiumAuthError:
        raise
    except Exception:
        raise PremiumAuthError('로그인 정보를 안전하게 저장하지 못했습니다. Windows 사용자 폴더를 확인하세요.') from None


def forget_session() -> None:
    _require_windows()
    try:
        with _session_lock():
            session_path().unlink(missing_ok=True)
    except PremiumAuthError:
        raise
    except Exception:
        raise PremiumAuthError('저장된 로그인 정보를 지우지 못했습니다. 다른 작업이 끝난 뒤 다시 시도하세요.') from None


def _at_channel(url: str, target: str) -> bool:
    try:
        current, expected = urlsplit(url), urlsplit(target)
        base = expected.path.rstrip('/')
        return (current.scheme == 'https' and current.hostname == 'contents.premium.naver.com'
                and current.port in (None, 443) and current.username is None
                and current.password is None
                and (current.path.rstrip('/') == base or current.path.startswith(base + '/')))
    except ValueError:
        return False


def _channel_ready(page, target: str) -> bool:
    if not _at_channel(page.url, target):
        return False
    # Never read the credential form. The channel check only detects a login
    # or access error page; subscription entitlement belongs to post parsing.
    body = page.locator('body').inner_text(timeout=1000).strip()
    if not body:
        return False
    errors = ('로그인이 필요한 서비스입니다', '로그인 후 이용해 주세요',
              '접근이 제한되었습니다', '접근이 제한된 페이지', '잘못된 접근입니다',
              '요청하신 페이지를 찾을 수 없습니다')
    return not any(message in body for message in errors)


def login(config, control: TaskControl | None = None) -> None:
    """Own a separate visible browser on this thread; the user types login."""
    _require_windows()
    from playwright.sync_api import Error as BrowserError, TimeoutError as BrowserTimeout, sync_playwright
    from .addresses import channel_url

    control = control or TaskControl()
    control.check()
    ensure_private_browser()
    target = channel_url(config.blog_id)
    if not _at_channel(target, target):
        raise PremiumAuthError('프리미엄콘텐츠 채널 주소를 확인하세요.')
    browser = context = None
    try:
        with _session_lock(), sync_playwright() as playwright:
            try:
                try:
                    state = load_session()
                except PremiumAuthError:
                    # A new manual login can replace an expired or unreadable
                    # session, but cancellation always keeps the old file.
                    state = None
                environment = {key: value for key, value in os.environ.items()
                               if key.upper() not in ('DEBUG', 'PWDEBUG', 'DEBUG_FILE')}
                browser = playwright.chromium.launch(headless=False, env=environment)
                context = browser.new_context(storage_state=state, accept_downloads=False)
                page = context.new_page()
                control.emit('login', '열린 네이버 창에서 직접 로그인해 주세요. 완료되면 자동으로 저장합니다.')
                deadline = time.monotonic() + _LOGIN_TIMEOUT
                try:
                    page.goto('https://nid.naver.com/nidlogin.login?url=' + quote(target, safe=''),
                              wait_until='domcontentloaded', timeout=min(config.timeout, 5) * 1000)
                except BrowserTimeout:
                    pass
                while time.monotonic() < deadline:
                    control.check()
                    if not browser.is_connected() or page.is_closed():
                        raise OperationCancelled('로그인 창을 닫았습니다. 기존 로그인 정보는 유지했습니다.')
                    cookies = context.cookies()
                    if has_login_cookies(cookies):
                        try:
                            if _channel_ready(page, target):
                                control.check()
                                _save_unlocked(context.storage_state())
                                control.emit('login', '네이버 로그인을 저장했습니다. 구독 글의 열람 권한은 백업할 때 확인합니다.')
                                return
                        except BrowserTimeout:
                            pass
                    page.wait_for_timeout(500)
                raise PremiumAuthError('로그인 대기 시간이 끝났습니다. 로그인 버튼을 눌러 다시 시도하세요.')
            finally:
                if context is not None:
                    try:
                        context.close()
                    except Exception:
                        pass
                if browser is not None:
                    try:
                        browser.close()
                    except Exception:
                        pass
    except OperationCancelled:
        raise
    except PremiumAuthError:
        raise
    except BrowserError:
        control.check()
        raise PremiumAuthError('네이버 로그인 창이 종료되었거나 연결되지 않았습니다. 다시 로그인하세요.') from None
    except Exception:
        control.check()
        raise PremiumAuthError('네이버 로그인 정보를 저장하지 못했습니다. 전용 로그인 창에서 다시 시도하세요.') from None
