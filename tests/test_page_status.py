from __future__ import annotations

import pytest

from naver_blog_archive.page_status import page_unavailable_reason


def error_page(message: str = '비공개 게시물입니다.') -> str:
    # The structure shared by two independently fetched private Naver posts.
    return f'''<!doctype html><html><head><title>네이버 : 네이버 블로그</title></head>
    <body><div class="error_wrap"><div class="error">
    <i class="error_icon" aria-label="!"></i><h1 class="error_h1">{message}</h1>
    <div class="btn_area"><button id="backBtn">이전 화면으로</button></div>
    </div></div><script>var backUrl = 'https://m.blog.naver.com/blog?fromClosedPost=true';</script>
    </body></html>'''


@pytest.mark.parametrize('message', [
    '비공개 게시물입니다.',
    '삭제된 게시물입니다.',
    '존재하지 않는 게시물입니다.',
    '게시글이 삭제되었습니다.',
    '요청하신 페이지를 찾을 수 없습니다.',
    '서로이웃에게만 공개된 게시물입니다.',
    '이웃에게만 공개된 글입니다.',
    '로그인이 필요합니다.',
    '로그인 후 확인해 주세요.',
])
def test_explicit_mobile_error_page_returns_visible_reason(message):
    assert page_unavailable_reason(error_page(message)) == message


def test_message_whitespace_and_html_entities_are_normalized():
    assert page_unavailable_reason(error_page(' 비공개&nbsp;\n 게시물입니다. ')) == '비공개 게시물입니다.'


@pytest.mark.parametrize('body', [
    '<div class="se-main-container"><p>비공개 게시물입니다.</p></div>',
    '<div id="postViewArea"><p>이웃에게만 공개된 게시물입니다.</p></div>',
    '<div class="post_ct"><p>로그인이 필요합니다.</p></div>',
])
def test_real_article_takes_precedence_even_with_error_template(body):
    html = error_page().replace('<body>', '<body>' + body)
    assert page_unavailable_reason(html) is None


@pytest.mark.parametrize('html', [
    '<html><body><p>비공개 게시물입니다.</p></body></html>',
    '<html><body><h1>서로이웃에게만 공개된 글입니다.</h1></body></html>',
    '<script>alert("비공개 게시물입니다.");</script>',
    '<script>location.href = "/MobileErrorView.naver?errorType=noPost";</script>',
    '',
])
def test_keywords_and_script_redirects_alone_are_not_conclusive(html):
    assert page_unavailable_reason(html) is None


@pytest.mark.parametrize('message', [
    '일시적인 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.',
    '서비스 점검 중입니다.',
    '이웃 추천을 불러오지 못했습니다.',
    '본문을 불러오고 있습니다.',
    '오류가 발생했습니다.<script>var message = "비공개 게시물입니다.";</script>',
])
def test_unknown_or_temporary_error_does_not_become_permanent_failure(message):
    assert page_unavailable_reason(error_page(message)) is None


@pytest.mark.parametrize('attribute', ['hidden', 'aria-hidden="true"', 'style="display: none"',
                                       'style="color: red; visibility: hidden"'])
def test_hidden_error_template_is_not_an_active_error(attribute):
    html = error_page().replace('class="error_wrap"', f'class="error_wrap" {attribute}')
    assert page_unavailable_reason(html) is None


@pytest.mark.parametrize('before, after', [
    ('<title>네이버 : 네이버 블로그</title>', '<title>글 제목 : 네이버 블로그</title>'),
    ('id="backBtn"', 'id="somethingElse"'),
    ('class="error_h1"', 'class="post_heading"'),
    ('<body>', '<body><article>'),
])
def test_error_structure_must_match_observed_mobile_template(before, after):
    assert page_unavailable_reason(error_page().replace(before, after)) is None


def test_message_is_bounded():
    assert len(page_unavailable_reason(error_page('비공개 게시물입니다. ' + '안내 ' * 200))) == 240
