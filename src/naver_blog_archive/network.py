from __future__ import annotations

from dataclasses import dataclass
import json
import re
import time
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import requests

from .config import Config
from .page_status import page_unavailable_reason
from .progress import TaskControl


ARTICLE_BODY_SELECTORS = ('div.se-main-container', '#postViewArea', 'div.post_ct')
# These are Naver's page controls, not text or URL patterns: a post may itself
# discuss adding neighbors or intentionally link to a category.
BLOG_CHROME_SELECTOR = ', '.join((
    '.se-documentTitle', '.blog_category', '.blog_authorArea', '.blog_btnArea',
    '.post_function_t1', 'a.btn_buddyadd', 'a._add_buddy',
))
NON_CONTENT_SELECTOR = 'script, style, noscript, ' + BLOG_CHROME_SELECTOR


def original_image_url(url: str) -> str:
    if url.startswith('//'):
        url = 'https:' + url
    parts = urlsplit(url)
    # Preserve raw signed queries unless a thumbnail parameter actually exists.
    if not any(k == 'type' for k, _ in parse_qsl(parts.query)):
        return url
    query = '&'.join(p for p in parts.query.split('&') if p.split('=', 1)[0] != 'type')
    return urlunsplit(parts._replace(query=query))


def parse_post_url(url: str) -> tuple[str, str] | None:
    if url.startswith('//'):
        url = 'https:' + url
    parts = urlsplit(url)
    if parts.scheme not in ('http', 'https') or parts.hostname not in ('blog.naver.com', 'm.blog.naver.com'):
        return None
    if parts.path.lower() == '/postview.naver':
        query = dict(parse_qsl(parts.query))
        blog, post = query.get('blogId', ''), query.get('logNo', '')
    else:
        match = re.fullmatch(r'/([\w-]+)/(\d+)/?', parts.path)
        if not match:
            return None
        blog, post = match.groups()
    if re.fullmatch(r'[A-Za-z0-9_-]+', blog) and re.fullmatch(r'\d+', post):
        return blog, post
    return None


def post_url(blog: str, post: str) -> str:
    return f'https://m.blog.naver.com/{blog}/{post}'


@dataclass
class Listing:
    ids: list[str]
    complete: bool
    error: str = ''
    total: int | None = None


