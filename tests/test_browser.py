import os

import pytest

from naver_blog_archive.config import Config
from naver_blog_archive.network import Renderer
from naver_blog_archive.parser import extract_post


@pytest.mark.skipif(os.environ.get('NBA_BROWSER_TEST') != '1', reason='Set NBA_BROWSER_TEST=1 with Chromium installed')
def test_real_browser_waits_for_dynamic_body(tmp_path):
    from playwright.sync_api import sync_playwright
    renderer = Renderer(Config('demo', tmp_path, timeout=5, retries=1, delay=0))
    renderer.playwright = sync_playwright().start()
    renderer.browser = renderer.playwright.chromium.launch(headless=True)
    renderer.page = renderer.browser.new_page()
    page = '''<html><div class="se-title-text">Dynamic</div><div class="se-main-container"></div>
    <script>setTimeout(() => document.querySelector('.se-main-container').innerHTML = '<p>Loaded content</p>', 200)</script></html>'''
    renderer.page.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=page))
    try:
        document = extract_post(renderer.render('demo', '123456789'), 'demo', '123456789')
        assert document['markdown'] == 'Loaded content'
    finally:
        renderer.close()


@pytest.mark.skipif(os.environ.get('NBA_BROWSER_TEST') != '1', reason='Set NBA_BROWSER_TEST=1 with Chromium installed')
@pytest.mark.parametrize('initial, update', [
    ('<div class="se-main-container"></div>',
     "document.querySelector('.se-main-container').innerHTML = '<p>Loaded article</p>'"),
    ('<div id="postViewArea"></div>',
     "document.querySelector('#postViewArea').innerHTML = '<p>Loaded article</p>'"),
    ('', "document.querySelector('.post_ct').insertAdjacentHTML('beforeend', "
         "'<div class=\"se-main-container\"><p>Loaded article</p></div>')"),
    ('', "document.querySelector('.post_ct').insertAdjacentHTML('beforeend', '<p>Loaded article</p>')"),
])
def test_real_browser_waits_for_article_instead_of_ready_navigation(tmp_path, initial, update):
    from playwright.sync_api import sync_playwright

    renderer = Renderer(Config('demo', tmp_path, timeout=5, retries=1, delay=0))
    renderer.playwright = sync_playwright().start()
    renderer.browser = renderer.playwright.chromium.launch(headless=True)
    renderer.page = renderer.browser.new_page()
    page = f'''<html><div class="post_ct">
      <div class="se-documentTitle"><div class="se-title-text">Delayed article</div>
        <div class="blog_category"><a href="#">카테고리</a></div>
        <div class="blog_authorArea"><img src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7">작성자</div>
        <div class="blog_btnArea"><a class="btn_buddyadd" href="#">이웃추가</a></div>
      </div>{initial}</div>
      <script>setTimeout(() => {{ {update}; }}, 200)</script></html>'''
    renderer.page.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=page))
    try:
        document = extract_post(renderer.render('demo', '123456789'), 'demo', '123456789')
        assert document['title'] == 'Delayed article'
        assert document['markdown'] == 'Loaded article'
        assert document['images'] == []
    finally:
        renderer.close()
