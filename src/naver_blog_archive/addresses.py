"""Stable archive keys and URLs for blogs and Premium Contents channels."""
from __future__ import annotations

import re
from urllib.parse import parse_qs, unquote, urlsplit


_NAME = r'[A-Za-z0-9_-]+'
_PREMIUM_KEY = re.compile(rf'premium/({_NAME})/({_NAME})')
_POST = re.compile(r'[A-Za-z0-9_-]{1,80}')
PREMIUM_HOST = 'contents.premium.naver.com'


def is_premium(blog: str) -> bool:
    return isinstance(blog, str) and _PREMIUM_KEY.fullmatch(blog) is not None


def valid_source_key(value: object) -> bool:
    return isinstance(value, str) and (re.fullmatch(_NAME, value) is not None or is_premium(value))


def _url_parts(value: str):
    parts = urlsplit(value)
    if (parts.scheme not in ('http', 'https') or parts.username is not None
            or parts.password is not None or parts.port is not None):
        raise ValueError('지원하지 않는 네이버 주소입니다.')
    return parts


def normalize_blog_id(value: str) -> str:
    """Keep the legacy API while accepting an unambiguous premium channel key."""
    message = '네이버 블로그 ID 또는 블로그·프리미엄콘텐츠 채널 주소를 입력하세요.'
    if not isinstance(value, str):
        raise ValueError(message)
    value = value.strip()
    if valid_source_key(value):
        return value
    if any(character.isspace() or ord(character) < 32 for character in value):
        raise ValueError(message)
    if '://' not in value:
        value = 'https://' + value
    try:
        parts = _url_parts(value)
        segments = [unquote(part) for part in parts.path.strip('/').split('/')]
        if parts.hostname == PREMIUM_HOST:
            if (len(segments) not in (2, 3, 4) or any(not re.fullmatch(_NAME, part) for part in segments[:2])
                    or (len(segments) > 2 and segments[2] != 'contents')
                    or (len(segments) == 4 and not _POST.fullmatch(segments[3]))):
                raise ValueError(message)
            return 'premium/' + '/'.join(segments[:2])
        if parts.hostname not in ('blog.naver.com', 'm.blog.naver.com'):
            raise ValueError(message)
        if len(segments) == 1 and segments[0].lower() in ('postview.naver', 'postlist.naver'):
            identifiers = parse_qs(parts.query, keep_blank_values=True).get('blogId', [])
            blog = identifiers[0] if len(identifiers) == 1 else ''
        elif len(segments) == 1 or (len(segments) == 2 and segments[1].isdigit()):
            blog = segments[0]
        else:
            blog = ''
        if not re.fullmatch(_NAME, blog):
            raise ValueError(message)
        return blog
    except ValueError as exc:
        raise ValueError(message) from exc


def channel_url(blog: str) -> str:
    if is_premium(blog):
        return 'https://' + PREMIUM_HOST + '/' + blog.removeprefix('premium/')
    return f'https://m.blog.naver.com/{blog}'


def post_url(blog: str, post: str) -> str:
    return channel_url(blog) + ('/contents/' if is_premium(blog) else '/') + post


def canonical_post_url(blog: str, post: str) -> str:
    # Existing blog UUIDs use the desktop URL; never change that seed.
    return post_url(blog, post) if is_premium(blog) else f'https://blog.naver.com/{blog}/{post}'


def parse_post_url(url: str) -> tuple[str, str] | None:
    if url.startswith('//'):
        url = 'https:' + url
    try:
        parts = _url_parts(url)
        if parts.hostname == PREMIUM_HOST:
            segments = parts.path.strip('/').split('/')
            if (len(segments) == 4 and segments[2] == 'contents'
                    and all(re.fullmatch(_NAME, part) for part in segments[:2])
                    and _POST.fullmatch(segments[3])):
                return 'premium/' + '/'.join(segments[:2]), segments[3]
            return None
        if parts.hostname not in ('blog.naver.com', 'm.blog.naver.com'):
            return None
        if parts.path.lower() == '/postview.naver':
            query = parse_qs(parts.query)
            blog, post = query.get('blogId', [''])[0], query.get('logNo', [''])[0]
        else:
            match = re.fullmatch(r'/([\w-]+)/(\d+)/?', parts.path)
            if not match:
                return None
            blog, post = match.groups()
        if re.fullmatch(_NAME, blog) and re.fullmatch(r'\d+', post):
            return blog, post
    except (ValueError, TypeError):
        return None
    return None
