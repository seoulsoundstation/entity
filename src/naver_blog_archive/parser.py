from __future__ import annotations

from datetime import datetime, timezone
import json
import re
from urllib.parse import quote, urljoin, urlsplit
from uuid import uuid4

from bs4 import BeautifulSoup
from markdownify import markdownify

from .network import ARTICLE_BODY_SELECTORS, NON_CONTENT_SELECTOR, parse_post_url, post_url


def md_link(label: str, url: str, image: bool = False) -> str:
    label = re.sub(r'([\\\[\]|])', r'\\\1', re.sub(r'[\r\n]+', ' ', label))
    return f'{"!" if image else ""}[{label}]({quote(url, safe="/:?&=%+#@;,")})'


def image_source(img, base_url: str) -> str:
    # Lazy-load placeholders must not hide a usable regular source. Browsers
    # tolerate surrounding whitespace in HTML URL attributes; downloads need
    # the same normalization.
    for attribute in ('data-lazy-src', 'data-src', 'src'):
        value = str(img.get(attribute) or '').strip()
        if not value:
            continue
        try:
            candidate = urljoin(base_url, value)
            if urlsplit(candidate).scheme in ('http', 'https'):
                return candidate
        except ValueError:
            continue
    return ''


def _extract_link_cards(body, base_url: str, prefix: str, sources: list, images: list):
    # SmartEditor ONE and older SmartEditor cards wrap a thumbnail link and a
    # separate text link. Replacing the complete component keeps them together
    # instead of producing one long, multiline Markdown link.
    cards = list(body.select('.se-oglink, .se-module-oglink, .se_oglink, .se_og_box, .og-tag'))
    card_ids = {id(card) for card in cards}
    for card in cards:
        if any(id(parent) in card_ids for parent in card.parents):
            continue
        anchor = card.select_one('a.se-oglink-info[href], a.se_og_text[href]')
        if anchor is None:
            anchor = card.find('a', href=True)
        if anchor is None:
            continue
        try:
            url = urljoin(base_url, str(anchor['href']).strip())
            parts = urlsplit(url)
            if parts.scheme not in ('http', 'https') or not parts.hostname:
                continue
        except ValueError:
            continue
        title_element = card.select_one('.se-oglink-title, .se_og_title, .og-title, .og_title')
        if title_element is None:
            title_element = anchor.find(['strong', 'h3', 'h4'])
        title = (title_element.get_text(' ', strip=True) if title_element is not None
                 else anchor.get('title') or anchor.get_text(' ', strip=True))
        title = ' '.join(str(title).split()) or parts.hostname
        description_element = card.select_one(
            '.se-oglink-summary, .se_og_desc, .se_og_summary, .og-description, .og-desc, .og_description')
        description = (' '.join(description_element.get_text(' ', strip=True).split())
                       if description_element is not None else '')
        thumbnail_token = None
        for thumbnail in card.find_all('img'):
            thumbnail_url = image_source(thumbnail, base_url)
            if not thumbnail_url:
                continue
            thumbnail_token = prefix + 'IMAGE' + str(len(images))
            images.append({'token': thumbnail_token, 'url': thumbnail_url,
                           'alt': thumbnail.get('alt') or f'{title} 미리보기'})
            break
        token = prefix + 'SOURCE' + str(len(sources))
        sources.append({'token': token, 'kind': 'link_card', 'url': url,
                        'title': title, 'description': description, 'domain': parts.hostname,
                        'thumbnail_token': thumbnail_token, 'target': parse_post_url(url)})
        card.replace_with('\n' + token + '\n')


