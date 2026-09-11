"""Recognize authored file attachments without treating ordinary links as files."""
from __future__ import annotations

from copy import deepcopy
import json
import re
import string
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit
from uuid import uuid4

from bs4 import BeautifulSoup
from markdown_it import MarkdownIt


_NAVER_FILE_HOSTS = {'download.blog.naver.com', 'blogattach.naver.net'}
_FILE_COMPONENTS = '.se-file, .se-module-file, .se_file, .wrap_file_area'
_ESCAPE = re.compile(r'\\([' + re.escape(string.punctuation) + r'])')


def attachment_url(value: str, base_url: str = '') -> str:
    try:
        if not str(value).strip():
            return ''
        url = urljoin(base_url, str(value).strip())
        parts = urlsplit(url)
        if (parts.scheme not in ('http', 'https') or not parts.hostname
                or parts.username or parts.password or parts.port not in (None, 80, 443)):
            return ''
        return url
    except ValueError:
        return ''


def is_naver_attachment(url: str) -> bool:
    valid = attachment_url(url)
    return bool(valid and urlsplit(valid).hostname in _NAVER_FILE_HOSTS)


def attachment_name(url: str) -> str:
    """Decode a displayed name only; the downloader owns filesystem sanitation."""
    return unquote(urlsplit(url).path.rsplit('/', 1)[-1]).strip() or '첨부파일'


def attachment_identity(url: str) -> str:
    """Identify the same Naver file across renewed expiring URL signatures.

    Only the observed download host and two known path layouts qualify. The
    resource identifier, name, and query remain part of the identity.
    """
    valid = attachment_url(url)
    if not valid:
        return url
    parts = urlsplit(valid)
    pieces = parts.path.split('/')
    if parts.hostname == 'download.blog.naver.com':
        signature = 2 if len(pieces) >= 5 and pieces[1] == 'open' else 1
        if (len(pieces) > signature + 2 and re.fullmatch(r'[a-fA-F0-9]{20,}', pieces[signature])
                and (signature == 2 or re.fullmatch(r'\d{8}_\d+_blogfile', pieces[2]))):
            pieces[signature] = '_signature_'
            return urlunsplit(parts._replace(scheme='https', path='/'.join(pieces), fragment=''))
    return urlunsplit(parts._replace(fragment=''))


def _anchor_url(anchor, base_url: str) -> str:
    url = attachment_url(anchor.get('href') or '', base_url)
    if url and not str(anchor.get('href') or '').strip().startswith('#'):
        return url
    try:
        data = json.loads(anchor.get('data-linkdata') or '{}')
        if isinstance(data, dict) and isinstance(data.get('link'), str):
            return attachment_url(data['link'], base_url)
    except (ValueError, TypeError):
        pass
    return ''


def extract_file_links(body, base_url: str, prefix: str) -> list[dict]:
    """Replace real file components or direct Naver downloads in body order."""
    files = []
    candidates = list(body.select(_FILE_COMPONENTS + ', a[href], a[data-linktype="file"]'))
    consumed = set()
    for element in candidates:
        if element.parent is None or any(id(parent) in consumed for parent in element.parents):
            continue
        is_component = element.name != 'a'
        anchor = (element.select_one('a.se-file-save-button, a.file_name_area, a[href], a[data-linkdata]')
                  if is_component else element)
        if anchor is None:
            continue
        url = _anchor_url(anchor, base_url)
        explicit = is_component or anchor.get('data-linktype') == 'file' or anchor.has_attr('download')
        # A preview or ordinary linked photo remains a preview/photo even if its
        # target happens to be a file download.
        if (not url or (not explicit and not is_naver_attachment(url))
                or (not explicit and anchor.find('img'))
                or any(parent.name in ('pre', 'code') for parent in anchor.parents)
                or any(set(parent.get('class', [])) & {'se-oglink', 'se-module-oglink',
                           'se_oglink', 'se_og_box', 'og-tag'} for parent in anchor.parents)):
            continue
        name = attachment_name(url)
        if is_component:
            label = element.select_one('.se-file-name')
            extension = element.select_one('.se-file-extension')
            if label is not None:
                name = label.get_text('', strip=True)
                suffix = extension.get_text('', strip=True) if extension is not None else ''
                if suffix and not name.lower().endswith(suffix.lower()):
                    name += suffix
                name = name or attachment_name(url)
        elif anchor.get('download'):
            name = str(anchor['download'])
        token = prefix + 'FILE' + str(len(files))
        files.append({'token': token, 'url': url, 'name': name})
        consumed.add(id(element))
        element.replace_with('\n' + token + '\n')
    return files


