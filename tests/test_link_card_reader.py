"""Reading views preserve link-card context and safe, portable thumbnails."""
import os
from urllib.parse import quote, unquote, urlsplit

from bs4 import BeautifulSoup
import pytest

from naver_blog_archive.archive import format_link_card
from naver_blog_archive.config import Config
from naver_blog_archive.reader import create_preview


def saved_card(tmp_path, markdown):
    config = Config('demo', tmp_path / '보관함')
    relative = 'posts/demo/링크를 모은 글 (123456789).md'
    source = config.out_dir / relative
    source.parent.mkdir(parents=True)
    source.write_text(markdown, encoding='utf-8')
    row = {'blog_id': 'demo', 'post_id': '123456789', 'path': relative, 'title': '링크를 모은 글'}
    return config, row, source


SOURCE_URL = 'https://blog.naver.com/source/987654321'
LOCAL_NOTE = 'posts/source/포트폴리오 생각 #3 (987654321).md'
CARD = format_link_card(
    {'url': SOURCE_URL, 'title': '2019년 포트폴리오 생각',
     'description': '2019년 포트폴리오는 어떻게 짜야 할까? 경제 흐름을 살펴봅니다.'},
    '../../attachments/경제%20흐름.png',
    quote('../source/포트폴리오 생각 #3 (987654321).md', safe='/'),
)


def test_card_reader_formats_link_preview_and_preserves_markdown(tmp_path):
    config, row, source = saved_card(tmp_path, '# 링크를 모은 글\n\n' + CARD)
    destination = config.out_dir / LOCAL_NOTE
    destination.parent.mkdir(parents=True)
    destination.write_text('# 보관된 원본 글\n', encoding='utf-8')
    before = source.read_bytes(), source.stat().st_mtime_ns
    destination_before = destination.read_bytes(), destination.stat().st_mtime_ns
    preview = create_preview(config, row)
    soup = BeautifulSoup(preview.read_text(encoding='utf-8'), 'html.parser')
    card = soup.select_one('blockquote.link-card')
    assert card is not None
    assert '[!info]' not in card.get_text()
    assert card['aria-label'] == '링크 미리보기: 2019년 포트폴리오 생각'
    assert card.select_one('.link-card-title a').get_text() == '2019년 포트폴리오 생각'
    image = card.select_one('.link-card-thumbnail img')
    assert image['width'] == '320'
    assert image['alt'] == '2019년 포트폴리오 생각 썸네일'
    assert (preview.parent / unquote(image['src'])).resolve() == (config.out_dir / 'attachments/경제 흐름.png').resolve()
    assert '경제 흐름' in card.select_one('.link-card-description').get_text()
    local_link = card.select_one('.link-card-footer').find('a', string='보관된 글 열기')
    for link in (card.select_one('.link-card-title a'), image.parent, local_link):
        parts = urlsplit(link['href'])
        assert not parts.scheme and not parts.netloc and not parts.fragment
        assert '%23' in parts.path
        assert (preview.parent / unquote(parts.path)).resolve() == destination.resolve()
    web_link = card.select_one('.link-card-footer').find('a', string='blog.naver.com ↗')
    assert web_link['href'] == SOURCE_URL
    assert all(link['rel'] == ['noopener', 'noreferrer'] for link in card.find_all('a'))
    assert before == (source.read_bytes(), source.stat().st_mtime_ns)
    assert destination_before == (destination.read_bytes(), destination.stat().st_mtime_ns)


def test_card_without_thumbnail_or_excerpt_and_ordinary_quotes_remain_distinct(tmp_path):
    markdown = '''> [!info] [간단한 링크](https://example.com)
>
> [example.com ↗](https://example.com)

> 일반 인용문
>
> ![320](../../attachments/ordinary.png)

> [!info] 일반 정보 안내
'''
    config, row, _ = saved_card(tmp_path, markdown)
    soup = BeautifulSoup(create_preview(config, row).read_text(encoding='utf-8'), 'html.parser')
    assert len(soup.select('.link-card')) == 1
    assert soup.select_one('.link-card-footer a').get_text() == 'example.com ↗'
    assert soup.select_one('.link-card-thumbnail') is None
    quotes = soup.find_all('blockquote')
    assert quotes[1].get('class') is None
    assert quotes[1].img['alt'] == '320'
    assert quotes[1].img.get('width') is None
    assert '[!info] 일반 정보 안내' in quotes[2].get_text()


def test_card_keeps_untrusted_content_inert_and_clamps_thumbnail_width(tmp_path):
    markdown = r'''> [!info] [\<script\>제목\</script\>](https://example.com)
>
> [![9999999999999999999999999](../../../outside.png)](https://example.com)
>
> <img src="x" onerror="alert(1)">
>
> [example.com ↗](https://example.com) · [외부 파일](../../../outside.txt)
'''
    config, row, _ = saved_card(tmp_path, markdown)
    soup = BeautifulSoup(create_preview(config, row).read_text(encoding='utf-8'), 'html.parser')
    card = soup.select_one('.link-card')
    assert card is not None
    assert card.img['width'] == '320'
    assert not card.img.get('src')
    assert card.img['alt'] == '<script>제목</script> 썸네일'
    assert not soup.find_all('script')
    assert not soup.select('[onerror]')
    assert '<img src="x" onerror="alert(1)">' in card.get_text()
    assert card.find('a', string='외부 파일').get('href') is None
    assert "default-src 'none'" in soup.find('meta', attrs={'http-equiv': 'Content-Security-Policy'})['content']


@pytest.mark.skipif(os.environ.get('NBA_BROWSER_TEST') != '1', reason='Set NBA_BROWSER_TEST=1 with Chromium installed')
def test_browser_loads_card_thumbnail_without_cropping_at_mobile_width(tmp_path):
    from PIL import Image
    from playwright.sync_api import sync_playwright

    config, row, _ = saved_card(tmp_path, '# 링크를 모은 글\n\n' + CARD)
    image_path = config.out_dir / 'attachments/경제 흐름.png'
    image_path.parent.mkdir()
    Image.new('RGB', (1200, 800), '#3f8f66').save(image_path)
    preview = create_preview(config, row)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={'width': 375, 'height': 850})
            page.goto(preview.as_uri())
            image = page.locator('.link-card-thumbnail img')
            image.scroll_into_view_if_needed()
            page.wait_for_function('() => document.querySelector(".link-card img").naturalWidth === 1200')
            size = image.evaluate('(image) => ({width: image.clientWidth, height: image.clientHeight})')
            assert 0 < size['width'] <= 320
            assert abs(size['width'] / size['height'] - 1.5) < .02
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert page.locator('.link-card-title').inner_text() == '2019년 포트폴리오 생각'
            assert '[!info]' not in page.locator('.link-card').inner_text()
            expected = (config.out_dir / LOCAL_NOTE).resolve().as_uri()
            for selector in ('.link-card-title a', '.link-card-thumbnail a'):
                assert page.locator(selector).evaluate('(link) => link.href') == expected
            assert page.locator('.link-card-footer a').first.evaluate('(link) => link.href') == SOURCE_URL
        finally:
            browser.close()
