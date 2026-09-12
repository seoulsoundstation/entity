from dataclasses import replace
import json

import pytest

from naver_blog_archive.addresses import channel_url, post_url
from naver_blog_archive.config import Config
from naver_blog_archive.network import Client, Listing, Renderer
from naver_blog_archive.premium import (
    PremiumAccessDenied, PremiumClient, PremiumLoginRequired, PremiumRenderer,
    extract_premium_post, login_state, parse_content_list,
)
from naver_blog_archive.progress import OperationCancelled, TaskControl


BLOG = 'premium/salarymoney/moneystock'
POST = '260911082659513hl'
SECOND = '260910220137443ad'


def login(value=True):
    return '<script>\nvar isLogin = ' + str(value).lower() + ';\n</script>'


def article(body='<p>공개된 테스트 본문</p>', *, auth='true', logged=True, extra=''):
    return (login(logged) if logged is not None else '') + f'''
      <div id="ct"><div id="_SE_VIEWER_CONTENT" data-cp-name="salarymoney" data-sub-id="moneystock"
       data-content-id="{POST}" data-content-auth="{auth}">
        <div class="se-title-text">테스트 글 제목</div>
        <div class="viewer_date"><span class="viewer_date_text">2026.09.11. 오후 2:30</span></div>
        <div class="viewer_author_wrap"><img src="https://images.example/author.png">프로필</div>
        <div class="se-main-container">{body}</div>{extra}
      </div></div>'''


def listing(ids=(POST,), *, cursor='', more=False, total=1, logged=None, owner='salarymoney'):
    prefix = login(logged) if logged is not None else ''
    count = (f'<a class="content_tab_link"><span class="content_tab_text">전체 콘텐츠<em>{total}</em></span></a>'
             if total is not None else '')
    items = ''.join(f'<li class="content_item"><a class="content_thumb" href="/{owner}/moneystock/contents/{post}">사진</a>'
                    f'<a class="content_text_link" href="/{owner}/moneystock/contents/{post}">'
                    f'<strong class="content_title">제목 {index}</strong></a></li>' for index, post in enumerate(ids))
    return prefix + count + f'''<ul class="_CONTENT_LIST" data-cp-name="{owner}" data-sub-id="moneystock"
      data-cursor-name="lastContentId" data-cursor="{cursor}" data-has-next="{str(more).lower()}">{items}</ul>'''


class Response:
    def __init__(self, text, url=''):
        self.text, self.url, self.closed = text, url, False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True


def configured(tmp_path):
    return Config(BLOG, tmp_path, delay=0, retries=1)


def attach_responses(client, pages):
    responses = [Response(text) for text in pages]
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return responses[len(calls) - 1]

    client.get = get
    return responses, calls


def test_login_flags_are_read_without_executing_javascript():
    assert login_state(login(True)) is True
    assert login_state(login(False)) is False
    assert login_state('<header><a class="user_link _LOGIN">로그인</a></header>') is False
    assert login_state('<p>로그인이 필요합니다.</p>') is None
    assert login_state('<a class="user_link _LOGIN">내 구독</a>') is None


@pytest.mark.parametrize('wrapper', ['<div id="ct">{}</div>', '<div id="_SE_VIEWER_CONTENT">{}</div>',
                                    '<div class="se-main-container">{}</div>'])
def test_authored_content_cannot_override_page_login(wrapper):
    assert login_state(login(False) + wrapper.format(login(True))) is False
    assert login_state(wrapper.format(login(True))) is None
    assert login_state(wrapper.format('<a class="user_link _LOGIN">로그인</a>')) is None


def test_template_scripts_and_conflicting_login_flags_are_unknown():
    assert login_state('<script type="text/data">var isLogin = true;</script>') is None
    assert login_state(login(True) + login(False)) is None