def file_links_from_html(html: str, blog: str, post: str) -> list[dict]:
    """Read current file URL metadata without rendering or replacing the post."""
    from .network import ARTICLE_BODY_SELECTORS, NON_CONTENT_SELECTOR, post_url

    soup = BeautifulSoup(html, 'html.parser')
    body = next((element for selector in ARTICLE_BODY_SELECTORS
                 if (element := soup.select_one(selector)) is not None), None)
    if body is None:
        raise ValueError('첨부파일 주소를 갱신할 본문 영역을 찾지 못했습니다.')
    for element in body.select(NON_CONTENT_SELECTOR):
        if element.parent is not None:
            element.decompose()
    return [{'url': item['url'], 'name': item['name']}
            for item in extract_file_links(body, post_url(blog, post), 'NBAFILEMETADATA')]


def upgrade_file_links(document: dict) -> tuple[dict, int]:
    """Recover standalone cached Naver downloads without fetching any pages.

    Markdown parsing excludes fenced/indented code, images, nested previews,
    and links in prose. The original parser emitted actual attachment widgets
    as standalone links, optionally preceded by their label and filename.
    """
    result = deepcopy(document)
    markdown = result.get('markdown')
    if not isinstance(markdown, str) or not markdown:
        return result, 0
    if not any(host in markdown.lower() for host in _NAVER_FILE_HOSTS):
        return result, 0
    if 'files' in result and not isinstance(result['files'], list):
        return result, 0
    lines = markdown.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    edits, recovered = [], []
    tokens = MarkdownIt('commonmark').parse(markdown)
    for token in tokens:
        if token.type != 'inline' or token.map is None or token.map[1] - token.map[0] != 1:
            continue
        children = token.children or []
        if (len(children) < 3 or children[0].type != 'link_open' or children[-1].type != 'link_close'
                or any(child.type != 'text' for child in children[1:-1])):
            continue
        url = children[0].attrGet('href') or ''
        if not is_naver_attachment(url):
            continue
        line_no = token.map[0]
        # Lists/quotes also have inline tokens: only a plain standalone link
        # represents a known old widget, not a quote/example authored by a user.
        raw = lines[line_no].rstrip('\r\n')
        if raw.strip() != token.content or not raw.startswith('['):
            continue
        name = attachment_name(url)
        start, end = offsets[line_no], offsets[line_no] + len(raw)
        if line_no >= 4:
            heading = lines[line_no - 4].rstrip('\r\n')
            label = lines[line_no - 2].rstrip('\r\n')
            if (heading == '**첨부파일**' and not lines[line_no - 3].strip()
                    and not lines[line_no - 1].strip() and _ESCAPE.sub(r'\1', label) == name):
                start = offsets[line_no - 4]
        placeholder = 'NBAFILE' + uuid4().hex.upper()
        while placeholder in markdown:
            placeholder = 'NBAFILE' + uuid4().hex.upper()
        recovered.append({'token': placeholder, 'url': url, 'name': name})
        edits.append((start, end, placeholder))
    if not recovered:
        return result, 0
    for start, end, placeholder in reversed(edits):
        markdown = markdown[:start] + placeholder + markdown[end:]
    result['markdown'] = markdown
    result.setdefault('files', []).extend(recovered)
    return result, len(recovered)
