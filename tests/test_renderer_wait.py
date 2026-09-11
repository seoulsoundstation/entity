"""Renderer retry, waiting and cancellation behavior without a real browser."""
from types import SimpleNamespace

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from naver_blog_archive.config import Config
from naver_blog_archive import network
from naver_blog_archive.progress import OperationCancelled, TaskControl


ARTICLE = '<html><div class="se-main-container"><p>Saved article</p></div></html>'
PRIVATE_PAGE = '''<html><head><title>네이버 : 네이버 블로그</title></head><body>
<div class="error_wrap"><div class="error"><h1 class="error_h1">비공개 게시물입니다.</h1>
<div class="btn_area"><button id="backBtn">이전 화면으로</button></div></div></div>
</body></html>'''


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def monotonic(self):
        return self.now

    def advance(self, seconds):
        self.now += max(0, seconds)


class FakePage:
    """Time advances only during navigation and an observable body wait."""

    def __init__(self, clock, *, html=ARTICLE, responses=(200,), goto_seconds=0,
                 body_after=0, private_after=None, on_wait=None):
        self.clock = clock
        self.html = html
        self.responses = list(responses)
        self.goto_seconds = goto_seconds
        self.body_at = None if body_after is None else clock.now + body_after
        self.private_at = None if private_after is None else clock.now + private_after
        self.on_wait = on_wait
        self.navigations = []
        self.waits = []

    def goto(self, url, **kwargs):
        self.navigations.append(url)
        self.clock.advance(self.goto_seconds)
        response = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(response, Exception):
            raise response
        return None if response is None else SimpleNamespace(status=response)

    def content(self):
        if self.private_at is not None and self.clock.now >= self.private_at:
            return PRIVATE_PAGE
        return self.html

    def wait_for_function(self, expression, *, arg, timeout):
        self.waits.append(timeout)
        if self.on_wait is not None:
            self.on_wait()
        seconds = timeout / 1000
        if self.body_at is not None and self.body_at <= self.clock.now + seconds:
            self.clock.advance(self.body_at - self.clock.now)
            return True
        self.clock.advance(seconds)
        raise PlaywrightTimeoutError('Body is still loading')


@pytest.fixture
def renderer_case(monkeypatch, tmp_path):
    clock = FakeClock()
    events = []
    control = TaskControl(events.append)

    def wait(seconds):
        control.check()
        clock.advance(seconds)
        control.check()

    monkeypatch.setattr(network.time, 'monotonic', clock.monotonic)
    monkeypatch.setattr(network.time, 'sleep', clock.advance)
    monkeypatch.setattr(control, 'wait', wait)

    def create(*, timeout=30, retries=3, **page_options):
        page = FakePage(clock, **page_options)
        renderer = network.Renderer(Config('demo', tmp_path, timeout=timeout,
                                           retries=retries, delay=0), control)
        # A prepared renderer must reuse its existing browser and page.
        renderer.playwright = object()
        renderer.browser = object()
        renderer.page = page
        return SimpleNamespace(renderer=renderer, page=page, clock=clock,
                               control=control, events=events)

    return create


def phases(case, phase):
    return [event for event in case.events if event['phase'] == phase]


def test_private_page_reports_reason_without_waiting_or_retrying(renderer_case):
    case = renderer_case(html=PRIVATE_PAGE, body_after=None)

    with pytest.raises(network.PostUnavailableError, match='비공개 게시물'):
        case.renderer.render('jindori7', '222306042990')

    assert len(case.page.navigations) == 1
    assert case.page.waits == []
    assert not phases(case, 'render_retry')
    assert case.clock.now == 100


@pytest.mark.parametrize('status', [404, 410])
def test_missing_page_http_status_does_not_retry(renderer_case, status):
    case = renderer_case(responses=(status,), body_after=None)

    with pytest.raises(network.PostUnavailableError, match=str(status)):
        case.renderer.render('demo', '123')

    assert len(case.page.navigations) == 1
    assert case.page.waits == []
    assert not phases(case, 'render_retry')


def test_transient_navigation_timeout_retries_and_announces_attempts(renderer_case):
    case = renderer_case(responses=(PlaywrightTimeoutError('Navigation timeout'), 200))

    assert case.renderer.render('demo', '123') == ARTICLE

    assert len(case.page.navigations) == 2
    assert [event['attempt'] for event in phases(case, 'render')] == [1, 2]
    assert len(phases(case, 'render_retry')) == 1
    for event in case.events:
        assert event['blog_id'] == 'demo'
        assert event['post_id'] == '123'
        assert event['retries'] == 3
        assert event['message']


def test_cancel_during_body_wait_exits_promptly_without_retry(renderer_case):
    case = renderer_case(body_after=None)
    case.page.on_wait = case.control.cancel

    with pytest.raises(OperationCancelled):
        case.renderer.render('demo', '123')

    assert len(case.page.navigations) == 1
    assert case.clock.now <= 100.5
    assert not phases(case, 'render_retry')


def test_private_notice_loaded_after_navigation_is_detected_promptly(renderer_case):
    case = renderer_case(html='<html><body></body></html>', body_after=None,
                         private_after=0.5)

    with pytest.raises(network.PostUnavailableError, match='비공개 게시물'):
        case.renderer.render('demo', '123')

    assert len(case.page.navigations) == 1
    assert case.clock.now <= 101
    assert not phases(case, 'render_retry')


def test_slow_body_emits_periodic_status_then_succeeds(renderer_case):
    case = renderer_case(body_after=11.2, timeout=20)

    assert case.renderer.render('demo', '123') == ARTICLE

    waiting = phases(case, 'render_wait')
    assert len(waiting) == 2
    assert len(case.page.navigations) == 1
    assert all(event['attempt'] == 1 for event in waiting)
    assert all(event['blog_id'] == 'demo' and event['post_id'] == '123' for event in waiting)
    assert not phases(case, 'render_retry')


def test_navigation_and_body_share_one_attempt_deadline(renderer_case):
    case = renderer_case(goto_seconds=2, body_after=None, timeout=3, retries=1)

    with pytest.raises(PlaywrightTimeoutError):
        case.renderer.render('demo', '123')

    assert len(case.page.navigations) == 1
    assert case.clock.now == pytest.approx(103)
    assert not phases(case, 'render_retry')


def test_transient_http_error_remains_retryable(renderer_case):
    case = renderer_case(responses=(503, 200))

    assert case.renderer.render('demo', '123') == ARTICLE

    assert len(case.page.navigations) == 2
    assert len(phases(case, 'render_retry')) == 1


def test_cancel_before_render_never_navigates(renderer_case):
    case = renderer_case()
    case.control.cancel()

    with pytest.raises(OperationCancelled):
        case.renderer.render('demo', '123')

    assert case.page.navigations == []
    assert not phases(case, 'render_retry')
