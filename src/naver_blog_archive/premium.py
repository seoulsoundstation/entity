"""Read Premium Contents through its ordinary, authorized web responses."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import escape
import json
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
import requests

from .addresses import PREMIUM_HOST, channel_url, is_premium, parse_post_url, post_url
from .network import Client, Listing, Renderer


_TEMPLATE = 'SCS_PREMIUM_CONTENT_LIST'
_CURSOR = re.compile(r'[A-Za-z0-9_-]{1,80}')
_LOGIN = re.compile(r'^\s*(?:var|let|const)\s+isLogin\s*=\s*(true|false)\s*;', re.MULTILINE)


class PremiumLoginRequired(RuntimeError):
    """The login expired; the caller should stop the run instead of every post."""


class PremiumAccessDenied(ValueError):
    """The current account cannot read this complete article."""


def _outside_content(element) -> bool:
    return not any(parent.get('id') in ('ct', 'ct_wrap', '_SE_VIEWER_CONTENT', '_VIEWER_VIDEO_CONTENT')
                   or 'se-main-container' in parent.get('class', []) for parent in element.parents)


def login_state(html: str) -> bool | None:
    """Read the server's login flag, never text or code authored in an article."""
    soup = BeautifulSoup(html, 'html.parser')
    flags = set()
    for script in soup.find_all('script'):
        if script.get('src') or not _outside_content(script):
            continue
        if script.get('type', '').lower() not in ('', 'text/javascript', 'application/javascript'):
            continue
        flags.update(value == 'true' for value in _LOGIN.findall(script.string or ''))
    if len(flags) == 1:
        return flags.pop()
    if len(flags) > 1:
        return None
    for anchor in soup.select('a.user_link._LOGIN'):
        if _outside_content(anchor) and anchor.get_text(' ', strip=True) == '로그인':
            return False
    return None


def _login_error() -> PremiumLoginRequired:
    return PremiumLoginRequired('네이버 로그인이 필요하거나 만료되었습니다. 네이버 로그인 연결 후 다시 시작하세요.')


def _published_at(soup) -> str | None:
    element = next((item for item in soup.select('.viewer_date_text, .viewer_video_meta_text')
                    if re.match(r'\d{4}\.\d{1,2}\.\d{1,2}\.', item.get_text(' ', strip=True))), None)
    if element is None:
        return None
    text = ' '.join(element.get_text(' ', strip=True).split())
    match = re.fullmatch(r'(\d{4})\.(\d{1,2})\.(\d{1,2})\.\s*(오전|오후)\s*(\d{1,2}):(\d{2})', text)
    if match is None:
        return None
    year, month, day, period, hour, minute = match.groups()
    hour = int(hour)
    if not 1 <= hour <= 12:
        return None
    hour = hour % 12 + (12 if period == '오후' else 0)
    try:
        return datetime(int(year), int(month), int(day), hour, int(minute),
                        tzinfo=timezone(timedelta(hours=9))).isoformat()
    except ValueError:
        return None


