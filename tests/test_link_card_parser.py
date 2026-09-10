from pathlib import Path

import pytest

from naver_blog_archive.parser import extract_post


def article(body):
    return f'<div class="se-title-text">본문 제목</div><div class="se-main-container">{body}</div>'


def test_modern_naver_card_preserves_thumbnail_title_summary_and_position():
    html = (Path(__file__).parent / 'fixtures' / 'link_card_modern.html').read_text(encoding='utf-8')
    document = extract_post(html, 'tmdejr1267', '221622842053')
    assert len(document['sources']) == len(document['images']) == 1
    card = document['sources'][0]
    thumbnail = document['images'][0]
    assert card['kind'] == 'link_card'
    assert card['title'] == '12월의 중순 생각 기록: 2019년 포트폴리오 생각'
    assert card['description'].startswith('* 2019년 포트폴리오는 어떻게 짜야할까?')
    assert card['domain'] == 'blog.naver.com'
    assert card['url'] == 'https://blog.naver.com/tmdejr1267/221417590875'
    assert card['target'] == ('tmdejr1267', '221417590875')
    assert thumbnail['url'].startswith('https://dthumb-phinf.pstatic.net/?src=')
    assert '&type=ff500_300' in thumbnail['url']
    assert card['thumbnail_token'] == thumbnail['token']
    assert thumbnail['alt'] == card['title'] + ' 미리보기'
    assert thumbnail['token'] not in document['markdown']
    assert card['title'] not in document['markdown']
    assert document['markdown'] == f'미리보기 앞 본문\n\n{card["token"]}\n\n미리보기 뒤 본문'


def test_legacy_card_is_not_consumed_by_generic_citation_extraction():
    document = extract_post(article('''
        <div class="se_component se_oglink"><div class="se_sectionArea"><div class="se_og_box">
          <a class="se_og_thumbnail" href="https://example.test/article"><img src="/photo.png"></a>
          <a class="se_og_text" href="https://example.test/article">
            <strong class="se_og_title">  카드 <span>제목</span>  </strong>
            <p class="se_og_desc">  설명 첫 줄\n둘째 줄 </p><p class="se_og_cp">example.test</p>
          </a>
        </div></div></div>
        <div class="se_sectionArea"><a href="https://blog.naver.com/source/123">별도 출처</a></div>
    '''), 'demo', '1')
    assert len(document['sources']) == 2
    card, citation = document['sources']
    assert card['title'] == '카드 제목'
    assert card['description'] == '설명 첫 줄 둘째 줄'
    assert card['target'] is None
    assert document['images'][0]['url'] == 'https://m.blog.naver.com/photo.png'
    assert citation.get('kind') is None
    assert citation['title'] == '별도 출처'
    assert citation['target'] == ('source', '123')
    assert card['token'] in document['markdown'] and citation['token'] in document['markdown']


@pytest.mark.parametrize('container', ['se-module-oglink', 'se_og_box', 'og-tag'])
def test_card_without_thumbnail_or_summary_still_keeps_a_title_link(container):
    document = extract_post(article(f'<div class="{container}">'
                            '<a href="https://example.test/news"><strong>제목만 있음</strong></a></div>'),
                            'demo', '1')
    card = document['sources'][0]
    assert card['title'] == '제목만 있음'
    assert card['description'] == ''
    assert card['domain'] == 'example.test'
    assert card['thumbnail_token'] is None
    assert document['images'] == []


def test_regular_photo_links_and_plain_text_links_remain_ordinary_content():
    document = extract_post(article('<a href="https://example.test/photo"><img src="/photo.png"></a>'
                            '<p><a href="https://example.test/article">본문의 일반 링크</a></p>'), 'demo', '1')
    assert document['sources'] == []
    assert len(document['images']) == 1
    assert document['images'][0]['token'] in document['markdown']
    assert '[본문의 일반 링크](https://example.test/article)' in document['markdown']


def test_card_thumbnail_uses_lazy_image_source_and_skips_placeholder():
    document = extract_post(article('''<div class="se-oglink">
      <a class="se-oglink-thumbnail" href="https://example.test/article">
        <img src="data:image/gif;base64,AA=="><img data-lazy-src="//images.test/card.webp" alt="카드 사진">
      </a><a class="se-oglink-info" href="https://example.test/article"><strong class="se-oglink-title">제목</strong></a>
    </div>'''), 'demo', '1')
    assert len(document['images']) == 1
    assert document['images'][0]['url'] == 'https://images.test/card.webp'
    assert document['images'][0]['alt'] == '카드 사진'


@pytest.mark.parametrize('url', ['javascript:alert(1)', 'file:///C:/article.md', 'http://['])
def test_non_web_or_invalid_card_url_is_not_promoted_to_a_downloadable_card(url):
    document = extract_post(article(f'<div class="se-oglink"><a href="{url}">일반 내용</a></div>'), 'demo', '1')
    assert document['sources'] == []
    assert '일반 내용' in document['markdown']


def test_nested_and_repeated_card_components_are_extracted_once_each():
    markup = '''<div class="se-oglink"><div class="se-module-oglink">
        <a class="se-oglink-info" href="https://example.test/repeated">
          <strong class="se-oglink-title">같은 링크</strong></a>
    </div></div>'''
    document = extract_post(article(markup + '<p>사이 본문</p>' + markup), 'demo', '1')
    assert len(document['sources']) == 2
    first, second = document['sources']
    assert first['token'] != second['token']
    assert document['markdown'] == f'{first["token"]}\n\n사이 본문\n\n{second["token"]}'
