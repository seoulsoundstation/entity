import pytest

from naver_blog_archive.parser import extract_post


# Minimal page controls from the mobile Naver markup. The article below is
# synthetic so the fixture tests structure rather than any author's wording.
HEADER = '''<div class="se-component se-documentTitle">
  <div class="blog_category"><a href="/PostList.naver?blogId=demo&categoryNo=6">카테고리 메뉴</a></div>
  <div class="se-title-text"><p>보관할 제목</p></div>
  <div class="blog_authorArea"><a href="/PostList.naver?blogId=demo">
    <img src="https://example.test/profile.jpg" alt="프로필">작성자</a></div>
  <div class="blog_btnArea"><a href="#" class="btn_buddyadd _add_buddy">이웃추가</a>
    <div class="post_function_t1"><button>본문 기타 기능</button></div></div>
</div>'''


@pytest.mark.parametrize('opening, closing', [
    ('<div class="se-main-container">', '</div>'),
    ('<div id="postViewArea">', '</div>'),
    ('<div class="post_ct">', '</div>'),
])
def test_known_navigation_is_excluded_before_image_and_markdown_extraction(opening, closing):
    document = extract_post(opening + HEADER + '''
      <p>실제 본문</p><img src="https://example.test/article.png" alt="본문 사진">
      <div class="blog_category"><a href="/PostList.naver?categoryNo=3">다른 카테고리 메뉴</a></div>
      <div class="blog_btnArea"><button>공유하기 메뉴</button></div>
      <div class="post_function_t1"><button>더보기 메뉴</button></div>
      <a class="_add_buddy" href="#">이웃추가 메뉴</a>
      <a class="btn_buddyadd" href="#">이웃추가 메뉴</a>
    ''' + closing, 'demo', '123')

    assert document['title'] == '보관할 제목'
    assert '실제 본문' in document['markdown']
    assert '보관할 제목' not in document['markdown']
    assert '메뉴' not in document['markdown']
    assert '이웃추가' not in document['markdown']
    assert '작성자' not in document['markdown']
    assert '기타 기능' not in document['markdown']
    assert [image['url'] for image in document['images']] == ['https://example.test/article.png']


def test_authored_prose_and_normal_category_links_are_preserved():
    document = extract_post('''<div class="post_ct">
      <p>이웃추가나 카테고리를 클릭하는 방법을 설명합니다.</p>
      <a href="https://blog.naver.com/PostList.naver?blogId=demo&categoryNo=6">추천 카테고리</a>
      <p class="category">직접 쓴 카테고리 설명</p>
      <button>본문에 포함한 버튼 설명</button>
      <blockquote>본문 기타 기능과 공유하기에 대한 인용문</blockquote>
    </div>''', 'demo', '123')

    assert '이웃추가나 카테고리를 클릭하는 방법을 설명합니다.' in document['markdown']
    assert '[추천 카테고리](https://blog.naver.com/PostList.naver?blogId=demo&categoryNo=6)' in document['markdown']
    assert '직접 쓴 카테고리 설명' in document['markdown']
    assert '본문에 포함한 버튼 설명' in document['markdown']
    assert '본문 기타 기능과 공유하기에 대한 인용문' in document['markdown']


def test_navigation_removal_preserves_link_cards_media_and_published_metadata():
    document = extract_post('<div class="post_ct">' + HEADER + '''
      <time datetime="2020-01-02T03:04:05+09:00"></time>
      <div class="se-oglink"><a class="se-oglink-thumbnail" href="https://example.test/article">
        <img src="https://example.test/card.png"></a>
        <a class="se-oglink-info" href="https://example.test/article">
          <strong class="se-oglink-title">이웃추가와 카테고리</strong>
          <p class="se-oglink-summary">카테고리를 클릭하는 방법</p></a></div>
      <iframe src="https://example.test/video"></iframe>
    </div>''', 'demo', '123')

    assert document['published_at'] == '2020-01-02T03:04:05+09:00'
    assert len(document['sources']) == 1
    assert document['sources'][0]['title'] == '이웃추가와 카테고리'
    assert document['sources'][0]['description'] == '카테고리를 클릭하는 방법'
    assert document['images'][0]['url'] == 'https://example.test/card.png'
    assert '[미디어 원본](https://example.test/video)' in document['markdown']


def test_page_containing_only_navigation_is_not_saved_as_an_article():
    with pytest.raises(ValueError, match='본문 영역이 비어'):
        extract_post('<div class="post_ct">' + HEADER + '</div>', 'demo', '123')


def test_empty_inner_article_does_not_fall_back_to_outer_navigation():
    with pytest.raises(ValueError, match='본문 영역이 비어'):
        extract_post('<div class="post_ct">' + HEADER + '<div class="se-main-container"></div></div>',
                     'demo', '123')