def extract_premium_post(html: str, blog: str, post: str) -> dict:
    """Extract only when the page positively grants full article access."""
    from .parser import extract_post

    if not is_premium(blog):
        raise ValueError('프리미엄콘텐츠 채널 키가 아닙니다.')
    if login_state(html) is False:
        raise _login_error()
    soup = BeautifulSoup(html, 'html.parser')
    viewer = soup.select_one('#_SE_VIEWER_CONTENT')
    video = viewer is None
    if video:
        viewer = soup.select_one('._VOD_PLAYER_WRAP[data-type="VIDEO"]')
    if viewer is None:
        raise ValueError('프리미엄콘텐츠 본문 영역을 찾지 못했습니다.')
    _, owner, channel = blog.split('/')
    if (viewer.get('data-cp-name') != owner or viewer.get('data-sub-id') != channel
            or viewer.get('data-content-id') != post):
        raise ValueError('요청한 채널·글과 다른 본문이 반환되었습니다.')
    if viewer.get('data-content-auth') != 'true' or viewer.select_one('.viewer_paywall') is not None:
        raise PremiumAccessDenied('이 글의 전체 열람 권한이 없습니다. 구독 상태를 확인하세요. 미리보기는 저장하지 않습니다.')
    if video:
        if viewer.get('data-is-preview') == 'true' or viewer.select_one('._VIEWER_VIDEO_PLAYER_PAYWALL') is not None:
            raise PremiumAccessDenied('이 영상의 전체 열람 권한이 없습니다. 미리보기는 저장하지 않습니다.')
        details = soup.select_one('#_VIEWER_VIDEO_CONTENT')
        if details is None or details.select_one('.viewer_paywall') is not None:
            raise ValueError('열람 가능한 영상 설명 영역을 찾지 못했습니다.')
        video_id = viewer.get('data-video-id', '')
        if not re.fullmatch(r'[A-Za-z0-9_-]{8,128}', video_id):
            raise ValueError('영상 식별자를 확인할 수 없습니다.')
        title = details.select_one('.viewer_video_title')
        meta = soup.select_one('meta[property="og:title"]')
        title_text = (title.get_text(' ', strip=True) if title else
                      meta.get('content', '') if meta else '') or post
        description = details.select_one('.viewer_video_desc')
        body = (str(description) if description is not None and (description.get_text(strip=True) or description.find('img'))
                else '<p>영상 설명이 없습니다.</p>')
        document = extract_post('<div class="se-title-text">' + escape(title_text)
                                + '</div><div class="se-main-container">' + body + '</div>', blog, post)
        from .parser import md_link
        document['markdown'] += '\n\n' + md_link('영상 원문 열기', post_url(blog, post))
        document.update(content_type='video', video_id=video_id, published_at=_published_at(details))
        return document
    if viewer.select_one('.se-main-container') is None:
        raise ValueError('열람 가능한 프리미엄콘텐츠 본문을 찾지 못했습니다.')
    document = extract_post(str(viewer), blog, post)
    published = _published_at(viewer)
    if published:
        document['published_at'] = published
    return document


def parse_content_list(html: str, blog: str) -> tuple[list[str], str | None, bool, int | None]:
    """Parse the real list, excluding recommendations, notices, and other channels."""
    if not is_premium(blog):
        raise ValueError('프리미엄콘텐츠 채널 키가 아닙니다.')
    soup = BeautifulSoup(html, 'html.parser')
    lists = soup.select('ul._CONTENT_LIST')
    if len(lists) != 1:
        raise ValueError('프리미엄콘텐츠 글 목록 구조를 확인할 수 없습니다.')
    listing = lists[0]
    _, owner, channel = blog.split('/')
    if listing.get('data-cp-name') != owner or listing.get('data-sub-id') != channel:
        raise ValueError('요청한 채널과 다른 글 목록이 반환되었습니다.')
    next_flag = listing.get('data-has-next')
    # The real final template uses two present-but-empty attributes instead
    # of a literal false (observed on the channel's final 13 of 753 posts).
    # Missing attributes or an empty flag with a live cursor are not proof
    # of completion; the client also checks the collected total at the end.
    empty_end = (next_flag == '' and listing.get('data-cursor') == ''
                 and listing.get('data-cursor-name') == 'lastContentId')
    if next_flag not in ('true', 'false') and not empty_end:
        raise ValueError('글 목록의 마지막 페이지 여부를 확인할 수 없습니다.')
    more = next_flag == 'true'
    cursor = listing.get('data-cursor') or None
    if more and (listing.get('data-cursor-name') != 'lastContentId'
                 or cursor is None or not _CURSOR.fullmatch(cursor)):
        raise ValueError('다음 글 목록 주소를 확인할 수 없습니다.')
    ids = []
    for item in listing.select('li.content_item'):
        anchor = item.select_one('a.content_text_link[href]')
        if anchor is None:
            raise ValueError('글 주소가 없는 프리미엄콘텐츠 목록 항목입니다.')
        key = parse_post_url(urljoin(channel_url(blog), anchor['href']))
        if not key or key[0] != blog:
            raise ValueError('글 목록에 다른 채널 또는 잘못된 주소가 포함되어 있습니다.')
        if key[1] not in ids:
            ids.append(key[1])
    total = None
    count = soup.select_one('.content_tab_link .content_tab_text > em')
    if count is not None:
        text = count.get_text('', strip=True).replace(',', '')
        if not text.isdecimal():
            raise ValueError('프리미엄콘텐츠 전체 글 수를 확인할 수 없습니다.')
        total = int(text)
    return ids, cursor, more, total


