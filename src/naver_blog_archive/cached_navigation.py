"""Remove a verified Naver page header from older cached article bodies."""
from __future__ import annotations

from copy import deepcopy
import json
import re
from urllib.parse import parse_qs, urljoin, urlsplit


_BREAK = r'\r?\n(?:[ \t]*\r?\n)+'
_HEADER = re.compile(
    r'\[[^\r\n]+\]\((?P<category>[^\s()]+)\)' + _BREAK
    + r'(?P<title>[^\r\n]+)' + _BREAK
    + r'(?P<profile>[A-Za-z0-9]+)' + _BREAK
    + r'\[\*\*[^\r\n]+\*\*\]\((?P<author>[^\s()]+)\)' + _BREAK
    + r'\d{4}\. \d{1,2}\. \d{1,2}\. \d{1,2}:\d{2}' + _BREAK
    + r'\[이웃추가\]\(#\)' + _BREAK
    + r'본문 기타 기능' + _BREAK,
)
_MENU = re.compile(
    r'\* \*\*본문 폰트 크기 조정\*\*본문 폰트 크기 작게 보기본문 폰트 크기 크게 보기가\r?\n'
    r'\* \[공유하기\]\(#\)\r?\n'
    r'\* \[URL복사\]\(#\)\r?\n'
    r'\* \[신고하기\]\(#\)' + _BREAK,
)


def _own_blog_navigation(url: str, blog: str, *, category: bool) -> bool:
    try:
        parts = urlsplit(urljoin('https://blog.naver.com/', url))
        if (parts.scheme not in ('http', 'https')
                or parts.hostname not in ('blog.naver.com', 'm.blog.naver.com')
                or parts.username or parts.password or parts.port is not None
                or parts.path != '/PostList.naver'):
            return False
        query = parse_qs(parts.query, keep_blank_values=True)
        if query.get('blogId') != [blog]:
            return False
        if category:
            return (len(query.get('categoryNo', [])) == 1
                    and query['categoryNo'][0].isdigit()
                    and parts.fragment == 'postlist_block')
        return set(query) == {'blogId'} and not parts.fragment
    except ValueError:
        return False


def _is_profile_image(image: dict) -> bool:
    if image.get('alt') != '프로필' or not isinstance(image.get('url'), str):
        return False
    try:
        parts = urlsplit(image['url'])
        return (parts.scheme in ('http', 'https')
                and parts.hostname == 'blogpfthumb-phinf.pstatic.net'
                and not parts.username and not parts.password and parts.port is None)
    except ValueError:
        return False


def clean_cached_navigation(document: dict) -> tuple[dict, bool]:
    """Return a copy with only an unambiguous old page header removed.

    Recognition requires the complete opening category/title/profile/author/
    date/control sequence, including navigation to this document's own blog.
    Body text is never filtered by keywords. Ambiguous caches and empty
    articles remain unchanged, and this helper performs no I/O.
    """
    result = deepcopy(document)
    markdown, title, blog = (result.get(key) for key in ('markdown', 'title', 'blog_id'))
    images, sources = result.get('images'), result.get('sources')
    if (not all(isinstance(value, str) and value for value in (markdown, title, blog))
            or not isinstance(images, list) or not isinstance(sources, list)):
        return result, False
    match = _HEADER.match(markdown)
    if (match is None or ' '.join(match['title'].split()) != ' '.join(title.split())
            or not _own_blog_navigation(match['category'], blog, category=True)
            or not _own_blog_navigation(match['author'], blog, category=False)):
        return result, False
    profiles = [image for image in images if isinstance(image, dict)
                and image.get('token') == match['profile']]
    if len(profiles) != 1 or not _is_profile_image(profiles[0]):
        return result, False
    remaining = markdown[match.end():]
    if menu := _MENU.match(remaining):
        remaining = remaining[menu.end():]
    elif remaining.startswith('* **본문 폰트 크기 조정**'):
        # A partial or changed control menu is not enough evidence to remove
        # it or to guess where the article begins.
        return result, False
    if not remaining.strip(' \t\r\n\u200b\ufeff'):
        return result, False
    result['markdown'] = remaining
    token = match['profile']
    if token not in remaining and token not in json.dumps(sources, ensure_ascii=False):
        result['images'] = [image for image in images if image is not profiles[0]]
    return result, True
