"""A local, script-free reading view generated from the saved Markdown file."""
from __future__ import annotations

import hashlib
from html import escape
import os
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from markdown_it import MarkdownIt
from markdown_it.token import Token

from .config import Config
from .files import archive_lock, atomic_write, inside


_STYLE = '''
:root { color-scheme: light; font-family: "Malgun Gothic", "Apple SD Gothic Neo", sans-serif;
  color: #24352d; background: #f3f6f3; line-height: 1.85; }
* { box-sizing: border-box; }
body { margin: 0; padding: 32px 20px 64px; }
.toolbar, article { width: min(100%, 880px); margin-inline: auto; }
.toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: 12px 20px;
  padding: 0 6px 18px; color: #5a6d61; font-size: 14px; }
.toolbar strong { margin-right: auto; color: #315641; }
article { background: white; border: 1px solid #dce5dc; border-radius: 14px;
  padding: 38px 48px 50px; overflow-wrap: anywhere; }
article > :first-child { margin-top: 0; }
h1, h2, h3 { line-height: 1.4; color: #193b29; }
h1 { font-size: clamp(1.65rem, 4vw, 2.15rem); margin-bottom: 30px; }
h2, h3 { margin-top: 1.7em; }
p { margin: 1.1em 0; }
a { color: #187143; text-underline-offset: 3px; }
a:hover { color: #0a4727; text-decoration-thickness: 2px; }
a:focus-visible { outline: 2px solid #258950; outline-offset: 4px; border-radius: 2px; }
img { display: block; max-width: 100%; height: auto; margin: 22px auto; border-radius: 5px; }
blockquote { margin: 24px 0; padding: 2px 20px; border-left: 3px solid #a5c7ae;
  background: #f4f8f4; color: #506657; }
.link-card { max-width: 580px; padding: 0; border: 1px solid #d8e3da; border-radius: 10px;
  background: #fff; color: #496052; overflow: hidden; box-shadow: 0 2px 8px #24352d08; }
.link-card > p { margin: 0; }
.link-card-title { padding: 17px 20px 15px; border-bottom: 1px solid #e8eee9;
  font-size: 1.05rem; font-weight: 700; line-height: 1.55; background: #f6f9f6; }
.link-card-title a { text-decoration: none; }
.link-card-title a:hover { text-decoration: underline; }
.link-card-thumbnail { padding: 18px 20px 0; }
.link-card-thumbnail img { max-width: min(100%, 320px); height: auto; margin: 0 auto;
  object-fit: contain; border-radius: 5px; }
.link-card-description { padding: 16px 20px; font-size: .93rem; line-height: 1.8; }
.link-card-footer { padding: 13px 20px; margin-top: 16px !important; background: #f6f9f6;
  border-top: 1px solid #e8eee9; font-size: .8rem; line-height: 1.75; }
.link-card-description + .link-card-footer { margin-top: 0 !important; }
pre { padding: 18px; overflow: auto; background: #f1f4f1; border-radius: 6px; line-height: 1.5; }
code { background: #f1f4f1; border-radius: 3px; padding: 2px 4px; }
pre code { padding: 0; }
table { display: block; max-width: 100%; border-collapse: collapse; overflow-x: auto; }
th, td { border: 1px solid #dce5dc; padding: 8px 12px; }
th { background: #f4f8f4; }
hr { border: 0; border-top: 1px solid #dce5dc; margin: 32px 0; }
@media (max-width: 600px) { body { padding: 16px 10px 32px; }
  article { padding: 26px 20px 34px; border-radius: 10px; } }
@media print { body { background: white; padding: 0; } .toolbar { display: none; }
  article { border: 0; padding: 0; width: 100%; } img { break-inside: avoid; } }
'''


def _body(markdown: str) -> str:
    """Hide only the archive's leading YAML metadata block from the reader."""
    lines = markdown.splitlines(keepends=True)
    if not lines or lines[0].strip() != '---':
        return markdown
    for index, line in enumerate(lines[1:], 1):
        if line.strip() == '---':
            header = ''.join(lines[1:index])
            if 'source_url:' in header and 'post_id:' in header:
                return ''.join(lines[index + 1:])
            break
    return markdown


def _local_url(root: Path, source: Path, preview: Path, url: str, *, image: bool) -> str | None:
    parts = urlsplit(url)
    if parts.scheme.lower() in ('https', 'http'):
        return url
    if not image and parts.scheme.lower() == 'mailto':
        return url
    if parts.scheme or parts.netloc:
        return None
    if not parts.path:
        return None if image else url
    # A Markdown file from the web may contain local-path links. Resolve all of
    # them against the saved note and refuse access outside this archive.
    local = inside(root, str(source.parent / unquote(parts.path)))
    target = quote(Path(os.path.relpath(local, preview.parent)).as_posix(), safe='/')
    return urlunsplit(('', '', target, parts.query, parts.fragment))


