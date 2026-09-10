from copy import deepcopy

import pytest

from naver_blog_archive.cached_navigation import clean_cached_navigation


PROFILE = 'NBATOKENABCIMAGE0'
PHOTO = 'NBATOKENABCIMAGE1'
TITLE = '[제목] 투자 생각'
HEADER = (
    '[투자 생각](/PostList.naver?blogId=demo&categoryNo=6&logCode=0'
    '&categoryName=%ED%88%AC%EC%9E%90#postlist_block)\n\n'
    + TITLE + '\n\n' + PROFILE + '\n\n'
    '[**Seung**](/PostList.naver?blogId=demo)\n\n'
    '2019. 11. 11. 23:35\n\n[이웃추가](#)\n\n\n본문 기타 기능\n\n\n\n\n'
)
MENU = ('* **본문 폰트 크기 조정**본문 폰트 크기 작게 보기본문 폰트 크기 크게 보기가\n'
        '* [공유하기](#)\n* [URL복사](#)\n* [신고하기](#)\n\n\n\n\n')
BODY = ('본문에는 이웃추가, 카테고리, 공유하기, 댓글에 대한 설명도 있습니다.\n\n'
        + PHOTO + '\n\nNBACARDSOURCE\n\n'
        '[카테고리 설명](https://example.com/category)\n\n'
        '```markdown\n[이웃추가](#)\n본문 기타 기능\n```\n')


def document(markdown=HEADER + BODY):
    return {
        'blog_id': 'demo', 'post_id': '123', 'title': TITLE,
        'published_at': '2019-11-11T23:35:00+09:00',
        'archived_at': '2026-09-11T00:00:00+00:00',
        'archive_id': 'NBA-PERSISTENT', 'markdown': markdown,
        'images': [
            {'token': PROFILE, 'url': 'https://blogpfthumb-phinf.pstatic.net/profile.jpg', 'alt': '프로필'},
            {'token': PHOTO, 'url': 'https://blogfiles.pstatic.net/body.jpg', 'alt': '본문 사진'},
        ],
        'sources': [{'kind': 'link_card', 'token': 'NBACARDSOURCE', 'thumbnail_token': PHOTO,
                     'url': 'https://blog.naver.com/demo/456', 'title': '다른 글'}],
    }


@pytest.mark.parametrize('menu', ['', MENU])
def test_removes_complete_header_and_controls_preserving_body_and_metadata(menu):
    original = document(HEADER + menu + BODY)
    before = deepcopy(original)
    result, changed = clean_cached_navigation(original)
    assert changed
    assert original == before
    assert result['markdown'] == BODY
    assert result['images'] == [original['images'][1]]
    assert result['sources'] == original['sources']
    for key in ('blog_id', 'post_id', 'title', 'published_at', 'archived_at', 'archive_id'):
        assert result[key] == original[key]
    assert clean_cached_navigation(result) == (result, False)


def test_whitespace_collapsed_duplicate_title_and_windows_newlines():
    original = document((HEADER + MENU + BODY).replace('\n', '\r\n'))
    original['title'] = '[제목]  투자   생각'
    result, changed = clean_cached_navigation(original)
    assert changed
    assert result['markdown'] == BODY.replace('\n', '\r\n')


@pytest.mark.parametrize('markdown', [
    BODY,
    '설명\n\n' + HEADER + BODY,
    '```markdown\n' + HEADER + BODY + '\n```',
    '    ' + (HEADER + BODY).replace('\n', '\n    '),
    HEADER.replace(TITLE, '다른 제목') + BODY,
    HEADER.replace('categoryNo=6', 'categoryNo=bad') + BODY,
    HEADER.replace('#postlist_block', '#other') + BODY,
    HEADER.replace('blogId=demo', 'blogId=someone_else', 1) + BODY,
    HEADER.replace('[**Seung**](/PostList.naver?blogId=demo)',
                   '[**Seung**](/PostList.naver?blogId=another)') + BODY,
    HEADER.replace('/PostList.naver?', 'https://example.com/PostList.naver?') + BODY,
    HEADER.replace('/PostList.naver?', 'https://user@blog.naver.com/PostList.naver?') + BODY,
    HEADER.replace('/PostList.naver?', 'https://blog.naver.com:broken/PostList.naver?') + BODY,
    HEADER.replace('[이웃추가](#)', '[이웃추가](https://example.com)') + BODY,
    HEADER.replace(PROFILE, 'UNKNOWNPROFILE') + BODY,
    HEADER.replace('본문 기타 기능', '본문 안내') + BODY,
    HEADER + MENU.replace('* [URL복사](#)\n', '') + BODY,
    HEADER,
    HEADER + '\u200b\n\n',
    HEADER + MENU,
])
def test_uncertain_or_empty_documents_and_non_header_occurrences_are_unchanged(markdown):
    original = document(markdown)
    assert clean_cached_navigation(original) == (original, False)


@pytest.mark.parametrize('profile', [
    {'token': PROFILE, 'url': 'https://blogfiles.pstatic.net/body.jpg', 'alt': '프로필'},
    {'token': PROFILE, 'url': 'https://blogpfthumb-phinf.pstatic.net/profile.jpg', 'alt': '본문 사진'},
    {'token': PROFILE, 'url': 'https://blogpfthumb-phinf.pstatic.net.attacker.test/profile.jpg', 'alt': '프로필'},
    {'token': PROFILE, 'url': None, 'alt': '프로필'},
])
def test_unverified_profile_image_preserves_complete_document(profile):
    original = document()
    original['images'][0] = profile
    assert clean_cached_navigation(original) == (original, False)


@pytest.mark.parametrize('reference', ['body', 'card'])
def test_profile_image_record_remains_when_still_used_by_article(reference):
    original = document()
    if reference == 'body':
        original['markdown'] += '\n\n' + PROFILE
    else:
        original['sources'][0]['thumbnail_token'] = PROFILE
    result, changed = clean_cached_navigation(original)
    assert changed
    assert result['images'] == original['images']


def test_duplicate_image_token_is_ambiguous():
    original = document()
    original['images'].append(deepcopy(original['images'][0]))
    assert clean_cached_navigation(original) == (original, False)


@pytest.mark.parametrize('value', [{}, {'markdown': None}, {'markdown': '', 'images': [], 'sources': []}])
def test_missing_data_is_unchanged(value):
    assert clean_cached_navigation(value) == (value, False)
