"""Recover unambiguous link previews from previously extracted documents."""
from __future__ import annotations

from copy import deepcopy
import re
import string
from urllib.parse import urlsplit
from uuid import uuid4

from .network import parse_post_url


_ESCAPE = re.compile(r'\\([' + re.escape(string.punctuation) + r'])')
_PARAGRAPH = r'\r?\n[ \t]*\r?\n'


def _plain_text(value: str) -> str | None:
    # A cached card is text inside one outer Markdown link. Do not guess how
    # nested links, HTML, code, or additional formatting were originally read.
    unescaped = _ESCAPE.sub('', value)
    if re.search(r'[\[\]`<>*_~]', unescaped):
        return None
    return re.sub(r'\s+', ' ', _ESCAPE.sub(r'\1', value)).strip()


def _same_domain(domain: str, url: str) -> bool:
    try:
        parts = urlsplit(url)
        if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
            return False
        display = urlsplit('https://' + domain)
        host = parts.hostname.lower().removeprefix('www.')
        displayed = (display.hostname or '').lower().removeprefix('www.')
        if host == 'm.blog.naver.com':
            host = 'blog.naver.com'
        if displayed == 'm.blog.naver.com':
            displayed = 'blog.naver.com'
        return host == displayed and parts.port == display.port
    except ValueError:
        return False


def _fenced_ranges(markdown: str) -> list[tuple[int, int]]:
    ranges = []
    opening = None
    offset = 0
    for line in markdown.splitlines(keepends=True):
        fence = re.match(r' {0,3}(`{3,}|~{3,})(.*)', line.rstrip('\r\n'))
        if fence:
            marker, trailing = fence.groups()
            if opening is None:
                opening = (offset, marker[0], len(marker))
            elif marker[0] == opening[1] and len(marker) >= opening[2] and not trailing.strip():
                ranges.append((opening[0], offset + len(line)))
                opening = None
        offset += len(line)
    if opening is not None:
        ranges.append((opening[0], len(markdown)))
    return ranges


def upgrade_link_cards(document: dict) -> tuple[dict, int]:
    """Return a copy with old thumbnail/title/excerpt/domain previews restored.

    Only complete, standalone previews immediately following a known image
    token qualify. Ordinary links and uncertain Markdown stay byte-for-byte
    unchanged, and no network requests or filesystem writes are performed.
    """
    result = deepcopy(document)
    markdown = result.get('markdown')
    images, sources = result.get('images'), result.get('sources')
    if not isinstance(markdown, str) or not isinstance(images, list) or not isinstance(sources, list):
        return result, 0
    tokens = {image.get('token') for image in images if isinstance(image, dict)
              and isinstance(image.get('token'), str) and re.fullmatch(r'[A-Za-z0-9]+', image['token'])}
    if not tokens:
        return result, 0
    image_pattern = '|'.join(re.escape(token) for token in sorted(tokens, key=len, reverse=True))
    pattern = re.compile(
        r'^(?P<thumbnail>' + image_pattern + r')[ \t]*\r?\n(?:[ \t]*\r?\n)*'
        r'\[\*\*(?P<title>[^\r\n]+?)\*\*[ \t]*' + _PARAGRAPH
        + r'(?P<description>[^\r\n]+(?:\r?\n(?![ \t]*\r?$)[^\r\n]+)*?)'
        + _PARAGRAPH
        + r'(?P<domain>[A-Za-z0-9][A-Za-z0-9.\-]*(?::[0-9]+)?)/?\]'
        r'\((?P<url>https?://[^\s<>()\\]+)\)[ \t]*(?=\r?$)',
        re.MULTILINE,
    )
    fenced = _fenced_ranges(markdown)
    count = 0

    def replace(match):
        nonlocal count
        if any(start <= match.start() < end for start, end in fenced):
            return match.group()
        title = _plain_text(match['title'])
        description = _plain_text(match['description'])
        if not title or not description or not _same_domain(match['domain'], match['url']):
            return match.group()
        # New tokens cannot overlap the old extractor's IMAGE/SOURCE tokens.
        token = 'NBACARD' + uuid4().hex.upper()
        while token in markdown or any(source.get('token') == token for source in sources
                                        if isinstance(source, dict)):
            token = 'NBACARD' + uuid4().hex.upper()
        sources.append({'kind': 'link_card', 'token': token, 'url': match['url'],
                        'target': parse_post_url(match['url']), 'title': title,
                        'description': description, 'domain': match['domain'],
                        'thumbnail_token': match['thumbnail']})
        count += 1
        return token

    result['markdown'] = pattern.sub(replace, markdown)
    return result, count
