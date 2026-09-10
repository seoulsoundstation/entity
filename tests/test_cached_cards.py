from copy import deepcopy

import pytest

from naver_blog_archive.cached_cards import upgrade_link_cards


TOKEN = 'NBATOKENABCIMAGE1'
CARD = (TOKEN + '\n[**12월의 중순 생각 기록: 2019년 포트폴리오 생각**\n\n'
        r'\* 2019년 포트폴리오는 어떻게 짜야할까? 방향성: 외화 보유를 통한 변동성 헷지와 바벨전략...'
        '\n\nblog.naver.com](https://blog.naver.com/demo/123)')


def document(markdown=CARD):
    return {'title': '20190820 투자 생각', 'blog_id': 'demo', 'post_id': '456',
            'archived_at': '2026-09-10T00:00:00+00:00', 'markdown': markdown,
            'images': [{'token': TOKEN, 'url': 'https://example.test/thumbnail.png', 'alt': ''}],
            'sources': []}


def test_cached_preview_preserves_thumbnail_title_excerpt_and_target_without_mutating_input():
    original = document('앞 문단\n\n' + CARD + '\n\n다음 문단')
    before = deepcopy(original)
    result, count = upgrade_link_cards(original)
    assert count == 1
    assert original == before
    card = result['sources'][0]
    assert card['kind'] == 'link_card'
    assert card['title'] == '12월의 중순 생각 기록: 2019년 포트폴리오 생각'
    assert card['description'].startswith('* 2019년 포트폴리오는 어떻게 짜야할까?')
    assert card['domain'] == 'blog.naver.com'
    assert card['target'] == ('demo', '123')
    assert card['thumbnail_token'] == TOKEN
    assert result['markdown'] == '앞 문단\n\n' + card['token'] + '\n\n다음 문단'
    assert result['images'] == original['images']
    assert result['archived_at'] == original['archived_at']
    assert upgrade_link_cards(result) == (result, 0)


@pytest.mark.parametrize('markdown', [
    TOKEN + '\n[일반 링크](https://blog.naver.com/demo/123)',
    CARD.replace('**12월', '12월').replace('생각**', '생각'),
    CARD.replace('blog.naver.com]', 'example.com]'),
    CARD.replace(TOKEN, 'UNKNOWNIMAGE'),
    CARD.replace('포트폴리오 생각**', '[다른 링크](https://example.com)**'),
    CARD.replace('포트폴리오 생각**', '<b>HTML</b>**'),
    CARD.replace('blog.naver.com](https:', 'blog.naver.com](javascript:'),
    CARD.replace('https://blog.naver.com/', 'https://user@blog.naver.com/'),
    CARD.replace('https://blog.naver.com/', 'https://blog.naver.com:broken/'),
    CARD.replace(TOKEN + '\n', TOKEN + '\n별도 설명\n'),
    '```markdown\n' + CARD + '\n```',
    '~~~\n' + CARD + '\n~~~',
    '```\n' + CARD,
    '    ' + CARD.replace('\n', '\n    '),
    CARD.replace('\n\nblog.naver.com', '\n\n추가 문단\n\nblog.naver.com'),
])
def test_ordinary_or_ambiguous_markdown_is_unchanged(markdown):
    original = document(markdown)
    assert upgrade_link_cards(original) == (original, 0)


def test_preserves_escaped_punctuation_as_plain_metadata():
    original = document(CARD.replace('12월의 중순 생각 기록: 2019년 포트폴리오 생각',
                                    r'제목 \[참고\] \*별표\* C:\\notes'))
    result, count = upgrade_link_cards(original)
    assert count == 1
    assert result['sources'][0]['title'] == r'제목 [참고] *별표* C:\notes'


def test_multiple_cards_keep_order_and_existing_sources():
    second = CARD.replace(TOKEN, TOKEN + '0').replace('/demo/123', '/second/789')
    original = document(CARD + '\n\n문단\n\n' + second)
    original['images'].append({'token': TOKEN + '0', 'url': 'https://example.test/second.png'})
    source = {'token': 'EXISTINGSOURCE', 'url': 'https://example.com', 'title': '기존 출처'}
    original['sources'].append(source)
    result, count = upgrade_link_cards(original)
    assert count == 2
    assert result['sources'][0] == source
    assert [item['target'] for item in result['sources'][1:]] == [('demo', '123'), ('second', '789')]
    assert len({item['token'] for item in result['sources']}) == 3


def test_windows_newlines_wrapped_description_and_mobile_domain():
    original = document(CARD.replace('방향성:', '방향성:\n')
                        .replace('https://blog.naver.com/', 'https://m.blog.naver.com/')
                        .replace('\n', '\r\n'))
    result, count = upgrade_link_cards(original)
    assert count == 1
    assert '\n' not in result['sources'][0]['description']
    assert result['sources'][0]['target'] == ('demo', '123')


def test_only_actual_preview_after_fenced_example_is_converted():
    result, count = upgrade_link_cards(document('```\n' + CARD + '\n```\n\n' + CARD))
    assert count == 1
    assert result['markdown'].startswith('```\n' + CARD + '\n```\n\n')


def test_external_cards_preserve_link_without_becoming_a_blog_backup_target():
    result, count = upgrade_link_cards(document(CARD.replace('blog.naver.com', 'example.com')))
    assert count == 1
    assert result['sources'][0]['target'] is None


@pytest.mark.parametrize('value', [{}, {'markdown': None}, {'markdown': '', 'images': [], 'sources': []}])
def test_missing_or_empty_cached_data_is_unchanged(value):
    assert upgrade_link_cards(value) == (value, 0)
