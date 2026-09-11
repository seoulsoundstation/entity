"""Recognize explicit Naver error pages without searching authored post text."""

from __future__ import annotations

import re

from bs4 import BeautifulSoup


_ARTICLE_BODY_SELECTOR = 'div.se-main-container, #postViewArea, div.post_ct'
_UNAVAILABLE_MESSAGE = re.compile(
    r'비공개\s*(?:게시물|게시글|글)'
    r'|(?:삭제된|존재하지\s*않는|찾을\s*수\s*없는)\s*(?:게시물|게시글|포스트|글|페이지)'
    r'|(?:게시물|게시글|포스트|글|페이지).{0,40}(?:삭제되|존재하지\s*않|찾을\s*수\s*없)'
    r'|(?:서로\s*)?이웃.{0,40}(?:공개|볼\s*수|열람|확인)'
    r'|로그인.{0,24}(?:필요|해야|해주세요|해\s*주세요|후)'
)
_HIDDEN_STYLE = re.compile(r'(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*hidden)\b', re.I)


def page_unavailable_reason(html: str) -> str | None:
    """Return an explicit access/deletion message from Naver's mobile error page.

    A slow or changed page must still receive the normal article-body timeout.
    The observed Naver mobile error template has a dedicated heading and back
    button, unlike an article merely discussing private or deleted posts.
    Unknown error messages (including temporary service failures) are ignored.
    """
    soup = BeautifulSoup(html, 'html.parser')
    for node in soup.select('script, style, noscript, template'):
        node.decompose()
    if soup.select_one(_ARTICLE_BODY_SELECTOR) is not None:
        return None
    if soup.title is None or ' '.join(soup.title.get_text(' ', strip=True).split()) != '네이버 : 네이버 블로그':
        return None
    heading = soup.select_one('body > .error_wrap > .error > h1.error_h1')
    if heading is None or heading.parent.select_one('.btn_area #backBtn') is None:
        return None
    for node in (heading, *heading.parents):
        if (node.has_attr('hidden') or str(node.get('aria-hidden', '')).lower() == 'true'
                or _HIDDEN_STYLE.search(str(node.get('style', '')))):
            return None
    message = ' '.join(heading.get_text(' ', strip=True).split())
    if not _UNAVAILABLE_MESSAGE.search(message):
        return None
    return message[:240]
