from __future__ import annotations

from dataclasses import dataclass
import json
import re
import time
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import requests

from .config import Config
from .progress import TaskControl


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
            try:
                response = self.page.goto(post_url(blog, post), wait_until='domcontentloaded',
                                          timeout=self.config.timeout * 1000)
                self.check()
                if response is None or response.status >= 400:
                    raise RuntimeError(f'글 응답 HTTP {response.status if response else "없음"}')
                self.page.locator('div.se-main-container, #postViewArea, div.post_ct').first.wait_for(
                    state='attached', timeout=self.config.timeout * 1000)
                self.check()
                self.page.wait_for_function('''() => {
                    const body = document.querySelector('div.se-main-container, #postViewArea, div.post_ct');
                    return body && (body.innerText.trim() || body.querySelector('img, video, iframe, audio'));
                }''', timeout=self.config.timeout * 1000)
                self.check()
                return self.page.content()
            except Exception:
                if attempt + 1 == self.config.retries:
                    raise
                delay = max(self.config.delay, min(30, 2 ** attempt))
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
