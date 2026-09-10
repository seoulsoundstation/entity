from io import BytesIO
from urllib.parse import quote

from PIL import Image
import pytest
import requests

from naver_blog_archive.assets import Assets, image_candidates
from naver_blog_archive.config import Config
from naver_blog_archive.files import digest
from naver_blog_archive.parser import compose, extract_post, md_link
from naver_blog_archive.state import State


def article(content):
    return f'<div class="se-main-container">{content}</div>'


def test_nested_article_excludes_profile_photo_and_mobile_controls():
    document = extract_post(
        '<div class="post_ct"><img src="https://example.test/profile.jpg">'
        '<p>이웃추가 · 본문 기타 기능</p>'
        + article('<p>실제 본문</p><img src="https://example.test/article.png">')
        + '<p>공유하기</p></div>', 'demo', '123')

    assert [image['url'] for image in document['images']] == ['https://example.test/article.png']
    assert '실제 본문' in document['markdown']
    assert '이웃추가' not in document['markdown']
    assert '공유하기' not in document['markdown']


def test_old_editor_content_takes_priority_over_outer_mobile_wrapper():
    document = extract_post('<div class="post_ct">메뉴<div id="postViewArea">글 본문</div></div>',
                            'demo', '123')
    assert document['markdown'] == '글 본문'


@pytest.mark.parametrize('attributes, expected', [
    ('data-lazy-src="  " data-src=" //example.test/photo.png " src="placeholder.gif"',
     'https://example.test/photo.png'),
    ('data-lazy-src="data:image/gif;base64,AAAA" src=" https://example.test/photo.png "',
     'https://example.test/photo.png'),
    ('data-src="javascript:void(0)" src="/photo.png"', 'https://m.blog.naver.com/photo.png'),
    ('data-lazy-src="http://[" src="/photo.png"', 'https://m.blog.naver.com/photo.png'),
    ('src="data:image/gif;base64,AAAA"', ''),
])
def test_lazy_image_placeholders_fall_back_to_downloadable_source(attributes, expected):
    document = extract_post(article(f'<img {attributes}>'), 'demo', '123')
    assert document['images'][0]['url'] == expected


def test_photo_labels_do_not_split_markdown_table_cells():
    document = extract_post(article('<table><tr><td><img src="/photo.png" alt="매출 | 이익 [원]">'
                                    '</td><td>설명</td></tr></table>'), 'demo', '123')
    photo = document['images'][0]
    note = compose(document, {photo['token']: md_link(photo['alt'], '../../attachments/photo.png', True)})
    assert r'![매출 \| 이익 \[원\]](../../attachments/photo.png)' in note.decode('utf-8')


def test_image_labels_keep_windows_newlines_inside_single_markdown_line():
    assert md_link('첫 줄\r\n둘째 줄', 'photo.png', True) == '![첫 줄 둘째 줄](photo.png)'


def test_all_photos_keep_their_own_image_when_post_has_more_than_ten_photos():
    document = extract_post(article(''.join(f'<img src="/photo{index}.png">' for index in range(23))),
                            'demo', '123')
    replacements = {photo['token']: md_link(f'사진 {index}', f'../../attachments/photo{index}.png', True)
                    for index, photo in enumerate(document['images'])}
    note = compose(document, replacements).decode('utf-8')
    assert 'NBATOKEN' not in note
    assert note.count('![') == 23
    for value in replacements.values():
        assert note.count(value) == 1


def test_source_tokens_and_inserted_labels_are_replaced_in_one_pass():
    document = extract_post(article(''.join(
        f'<div class="se_sectionArea"><a href="https://example.test/{index}">출처 {index}</a></div>'
        for index in range(12))), 'demo', '123')
    replacements = {source['token']: md_link(f'출처 {index}', source['url'])
                    for index, source in enumerate(document['sources'])}
    # An inserted label can contain text that happens to equal another token.
    first, last = document['sources'][0]['token'], document['sources'][-1]['token']
    replacements[first] = last
    note = compose(document, replacements).decode('utf-8')
    assert note.count(last) == 1
    assert note.count(replacements[last]) == 1
    for source in document['sources'][1:-1]:
        assert note.count(replacements[source['token']]) == 1


def test_thumbnail_fallback_preserves_encoded_original_path_and_signed_query():
    original = 'https://example.test/%ED%95%9C%EA%B8%80.jpg?token=a%2Fb&type=w800&x=1'
    proxy = 'https://dthumb-phinf.pstatic.net/?src=' + quote('"' + original + '"', safe='') + '&type=ff500_300'
    assert image_candidates(proxy)[-2:] == [
        'https://example.test/%ED%95%9C%EA%B8%80.jpg?token=a%2Fb&x=1', original]


@pytest.mark.parametrize('source', ['file:///C:/photo.png', 'data:image/png;base64,AAAA', 'javascript:alert(1)', 'http://['])
def test_thumbnail_fallback_rejects_non_http_sources(source):
    proxy = 'https://dthumb-phinf.pstatic.net/?src=' + quote(source, safe='')
    assert image_candidates(proxy) == [proxy]


def test_thumbnail_fallback_only_applies_to_exact_naver_proxy_host():
    proxy = 'https://dthumb-phinf.pstatic.net.example.test/?src=https%3A%2F%2Fexample.test%2Fphoto.png'
    assert image_candidates(proxy) == [proxy]


def test_dead_thumbnail_saves_original_photo_and_reuses_the_same_local_file(tmp_path):
    original = 'https://example.test/photo.png'
    proxy = 'https://dthumb-phinf.pstatic.net/?src=' + quote('"' + original + '"', safe='') + '&type=ff500_300'
    stream = BytesIO()
    Image.new('RGB', (3, 2), 'red').save(stream, format='PNG')

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def iter_content(self, size):
            yield stream.getvalue()

    class Client:
        def __init__(self):
            self.calls = []

        def get(self, url, **kwargs):
            self.calls.append(url)
            if url != original:
                raise requests.HTTPError('404 dead thumbnail proxy')
            return Response()

    config, client = Config('demo', tmp_path), Client()
    state = State(tmp_path)
    try:
        path = Assets(config, state, client).obtain(proxy, 'https://m.blog.naver.com/demo/123')
        assert path and path.endswith('.png')
        saved = state.asset(proxy)
        assert saved['status'] == 'success'
        assert saved['file_hash'] == digest(tmp_path / path)
        assert (tmp_path / path).read_bytes() == stream.getvalue()
        assert client.calls[-1] == original
        calls_before, modified_before = client.calls[:], (tmp_path / path).stat().st_mtime_ns
        assert Assets(config, state, client).obtain(proxy, 'https://m.blog.naver.com/demo/123') == path
        assert client.calls == calls_before
        assert (tmp_path / path).stat().st_mtime_ns == modified_before
    finally:
        state.close()