def test_full_authorized_post_keeps_title_body_date_and_images_without_page_chrome():
    result = extract_premium_post(article('<p>첫 문단</p><img data-src="https://images.example/body.png"><p>마지막 문단</p>'), BLOG, POST)
    assert result['title'] == '테스트 글 제목'
    assert result['blog_id'] == BLOG and result['post_id'] == POST
    assert result['published_at'] == '2026-09-11T14:30:00+09:00'
    assert [image['url'] for image in result['images']] == ['https://images.example/body.png']
    assert '프로필' not in result['markdown']
    assert result['markdown'].startswith('첫 문단') and result['markdown'].endswith('마지막 문단')


def test_authorized_viewer_is_sufficient_when_login_flag_is_absent():
    assert extract_premium_post(article(logged=None), BLOG, POST)['title'] == '테스트 글 제목'


@pytest.mark.parametrize('hour, expected', [('오전 12:05', '00:05'), ('오후 12:05', '12:05'), ('오전 9:05', '09:05')])
def test_korean_am_pm_dates(hour, expected):
    result = extract_premium_post(article().replace('오후 2:30', hour), BLOG, POST)
    assert result['published_at'] == f'2026-09-11T{expected}:00+09:00'


@pytest.mark.parametrize('bad_date', ['2026.13.11. 오후 2:30', '2026.09.11. 오전 99:30', '알 수 없는 날짜'])
def test_unrecognized_dates_do_not_prevent_other_content_from_being_saved(bad_date):
    result = extract_premium_post(article().replace('2026.09.11. 오후 2:30', bad_date), BLOG, POST)
    assert result['published_at'] is None


def test_expired_login_is_distinct_from_subscription_permission_denial():
    with pytest.raises(PremiumLoginRequired, match='로그인'):
        extract_premium_post(article(auth='false', logged=False), BLOG, POST)
    with pytest.raises(PremiumAccessDenied, match='열람 권한'):
        extract_premium_post(article(auth='false', logged=True), BLOG, POST)


@pytest.mark.parametrize('auth', ['false', '', 'unknown'])
def test_partial_preview_is_never_saved_as_a_complete_post(auth):
    with pytest.raises(PremiumAccessDenied):
        extract_premium_post(article('<p>많은 문단이 있어도 미리보기입니다.</p>' * 50, auth=auth), BLOG, POST)


def test_paywall_blocks_even_if_authorization_flag_is_inconsistent():
    with pytest.raises(PremiumAccessDenied):
        extract_premium_post(article(extra='<div class="viewer_paywall">구독 안내</div>'), BLOG, POST)


@pytest.mark.parametrize('before, after', [
    ('data-cp-name="salarymoney"', 'data-cp-name="other"'),
    ('data-sub-id="moneystock"', 'data-sub-id="other"'),
    (f'data-content-id="{POST}"', 'data-content-id="other"'),
])
def test_wrong_article_identity_is_rejected(before, after):
    with pytest.raises(ValueError, match='다른 본문'):
        extract_premium_post(article().replace(before, after), BLOG, POST)


def test_missing_article_body_is_rejected():
    with pytest.raises(ValueError, match='본문'):
        extract_premium_post(login(True) + '<h1>일시적인 오류</h1>', BLOG, POST)
    with pytest.raises(ValueError, match='본문'):
        extract_premium_post(article().replace('se-main-container', 'unknown'), BLOG, POST)


def test_existing_parser_preserves_file_components_and_premium_article_cards():
    body = ('<div class="se-module-file"><span class="se-file-name">보고서</span><span class="se-file-extension">.pdf</span>'
            '<a href="https://example.com/report.pdf" data-linktype="file">파일 다운로드</a></div>'
            f'<div class="se-oglink"><a class="se-oglink-info" href="{post_url(BLOG, SECOND)}">'
            '<strong class="se-oglink-title">연결된 글</strong></a></div>')
    result = extract_premium_post(article(body), BLOG, POST)
    assert result['files'][0]['name'] == '보고서.pdf'
    assert result['sources'][0]['target'] == (BLOG, SECOND)


def test_list_parsing_uses_only_real_channel_items_and_deduplicates_links():
    html = '<a href="/salarymoney/moneystock/contents/recommended">추천</a>' + listing([POST, SECOND, POST], total='1,000')
    assert parse_content_list(html, BLOG) == ([POST, SECOND], None, False, 1000)


