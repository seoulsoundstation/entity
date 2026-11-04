from dataclasses import replace
import json

import pytest

from naver_blog_archive.archive import backup, reformat_archive
from naver_blog_archive.network import Client, Listing, category_paths
from naver_blog_archive.parser import compose
from naver_blog_archive.premium import parse_content_categories
from naver_blog_archive.state import State
from test_archive import MAIN, FakeClient, FakeRenderer, Response, config, html, saved_path
from test_premium import BLOG, POST, listing


PATH = ['투자 생각', '시장 단상']


def run(config, categories=None, pages=None, **kwargs):
    client = FakeClient(Listing([MAIN[1]], True, total=1, categories=categories or {}))
    return backup(config, client=client, renderer=FakeRenderer(pages), **kwargs)


def document(config):
    state = State(config.out_dir)
    try:
        return json.loads(state.get(*MAIN)['document'])
    finally:
        state.close()


def test_category_tree_resolves_parents_and_falls_back_to_listing_name():
    tree = {'6': ('투자 생각', None), '14': ('시장 단상', '6'), '1': ('순환', '2'), '2': ('구조', '1')}
    found = {'a': ('14', '시장 단상'), 'b': ('6', '투자 생각'), 'c': ('99', '삭제된 분류'), 'd': ('1', '순환'), 'e': ('99', '')}
    result = category_paths(tree, found)
    assert result['a'] == PATH and result['b'] == ['투자 생각'] and result['c'] == ['삭제된 분류']
    assert result['d'] == ['구조', '순환'] and 'e' not in result


def test_blog_listing_reads_category_tree(config, monkeypatch):
    client = Client(config)
    pages = [{'result': {'items': [{'logNo': 1, 'categoryNo': 14, 'categoryName': '시장 단상'},
                                   {'logNo': 2, 'categoryNo': 6, 'categoryName': '투자 생각'}], 'totalCount': 2}},
             {'result': {'items': [], 'totalCount': 2}},
             {'result': {'mylogCategoryList': [
                 {'categoryNo': 6, 'parentCategoryNo': None, 'categoryName': ' 투자  생각 '},
                 {'categoryNo': 14, 'parentCategoryNo': 6, 'categoryName': '시장 단상'}]}}]
    calls, responses = [], iter(Response(text=json.dumps(page)) for page in pages)
    monkeypatch.setattr(client, 'get', lambda url, **k: calls.append(url) or next(responses))
    result = client.listing('demo')
    assert result.complete and result.categories == {'1': PATH, '2': ['투자 생각']}
    assert calls[-1] == 'https://m.blog.naver.com/api/blogs/demo/category-list'
    client.close()


def test_blog_listing_keeps_leaf_category_when_tree_is_unavailable(config, monkeypatch):
    client = Client(config)
    responses = iter([Response(text=json.dumps({'result': {'items': [{'logNo': 1, 'categoryNo': 14, 'categoryName': '시장 단상'}]}})),
                      Response(text=json.dumps({'result': {'items': []}})), Response(text='not json')])
    monkeypatch.setattr(client, 'get', lambda *a, **k: next(responses))
    assert client.listing('demo').categories == {'1': ['시장 단상']}
    client.close()


def test_premium_listing_category_is_read_per_post():
    page = listing([POST]).replace('<a class="content_text_link"',
                                   '<a href="/salarymoney/moneystock/contents?categoryId=1" class="content_category">주요 이슈 정리 </a>'
                                   '<a class="content_text_link"')
    assert parse_content_categories(page, BLOG) == {POST: ['주요 이슈 정리']}
    assert parse_content_categories(listing([POST]), BLOG) == {}


def test_front_matter_holds_category_path_list_and_original_title():
    text = compose({'title': '제목 "따옴표"', 'blog_id': 'demo', 'post_id': '1', 'archived_at': 'now',
                    'markdown': '본문', 'category': PATH}, {}).decode('utf-8')
    assert 'title: "[투자 생각 > 시장 단상] 제목 \\"따옴표\\""\noriginal_title: "제목 \\"따옴표\\""' in text
    assert 'category: "투자 생각/시장 단상"\ncategories:\n  - "투자 생각"\n  - "시장 단상"\n---' in text
    assert '\n# 제목 "따옴표"\n' in text


def test_new_post_is_named_and_described_with_category(config):
    assert run(config, {MAIN[1]: PATH})['posts'] == {'success': 1}
    path = saved_path(config)
    assert path.name == f'[투자 생각 - 시장 단상] Title ({MAIN[1]}).md'
    text = path.read_text(encoding='utf-8')
    assert 'title: "[투자 생각 > 시장 단상] Title"' in text and 'category: "투자 생각/시장 단상"' in text
    assert document(config)['category_named'] is True


def test_existing_post_is_renamed_once_and_later_changes_update_properties_only(config):
    run(config)
    before = saved_path(config)
    assert before.name == f'Title ({MAIN[1]}).md'
    assert run(config, {MAIN[1]: PATH}, pages={})['posts'] == {'success': 1}
    renamed = saved_path(config)
    assert renamed.name == f'[투자 생각 - 시장 단상] Title ({MAIN[1]}).md' and not before.exists()
    assert list((config.out_dir / 'history').rglob('*.md'))
    assert run(config, {MAIN[1]: ['새 분류']}, pages={})['posts'] == {'success': 1}
    assert saved_path(config) == renamed
    text = renamed.read_text(encoding='utf-8')
    assert 'category: "새 분류"' in text and 'title: "[새 분류] Title"' in text


def test_bracketed_title_still_gains_category_prefix(config):
    run(config, pages={MAIN: html(title='[리뷰] 책')})
    run(config, {MAIN[1]: PATH}, pages={})
    assert saved_path(config).name == f'[투자 생각 - 시장 단상] [리뷰] 책 ({MAIN[1]}).md'


def test_refresh_and_failed_listing_keep_category(config):
    run(config, {MAIN[1]: PATH})
    path = saved_path(config)
    assert run(config, refresh=True)['posts'] == {'success': 1}
    assert saved_path(config) == path and document(config)['category'] == PATH
    assert reformat_archive(config)['posts'] == {'success': 1}
    assert 'category: "투자 생각/시장 단상"' in path.read_text(encoding='utf-8')


def test_edited_note_is_not_renamed_or_overwritten(config):
    run(config)
    path = saved_path(config)
    path.write_text('내 메모', encoding='utf-8')
    run(config, {MAIN[1]: PATH}, pages={})
    assert saved_path(config) == path and path.read_text(encoding='utf-8') == '내 메모'
