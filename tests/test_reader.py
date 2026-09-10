"""Portable picture rendering and isolation of untrusted saved Markdown."""
from pathlib import Path
import os
import shutil
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup
import pytest

from naver_blog_archive.config import Config
from naver_blog_archive.files import archive_lock
from naver_blog_archive.reader import create_preview


def saved(tmp_path, markdown, *, title='제주에서 보낸 하루'):
    config = Config('demo', tmp_path / '보관함')
    relative = 'posts/demo/제주 [여행] #1 %.md'
    path = config.out_dir / relative
    path.parent.mkdir(parents=True)
    path.write_text(markdown, encoding='utf-8-sig')
    row = {'blog_id': 'demo', 'post_id': '123456789', 'path': relative, 'title': title}
    return config, row, path


def test_reader_formats_markdown_and_rebases_pictures_without_changing_original(tmp_path):
    config, row, source = saved(tmp_path, '''---
title: "제주에서 보낸 하루"
post_id: "123456789"
source_url: "https://blog.naver.com/demo/123456789"
---

# 제주에서 보낸 하루

**바다**를 보고 왔습니다.

![제주 사진](../../attachments/사진%20%231%20%25.png)

| 장소 | 느낌 |
| --- | --- |
| 제주 | 좋음 |
''')
    photo = config.out_dir / 'attachments' / '사진 #1 %.png'
    photo.parent.mkdir()
    photo.write_bytes(b'local photo')
    before = source.read_bytes(), source.stat().st_mtime_ns
    preview = create_preview(config, row)
    soup = BeautifulSoup(preview.read_text(encoding='utf-8'), 'html.parser')
    assert soup.title.string == row['title'] + ' · 블로그 보관함'
    assert soup.article.h1.string == row['title']
    assert soup.article.strong.string == '바다'
    assert soup.table is not None
    assert 'source_url:' not in soup.article.get_text()
    image = soup.article.img
    assert (preview.parent / unquote(image['src'])).resolve() == photo.resolve()
    assert image['alt'] == '제주 사진'
    assert image['loading'] == 'lazy'
    assert 'max-width: 100%' in soup.style.string
    assert before == (source.read_bytes(), source.stat().st_mtime_ns)
    assert not (config.out_dir / 'archive_state.sqlite').exists()
    download = soup.find('a', download=True)
    assert (preview.parent / unquote(download['href'])).resolve() == source.resolve()


def test_generated_preview_still_finds_local_images_after_whole_archive_moves(tmp_path):
    config, row, _ = saved(tmp_path, '# 사진\n\n![](../../attachments/test.png)')
    image = config.out_dir / 'attachments' / 'test.png'
    image.parent.mkdir()
    image.write_bytes(b'image content')
    preview = create_preview(config, row)
    moved = tmp_path / '다른 위치'
    shutil.copytree(config.out_dir, moved)
    moved_preview = moved / preview.relative_to(config.out_dir)
    soup = BeautifulSoup(moved_preview.read_text(encoding='utf-8'), 'html.parser')
    assert (moved_preview.parent / unquote(soup.img['src'])).read_bytes() == b'image content'
    assert str(config.out_dir) not in moved_preview.read_text(encoding='utf-8')


def test_raw_html_and_javascript_are_inert_and_local_escape_urls_are_removed(tmp_path):
    config, row, source = saved(tmp_path, '''# Content

<script>alert('script')</script>
<img src="x" onerror="alert('image')">
<iframe src="https://example.com"></iframe>

[run](javascript:alert(1))

![external local](../../../outside.png)

[outside](../../../outside.txt)

![UNC](//example.com/picture.png)

[HTTPS](https://example.com/path?q=1&next=2)

[section](#section)
''', title='\"<script>alert(9)</script>')
    preview = create_preview(config, row)
    soup = BeautifulSoup(preview.read_text(encoding='utf-8'), 'html.parser')
    assert not soup.find_all('script')
    assert not soup.find_all('iframe')
    assert not soup.select('[onerror]')
    assert '<script>' in soup.article.get_text()
    assert all(not image.get('src') for image in soup.article.find_all('img'))
    assert all(urlsplit(link.get('href', '')).scheme != 'javascript' for link in soup.find_all('a'))
    assert soup.find('a', string='outside').get('href') is None
    assert soup.find('a', string='HTTPS')['href'] == 'https://example.com/path?q=1&next=2'
    assert soup.find('a', string='section')['href'] == '#section'
    assert "default-src 'none'" in soup.find('meta', attrs={'http-equiv': 'Content-Security-Policy'})['content']


def test_normal_thematic_break_at_start_is_preserved(tmp_path):
    config, row, _ = saved(tmp_path, '---\n\n본문\n\n---\n\n마지막')
    preview = create_preview(config, row)
    soup = BeautifulSoup(preview.read_text(encoding='utf-8'), 'html.parser')
    assert len(soup.article.find_all('hr')) == 2
    assert '본문' in soup.article.get_text()


@pytest.mark.parametrize('path', ['../outside.md', 'posts/demo/missing.md', 'posts/demo/program.exe'])
def test_rejects_paths_outside_archive_missing_files_and_non_markdown(tmp_path, path):
    config, row, _ = saved(tmp_path, 'Content')
    (config.out_dir / 'posts/demo/program.exe').write_bytes(b'not markdown')
    with pytest.raises(ValueError):
        create_preview(config, {**row, 'path': path})
    assert not (config.out_dir / '.preview').exists()


def test_existing_backup_lock_leaves_source_and_preview_untouched(tmp_path):
    config, row, source = saved(tmp_path, 'Before')
    preview = create_preview(config, row)
    original = preview.read_bytes()
    source.write_text('After', encoding='utf-8')
    with archive_lock(config.out_dir):
        with pytest.raises(RuntimeError, match='이미 실행 중'):
            create_preview(config, row)
    assert preview.read_bytes() == original
    assert source.read_text(encoding='utf-8') == 'After'
    assert create_preview(config, row) == preview
    assert b'After' in preview.read_bytes()


@pytest.mark.skipif(os.environ.get('NBA_BROWSER_TEST') != '1', reason='Set NBA_BROWSER_TEST=1 with Chromium installed')
def test_browser_loads_saved_picture_and_fits_narrow_reading_width(tmp_path):
    from PIL import Image
    from playwright.sync_api import sync_playwright

    config, row, _ = saved(tmp_path, '# 제주 사진\n\n![큰 사진](../../attachments/큰%20사진.png)')
    image = config.out_dir / 'attachments' / '큰 사진.png'
    image.parent.mkdir()
    Image.new('RGB', (1200, 800), '#3f8f66').save(image)
    preview = create_preview(config, row)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={'width': 375, 'height': 850})
            page.goto(preview.as_uri())
            page.locator('article img').scroll_into_view_if_needed()
            page.wait_for_function('document.querySelector("article img").naturalWidth === 1200')
            assert page.locator('article img').evaluate('(image) => image.clientWidth') <= 315
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert page.locator('article h1').inner_text() == '제주 사진'
        finally:
            browser.close()