def test_list_parser_exposes_cursor_and_more_state():
    assert parse_content_list(listing([POST], cursor=SECOND, more=True, total=2), BLOG) == ([POST], SECOND, True, 2)
    assert parse_content_list(listing([], total=0), BLOG) == ([], None, False, 0)


def test_observed_final_thirteen_posts_use_present_but_empty_cursor_flags():
    ids = ['250321091549465sd', '250319223929695hv', '250320081805489uz', '250319205823446ey',
           '250319133545213ge', '250319080833149oz', '250317210540319lv', '250317213353192vt',
           '250310164823413cs', '250315220240954ib', '250315143727121en', '250315075004788ap',
           '250309184905989ln']
    html = listing(ids, total=753).replace('data-has-next="false"', 'data-has-next=""')
    assert parse_content_list(html, BLOG) == (ids, None, False, 753)


@pytest.mark.parametrize('html', [
    listing(cursor=SECOND).replace('data-has-next="false"', 'data-has-next=""'),
    listing().replace('data-has-next="false"', 'data-has-next=""').replace('data-cursor=""', ''),
    listing().replace('data-has-next="false"', 'data-has-next=""').replace('data-cursor-name="lastContentId"', ''),
])
def test_incomplete_cursor_metadata_is_not_an_empty_final_page(html):
    with pytest.raises(ValueError, match='마지막 페이지'):
        parse_content_list(html, BLOG)


@pytest.mark.parametrize('html', [
    '<h1>에러</h1>', listing(owner='other'),
    listing().replace('data-has-next="false"', ''),
    listing(more=True), listing(more=True, cursor='../outside'),
    listing().replace('content_text_link', 'unknown'),
    listing().replace('/salarymoney/moneystock/contents/', 'https://example.com/'),
])
def test_malformed_or_wrong_channel_list_cannot_be_reported_complete(html):
    with pytest.raises(ValueError):
        parse_content_list(html, BLOG)


def test_listing_fetches_initial_and_observed_scroll_endpoint_until_end(tmp_path):
    client = PremiumClient(configured(tmp_path))
    response2 = json.dumps({'renderedComponent': {'SCS_PREMIUM_CONTENT_LIST': listing([SECOND], total=2)}})
    responses, calls = attach_responses(client, [listing([POST], cursor=SECOND, more=True, total=2, logged=True), response2])
    result = client.listing(BLOG)
    assert result == Listing([POST, SECOND], True, total=2)
    assert calls[0] == (channel_url(BLOG) + '/contents', {})
    assert calls[1][0] == 'https://contents.premium.naver.com/ch/template/SCS_PREMIUM_CONTENT_LIST'
    assert calls[1][1]['params'] == {'cpName': 'salarymoney', 'subId': 'moneystock', 'categoryId': '', 'tag': '',
                                     'authorId': '', 'allianceId': '', 'lastContentId': SECOND}
    assert all(response.closed for response in responses)
    client.close()


@pytest.mark.parametrize('total, complete', [(2, True), (3, False)])
def test_listing_final_empty_flags_still_require_complete_reported_total(tmp_path, total, complete):
    client = PremiumClient(configured(tmp_path))
    final = listing([SECOND], total=total).replace('data-has-next="false"', 'data-has-next=""')
    response2 = json.dumps({'renderedComponent': {'SCS_PREMIUM_CONTENT_LIST': final}})
    _, calls = attach_responses(client, [listing([POST], cursor=SECOND, more=True, total=total, logged=True), response2])
    result = client.listing(BLOG)
    assert result.ids == [POST, SECOND]
    assert result.complete is complete
    assert len(calls) == 2
    if not complete:
        assert '목록 누락' in result.error
    client.close()


def test_listing_expired_login_raises_once_instead_of_returning_all_public_locked_items(tmp_path):
    client = PremiumClient(configured(tmp_path))
    _, calls = attach_responses(client, [listing(logged=False)])
    with pytest.raises(PremiumLoginRequired):
        client.listing(BLOG)
    assert len(calls) == 1
    client.close()