def _style_link_cards(tokens: list[Token]) -> None:
    """Present the archive's Obsidian link callouts without enabling raw HTML."""
    for index, opening in enumerate(tokens):
        if opening.type != 'blockquote_open' or index + 3 >= len(tokens):
            continue
        first, inline = tokens[index + 1:index + 3]
        children = inline.children or []
        if (first.type != 'paragraph_open' or inline.type != 'inline'
                or len(children) < 4 or children[0].type != 'text'
                or children[0].content != '[!info] '
                or children[1].type != 'link_open' or children[-1].type != 'link_close'
                or sum(child.type == 'link_open' for child in children) != 1):
            continue
        closing = next((end for end in range(index + 3, len(tokens))
                        if tokens[end].type == 'blockquote_close'
                        and tokens[end].level == opening.level), None)
        if closing is None:
            continue
        title = ''.join(child.content for child in children[2:-1]
                        if child.type in ('text', 'code_inline')).strip() or '링크 글'
        inline.children = children[1:]
        opening.attrJoin('class', 'link-card')
        opening.attrSet('aria-label', '링크 미리보기: ' + title)
        paragraphs = [position for position in range(index + 1, closing)
                      if tokens[position].type == 'paragraph_open'
                      and tokens[position].level == opening.level + 1]
        for position in paragraphs:
            paragraph = tokens[position]
            content = tokens[position + 1]
            contents = content.children or []
            images = [child for child in contents if child.type == 'image']
            if position == index + 1:
                kind = 'title'
            elif images:
                kind = 'thumbnail'
            elif position == paragraphs[-1] and any(child.type == 'link_open' for child in contents):
                kind = 'footer'
            else:
                kind = 'description'
            paragraph.attrJoin('class', 'link-card-' + kind)
            for image in images:
                # Obsidian interprets a numeric image label as a display width.
                # Restrict that convention to cards, and expose a useful alt.
                if image.content.isascii() and image.content.isdecimal():
                    width = min(320, max(1, int(image.content))) if len(image.content) < 5 else 320
                    image.attrSet('width', str(width))
                    image.content = title + ' 썸네일'
                    text = Token('text', '', 0)
                    text.content = image.content
                    image.children = [text]


def create_preview(config: Config, row: dict) -> Path:
    """Render one saved note without changing it or its database record.

    Relative images are rebased to the generated page, so the archive remains
    portable. Raw HTML is escaped, unsafe URLs are removed, and a restrictive
    CSP provides a second layer against executing content from saved posts.
    """
    root = config.out_dir.resolve()
    source = inside(root, row['path'])
    if source.suffix.lower() != '.md':
        raise ValueError('저장 글은 Markdown(.md) 파일만 읽을 수 있습니다.')
    if not source.is_file():
        raise ValueError('저장 파일이 없습니다. 백업을 실행하면 복구를 시도합니다.')
    key = hashlib.sha256(source.relative_to(root).as_posix().encode('utf-8')).hexdigest()
    preview = inside(root, f'.preview/{key}.html')
    with archive_lock(root):
        markdown = source.read_text(encoding='utf-8-sig')
        renderer = MarkdownIt('commonmark', {'html': False}).enable('table')
        tokens = renderer.parse(_body(markdown))
        _style_link_cards(tokens)
        pending = list(tokens)
        while pending:
            token = pending.pop()
            if token.children:
                pending.extend(token.children)
            attribute = 'src' if token.type == 'image' else 'href' if token.type == 'link_open' else None
            if attribute is None:
                continue
            url = token.attrGet(attribute) or ''
            try:
                target = _local_url(root, source, preview, url, image=token.type == 'image')
            except (OSError, ValueError):
                target = None
            if target is None:
                token.attrs.pop(attribute, None)
            else:
                token.attrSet(attribute, target)
            if token.type == 'image':
                token.attrSet('loading', 'lazy')
                token.attrSet('decoding', 'async')
            else:
                token.attrSet('rel', 'noopener noreferrer')
        content = renderer.renderer.render(tokens, renderer.options, {})
        title = escape(str(row.get('title') or row.get('post_id') or source.stem))
        md_url = quote(Path(os.path.relpath(source, preview.parent)).as_posix(), safe='/')
        source_url = 'https://blog.naver.com/' + quote(str(row['blog_id']), safe='') + '/' + quote(str(row['post_id']), safe='')
        page = f'''<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src 'self' file: https: http:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<meta name="referrer" content="no-referrer">
<title>{title} · 블로그 보관함</title><style>{_STYLE}</style></head>
<body><nav class="toolbar" aria-label="저장 글 메뉴"><strong>블로그 보관함 · 읽기</strong>
<a href="{escape(md_url, quote=True)}" download>Markdown 파일</a>
<a href="{escape(source_url, quote=True)}" rel="noopener noreferrer">네이버 원문</a></nav>
<article aria-label="{title}">{content}</article></body></html>
'''
        atomic_write(preview, page.encode('utf-8'))
    return preview
