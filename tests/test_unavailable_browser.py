import os
from threading import Timer
import time

import pytest

from naver_blog_archive.config import Config
from naver_blog_archive.network import PostUnavailableError, Renderer
from naver_blog_archive.parser import extract_post
from naver_blog_archive.progress import OperationCancelled, TaskControl


pytestmark = pytest.mark.skipif(os.environ.get('NBA_BROWSER_TEST') != '1',
                                reason='Set NBA_BROWSER_TEST=1 with Chromium installed')

PRIVATE = '''<html><head><title>네이버 : 네이버 블로그</title></head><body>
<div class="error_wrap"><div class="error"><h1 class="error_h1">비공개 게시물입니다.</h1>
<div class="btn_area"><button id="backBtn">이전 화면으로</button></div></div></div></body></html>'''
HEALTHY = '<div class="se-title-text">다음 글</div><div class="se-main-container">정상 본문</div>'


def browser_renderer(tmp_path, control, *, timeout=3):
    from playwright.sync_api import sync_playwright

    renderer = Renderer(Config('demo', tmp_path, timeout=timeout, retries=3, delay=0), control)
    renderer.playwright = sync_playwright().start()
    renderer.browser = renderer.playwright.chromium.launch(headless=True)
    renderer.page = renderer.browser.new_page()
    return renderer


@pytest.mark.parametrize('mode', ['private', 'dynamic_private', '404'])
def test_unavailable_page_finishes_once_and_same_browser_can_read_next_post(tmp_path, mode):
    events, requests = [], []
    renderer = browser_renderer(tmp_path, TaskControl(events.append))

    def respond(route):
        requests.append(route.request.url)
        if route.request.url.endswith('/2'):
            route.fulfill(status=200, content_type='text/html; charset=utf-8', body=HEALTHY)
        elif mode == '404':
            route.fulfill(status=404, content_type='text/html; charset=utf-8', body='Not found')
        else:
            page = PRIVATE
            if mode == 'dynamic_private':
                page = '''<html><head><title>네이버 : 네이버 블로그</title></head><body>
                  <script>setTimeout(() => { document.body.innerHTML =
                  '<div class="error_wrap"><div class="error"><h1 class="error_h1">비공개 게시물입니다.</h1><div class="btn_area"><button id="backBtn">이전 화면으로</button></div></div></div>';
                  }, 100);</script></body></html>'''
            route.fulfill(status=200, content_type='text/html; charset=utf-8', body=page)

    renderer.page.route('**/*', respond)
    try:
        started = time.monotonic()
        with pytest.raises(PostUnavailableError, match='HTTP 404' if mode == '404' else '비공개'):
            renderer.render('demo', '1')
        assert time.monotonic() - started < renderer.config.timeout
        assert len(requests) == 1
        assert not any(event['phase'] == 'render_retry' for event in events)
        assert extract_post(renderer.render('demo', '2'), 'demo', '2')['markdown'] == '정상 본문'
        assert len(requests) == 2
    finally:
        renderer.close()


def test_stop_interrupts_actual_browser_body_wait_without_waiting_for_timeout(tmp_path):
    events = []
    control = TaskControl(events.append)
    renderer = browser_renderer(tmp_path, control, timeout=30)
    renderer.page.route('**/*', lambda route: route.fulfill(
        status=200, content_type='text/html', body='<div class="se-main-container"></div>'))
    timer = Timer(.2, control.cancel)
    try:
        started = time.monotonic()
        timer.start()
        with pytest.raises(OperationCancelled):
            renderer.render('demo', '1')
        assert time.monotonic() - started < 2
        assert not any(event['phase'] == 'render_retry' for event in events)
    finally:
        timer.cancel()
        timer.join()
        renderer.close()