def extract_post(html: str, blog: str, post: str) -> dict:
    soup = BeautifulSoup(html, 'html.parser')
    # A combined CSS selector returns the first element in document order,
    # so an outer post_ct wrapper can otherwise win over the actual article.
    body = next((element for selector in ARTICLE_BODY_SELECTORS
                 if (element := soup.select_one(selector)) is not None), None)
    if body is None:
        raise ValueError('본문 영역을 찾지 못했습니다. 비공개·삭제·오류 페이지 또는 형식 변경을 확인하세요.')
    title_el = soup.select_one('div.se-title-text, .se_title, h3.tit_h3')
    meta = soup.select_one('meta[property="og:title"]')
    title = title_el.get_text(' ', strip=True) if title_el else (meta.get('content', '') if meta else '')
    if not title and soup.title:
        title = soup.title.get_text(' ', strip=True)
    title = title.strip() or post
    published = soup.select_one('meta[property="article:published_time"], time[datetime]')
    published_at = (published.get('content') or published.get('datetime')) if published else None
    # Read the title/date above before removing the page header. Keep authored
    # text and links even when their labels mention Naver's navigation.
    for element in body.select(NON_CONTENT_SELECTOR):
        if element.parent is not None:
            element.decompose()
    prefix = 'NBATOKEN' + uuid4().hex.upper()
    sources, images = [], []
    _extract_link_cards(body, post_url(blog, post), prefix, sources, images)
    # Extract before Markdown conversion; source content is replaced in its original position.
    for section in list(body.select('div.se_sectionArea')):
        a = section.find('a', href=True)
        if not a:
            continue
        url = urljoin(post_url(blog, post), a['href'])
        token = prefix + 'SOURCE' + str(len(sources))
        sources.append({'token': token, 'url': url, 'title': a.get_text(' ', strip=True) or '출처',
                        'target': parse_post_url(url)})
        section.replace_with('\n' + token + '\n')
    # Remove photo-only links once, before replacing any of their children.
    # A link can contain several images; replacing the whole link per image
    # would discard its siblings and then try to replace a detached element.
    for anchor in list(body.find_all('a')):
        if anchor.find('img') and not anchor.get_text(strip=True):
            anchor.unwrap()
    for img in list(body.find_all('img')):
        src = image_source(img, post_url(blog, post))
        token = prefix + 'IMAGE' + str(len(images))
        images.append({'token': token, 'url': src, 'alt': img.get('alt', '')})
        img.replace_with('\n' + token + '\n')
    for media in list(body.select('iframe, video, audio')):
        src = media.get('src')
        if not src:
            child = media.find('source', src=True)
            src = child.get('src') if child else None
        if src:
            link = soup.new_tag('a', href=urljoin(post_url(blog, post), src))
            link.string = '미디어 원본'
            media.replace_with(link)
        else:
            media.replace_with('[미디어: 원문에서 확인]')
    if not body.get_text(strip=True):
        raise ValueError('본문 영역이 비어 있어 저장하지 않았습니다.')
    markdown = markdownify(str(body), heading_style='ATX').strip()
    if not markdown:
        raise ValueError('Markdown 변환 결과가 비어 있습니다.')
    return {'title': title, 'blog_id': blog, 'post_id': post, 'markdown': markdown,
            'images': images, 'sources': sources, 'published_at': published_at,
            'archived_at': datetime.now(timezone.utc).isoformat()}


def compose(document: dict, replacements: dict[str, str]) -> bytes:
    body = document['markdown']
    if replacements:
        # IMAGE1 is a prefix of IMAGE10 in existing cached documents. Match
        # longest tokens first in one pass, without reprocessing inserted text.
        pattern = '|'.join(re.escape(token) for token in sorted(replacements, key=len, reverse=True))
        body = re.sub(pattern, lambda match: replacements[match.group()], body)
    metadata = {key: document[key] for key in ('title', 'blog_id', 'post_id', 'archived_at')}
    metadata['source_url'] = post_url(document['blog_id'], document['post_id'])
    if document.get('published_at'):
        metadata['published_at'] = document['published_at']
    # JSON scalar strings are valid YAML and safely handle quotes in titles.
    frontmatter = '\n'.join(f'{key}: {json.dumps(value, ensure_ascii=False)}' for key, value in metadata.items())
    title = document['title'].replace('\n', ' ')
    return f'---\n{frontmatter}\n---\n\n# {title}\n\n{body}\n'.encode('utf-8')