class Client:
    def __init__(self, config: Config, control: TaskControl | None = None):
        self.config = config
        self.control = control
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0 Safari/537.36',
                                     'Referer': f'https://m.blog.naver.com/{config.blog_id}'})
        self.last_request = 0.0

    def close(self):
        self.session.close()

    def check(self):
        if self.control:
            self.control.check()

    def wait(self, seconds):
        if self.control:
            self.control.wait(seconds)
        else:
            time.sleep(seconds)

    def get(self, url: str, **kwargs):
        if urlsplit(url).scheme not in ('http', 'https'):
            raise ValueError('HTTP(S) URL만 다운로드할 수 있습니다.')
        for attempt in range(self.config.retries):
            self.check()
            self.wait(max(0, self.config.delay - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            try:
                response = self.session.get(url, timeout=self.config.timeout, **kwargs)
                try:
                    self.check()
                except KeyboardInterrupt:
                    response.close()
                    raise
                if response.status_code in (408, 429) or response.status_code >= 500:
                    retry_after = response.headers.get('Retry-After', '')
                    response.close()
                    if attempt + 1 < self.config.retries:
                        self.wait(min(60, float(retry_after)) if retry_after.isdigit() else min(30, 2 ** attempt))
                        continue
                try:
                    response.raise_for_status()
                except requests.RequestException:
                    response.close()
                    raise
                return response
            except (requests.Timeout, requests.ConnectionError):
                if attempt + 1 == self.config.retries:
                    raise
                self.wait(min(30, 2 ** attempt))
        raise RuntimeError('요청 재시도 횟수를 초과했습니다.')

    def resolve_source(self, url: str) -> str:
        if urlsplit(url).hostname != 'naver.me':
            return url
        with self.get(url, stream=True, allow_redirects=True) as response:
            return response.url

    def listing(self, blog: str) -> Listing:
        ids, seen, total = [], set(), None
        for page in range(1, self.config.max_pages + 1):
            self.check()
            if self.control:
                self.control.emit('listing', f'글 목록 {page}페이지 확인 중', page=page, listed=len(ids), total=total)
            try:
                with self.get(f'https://m.blog.naver.com/api/blogs/{blog}/post-list',
                              params={'categoryNo': 0, 'itemCount': 30, 'page': page}) as response:
                    raw = json.loads(re.sub(r"^[)\]}',]*\s*", '', response.text.strip()))
                result = raw.get('result', raw)
                if not isinstance(result, dict) or not any(k in result for k in ('items', 'postList')):
                    raise ValueError('지원하지 않는 글 목록 응답 구조입니다.')
                items = result.get('items') if 'items' in result else result['postList']
                if not isinstance(items, list):
                    raise ValueError('글 목록이 배열이 아닙니다.')
                if result.get('totalCount') is not None:
                    reported_total = int(result['totalCount'])
                    if reported_total < 0:
                        raise ValueError('잘못된 전체 글 수입니다.')
                    # The live mobile API can report zero while returning real posts.
                    total = reported_total if reported_total > 0 else (None if items or ids else 0)
                if not items:
                    if total is not None and len(ids) < total:
                        raise ValueError(f'목록 누락: 수집 {len(ids)} / 응답 전체 {total}')
                    return Listing(ids, True, total=total)
                added = 0
                for item in items:
                    value = item.get('logNo') or item.get('logNumber')
                    if value is None or not str(value).isdigit():
                        raise ValueError('글 번호가 없는 목록 항목입니다.')
                    value = str(value)
                    if value not in seen:
                        seen.add(value)
                        ids.append(value)
                        added += 1
                if not added:
                    raise ValueError('같은 목록 페이지가 반복되어 수집을 중단했습니다.')
                # Continue to an empty page; a stale total must not truncate the archive.
            except (requests.RequestException, ValueError, TypeError, AttributeError) as exc:
                return Listing(ids, False, str(exc), total)
        return Listing(ids, False, '목록 수집 페이지 상한에 도달했습니다.', total)


class PostUnavailableError(RuntimeError):
    """The page explicitly cannot be read; retrying this request will not help."""


class Renderer:
    def __init__(self, config: Config, control: TaskControl | None = None):
        self.config = config
        self.control = control
        self.playwright = None
        self.browser = None
        self.page = None

    def check(self):
        if self.control:
            self.control.check()

    def _emit(self, phase, message, blog, post, attempt, **fields):
        print(message)
        if self.control:
            self.control.emit(phase, message, blog_id=blog, post_id=post,
                              attempt=attempt, retries=self.config.retries, **fields)

    def _wait_for_body(self, blog, post, attempt, started, deadline):
        from playwright.sync_api import TimeoutError as BrowserTimeout

        last_notice = started
        while True:
            self.check()
            reason = page_unavailable_reason(self.page.content())
            if reason:
                raise PostUnavailableError(reason)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BrowserTimeout(f'본문을 {self.config.timeout}초 안에 불러오지 못했습니다.')
            try:
                # Short waits keep cancellation and progress updates responsive
                # while a page is still loading. All slices share one deadline.
                self.page.wait_for_function('''({selectors, excluded}) => {
                    const body = selectors.map(selector => document.querySelector(selector)).find(Boolean);
                    if (!body) return false;
                    const content = body.cloneNode(true);
                    content.querySelectorAll(excluded).forEach(element => element.remove());
                    return !!(content.textContent.trim() || content.querySelector('img, video, iframe, audio'));
                }''', arg={'selectors': ARTICLE_BODY_SELECTORS, 'excluded': NON_CONTENT_SELECTOR},
                    timeout=max(1, min(500, remaining * 1000)))
                self.check()
                return self.page.content()
            except BrowserTimeout:
                self.check()
                now = time.monotonic()
                if now - last_notice >= 5:
                    elapsed = int(now - started)
                    self._emit('render_wait',
                               f'본문 로딩 대기: {blog}/{post} · {elapsed}초 경과 '
                               f'(시도 {attempt}/{self.config.retries}, 제한 {self.config.timeout}초)',
                               blog, post, attempt, elapsed=elapsed)
                    last_notice = now

    def render(self, blog: str, post: str) -> str:
        self.check()
        from playwright.sync_api import sync_playwright
        if self.playwright is None:
            self.playwright = sync_playwright().start()
        if self.browser is None:
            self.browser = self.playwright.chromium.launch(headless=True)
        if self.page is None:
            self.page = self.browser.new_page()
        for attempt in range(self.config.retries):
            self.check()
            self._emit('render', f'원문 접속: {blog}/{post} '
                       f'(시도 {attempt + 1}/{self.config.retries}, 제한 {self.config.timeout}초)',
                       blog, post, attempt + 1)
            self.check()
            started = time.monotonic()
            deadline = started + self.config.timeout
            try:
                response = self.page.goto(post_url(blog, post), wait_until='domcontentloaded',
                                          timeout=self.config.timeout * 1000)
                self.check()
                if response is not None and response.status in (404, 410):
                    raise PostUnavailableError(f'글을 제공하지 않는 주소입니다. HTTP {response.status}')
                if response is None or response.status >= 400:
                    raise RuntimeError(f'글 응답 HTTP {response.status if response else "없음"}')
                return self._wait_for_body(blog, post, attempt + 1, started, deadline)
            except PostUnavailableError:
                raise
            except Exception as exc:
                self.check()
                if attempt + 1 == self.config.retries:
                    raise
                delay = max(self.config.delay, min(30, 2 ** attempt))
                detail = str(exc).splitlines()[0][:180] or type(exc).__name__
                self._emit('render_retry', f'원문 재시도 대기: {blog}/{post} · {detail} · '
                           f'{delay:g}초 후 시도 {attempt + 2}/{self.config.retries}',
                           blog, post, attempt + 1, next_attempt=attempt + 2, delay=delay)
                if self.control:
                    self.control.wait(delay)
                else:
                    time.sleep(delay)
        raise RuntimeError('본문 렌더링 실패')

    def close(self):
        try:
            if self.browser:
                self.browser.close()
        finally:
            if self.playwright:
                self.playwright.stop()