class PremiumClient(Client):
    """List through the same public template request used by normal scrolling."""

    def listing(self, blog: str) -> Listing:
        if not is_premium(blog):
            return super().listing(blog)
        _, owner, channel = blog.split('/')
        ids, seen, cursors = [], set(), set()
        cursor, total = None, None
        for page in range(1, self.config.max_pages + 1):
            self.check()
            if self.control:
                self.control.emit('listing', f'글 목록 {page}페이지 확인 중', page=page,
                                  listed=len(ids), total=total)
            try:
                if page == 1:
                    with self.get(channel_url(blog) + '/contents') as response:
                        html = response.text
                    state = login_state(html)
                    if state is False:
                        raise _login_error()
                    if state is not True:
                        raise ValueError('네이버 로그인 상태를 확인할 수 없습니다. 연결 상태를 확인하세요.')
                else:
                    params = {'cpName': owner, 'subId': channel, 'categoryId': '', 'tag': '',
                              'authorId': '', 'allianceId': '', 'lastContentId': cursor}
                    with self.get(f'https://{PREMIUM_HOST}/ch/template/{_TEMPLATE}', params=params) as response:
                        raw = json.loads(response.text)
                    if not isinstance(raw, dict) or not isinstance(raw.get('renderedComponent'), dict):
                        raise ValueError('프리미엄콘텐츠 다음 목록 응답 형식이 다릅니다.')
                    html = raw['renderedComponent'].get(_TEMPLATE)
                    if not isinstance(html, str):
                        raise ValueError('프리미엄콘텐츠 다음 목록을 찾지 못했습니다.')
                    if login_state(html) is False:
                        raise _login_error()
                self.check()
                page_ids, next_cursor, more, page_total = parse_content_list(html, blog)
                if page_total is not None:
                    total = page_total
                added = 0
                for post in page_ids:
                    if post not in seen:
                        ids.append(post)
                        seen.add(post)
                        added += 1
                if not more:
                    if total is not None and len(ids) < total:
                        raise ValueError(f'목록 누락: 수집 {len(ids)} / 응답 전체 {total}')
                    return Listing(ids, True, total=total)
                if not added or next_cursor in cursors:
                    raise ValueError('같은 글 목록 또는 다음 페이지가 반복되어 수집을 중단했습니다.')
                cursors.add(next_cursor)
                cursor = next_cursor
            except (requests.RequestException, ValueError, TypeError, AttributeError) as exc:
                return Listing(ids, False, str(exc), total)
        return Listing(ids, False, '목록 수집 페이지 상한에 도달했습니다.', total)


class PremiumRenderer(Renderer):
    def __init__(self, config, client, control=None):
        super().__init__(config, control=control)
        self.client = client

    def render(self, blog: str, post: str) -> str:
        if not is_premium(blog):
            return super().render(blog, post)
        self.check()
        if self.control:
            self.control.emit('render', f'원문 접속: {post_url(blog, post)}', blog_id=blog, post_id=post)
        with self.client.get(post_url(blog, post)) as response:
            self.check()
            html = response.text
            host = urlsplit(getattr(response, 'url', '') or post_url(blog, post)).hostname
            if host == 'nid.naver.com' or login_state(html) is False:
                raise _login_error()
            if host != PREMIUM_HOST:
                raise ValueError('프리미엄콘텐츠와 다른 사이트로 이동하여 본문을 저장하지 않았습니다.')
        return html