def test_listing_unknown_login_is_incomplete_without_requesting_next_page(tmp_path):
    client = PremiumClient(configured(tmp_path))
    _, calls = attach_responses(client, [listing(more=True, cursor=SECOND)])
    result = client.listing(BLOG)
    assert not result.complete and result.ids == [] and '로그인 상태' in result.error
    assert len(calls) == 1
    client.close()


@pytest.mark.parametrize('second', [
    '{}', 'not json',
    json.dumps({'renderedComponent': {'SCS_PREMIUM_CONTENT_LIST': listing([POST], more=True, cursor=SECOND, total=2)}}),
    json.dumps({'renderedComponent': {'SCS_PREMIUM_CONTENT_LIST': listing([], total=2)}}),
])
def test_listing_errors_retain_prior_ids_and_report_incomplete(tmp_path, second):
    client = PremiumClient(configured(tmp_path))
    attach_responses(client, [listing([POST], cursor=SECOND, more=True, total=2, logged=True), second])
    result = client.listing(BLOG)
    assert result.ids == [POST] and not result.complete and result.error
    client.close()


def test_listing_page_limit_does_not_silently_claim_complete(tmp_path):
    client = PremiumClient(replace(configured(tmp_path), max_pages=1))
    attach_responses(client, [listing(more=True, cursor=SECOND, total=2, logged=True)])
    result = client.listing(BLOG)
    assert not result.complete and result.ids == [POST] and '상한' in result.error
    client.close()


def test_listing_cancellation_propagates(tmp_path):
    control = TaskControl()
    control.cancel()
    client = PremiumClient(configured(tmp_path), control=control)
    with pytest.raises(OperationCancelled):
        client.listing(BLOG)
    client.close()


def test_blog_listing_still_uses_existing_client(tmp_path, monkeypatch):
    monkeypatch.setattr(Client, 'listing', lambda self, blog: Listing(['123'], True, total=1))
    client = PremiumClient(configured(tmp_path))
    assert client.listing('demo').ids == ['123']
    client.close()


def test_premium_renderer_uses_authorized_ssr_request_without_launching_browser(tmp_path):
    client = PremiumClient(configured(tmp_path))
    responses, calls = attach_responses(client, [article()])
    renderer = PremiumRenderer(configured(tmp_path), client)
    assert renderer.render(BLOG, POST) == article()
    assert calls == [(post_url(BLOG, POST), {})]
    assert renderer.browser is None and renderer.playwright is None and responses[0].closed
    renderer.close()
    client.close()


@pytest.mark.parametrize('text,url', [
    (article(logged=False), ''), ('<form>로그인</form>', 'https://nid.naver.com/nidlogin.login'),
])
def test_renderer_login_expiry_stops_run(tmp_path, text, url):
    client = PremiumClient(configured(tmp_path))
    responses, _ = attach_responses(client, [text])
    responses[0].url = url
    renderer = PremiumRenderer(configured(tmp_path), client)
    with pytest.raises(PremiumLoginRequired):
        renderer.render(BLOG, POST)
    assert responses[0].closed
    renderer.close()
    client.close()


def test_renderer_does_not_accept_cross_site_redirect_content(tmp_path):
    client = PremiumClient(configured(tmp_path))
    responses, _ = attach_responses(client, [article()])
    responses[0].url = 'https://example.com/wrong'
    with pytest.raises(ValueError, match='다른 사이트'):
        PremiumRenderer(configured(tmp_path), client).render(BLOG, POST)
    client.close()


def test_renderer_keeps_normal_blog_path(tmp_path, monkeypatch):
    monkeypatch.setattr(Renderer, 'render', lambda self, blog, post: f'{blog}/{post}')
    client = PremiumClient(configured(tmp_path))
    assert PremiumRenderer(configured(tmp_path), client).render('demo', '123') == 'demo/123'
    client.close()
