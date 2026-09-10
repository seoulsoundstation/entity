from dataclasses import replace
import json
import pytest

from bs4 import BeautifulSoup
from markdown_it import MarkdownIt

from naver_blog_archive.archive import backup, inspect_archive, reformat_archive, format_note, format_link_card, write_note
from naver_blog_archive.catalog import search_archive
from naver_blog_archive.state import State
from test_archive import MAIN, PHOTO, FakeClient, FakeRenderer, config, html, run, saved_path


def card(url='https://blog.naver.com/linked/987654321', description='링크된 글의 짧은 설명입니다.'):
    return f'''<div class="se-component se-oglink"><div class="se-module se-module-oglink">
      <a class="se-oglink-thumbnail" href="{url}"><img src="{PHOTO}"></a>
      <a class="se-oglink-info" href="{url}"><strong class="se-oglink-title">링크 제목</strong>
        <p class="se-oglink-summary">{description}</p><p class="se-oglink-url">blog.naver.com</p></a>
    </div></div>'''


def test_link_card_is_one_group_with_local_thumbnail_and_no_automatic_linked_post_fetch(config):
    client, renderer = FakeClient(), FakeRenderer({MAIN: html('<p>앞 문장</p>' + card() + '<p>뒤 문장</p>')})
    result = backup(config, client=client, renderer=renderer)
    assert result['posts'] == {'success': 1}
    assert renderer.calls == [MAIN]
    note = saved_path(config).read_text(encoding='utf-8')
    assert '> [!info] [링크 제목](https://blog.naver.com/linked/987654321)' in note
    assert '> [![320](../../attachments/' in note
    assert note.index('앞 문장') < note.index('[!info]') < note.index('뒤 문장')
    assert 'NBATOKEN' not in note
    soup = BeautifulSoup(MarkdownIt().render(note), 'html.parser')
    assert len(soup.find_all('blockquote')) == 1
    assert len(soup.find_all('img')) == 1
    assert soup.blockquote.img.parent['href'] == 'https://blog.naver.com/linked/987654321'
    assert soup.blockquote.img['src'].startswith('../../attachments/')
    assert search_archive(config, '짧은 설명')['total'] == 1
    assert not inspect_archive(config, verify=True)['verification_problems']
    renderer, client = FakeRenderer({}), FakeClient()
    assert backup(config, renderer=renderer, client=client)['run_counts']['reused'] == 1
    assert renderer.calls == client.calls == []


def test_short_card_link_does_not_resolve_or_change_citation_graph(config):
    client, renderer = FakeClient(), FakeRenderer({MAIN: html(card('https://naver.me/card'))})
    client.resolve_source = lambda *_: (_ for _ in ()).throw(AssertionError('card preview must not resolve/fetch linked article'))
    backup(config, client=client, renderer=renderer)
    assert backup(config, client=client, renderer=renderer)['run_counts']['reused'] == 1
    assert renderer.calls == [MAIN]


def test_card_uses_an_existing_local_note_without_requiring_one(config):
    run(config, {MAIN: html(card('https://blog.naver.com/demo/123456789'))})
    note = saved_path(config).read_text(encoding='utf-8')
    assert '보관된 글 열기' in note
    assert 'https://blog.naver.com/demo/123456789' in note


def test_failed_thumbnail_has_readable_card_without_broken_image(config):
    result = run(config, {MAIN: html(card())}, FakeClient(data=b'not an image'))
    note = saved_path(config).read_text(encoding='utf-8')
    assert result['posts'] == {'partial': 1}
    assert '썸네일을 저장하지 못했습니다.' in note
    assert '> [!info] [링크 제목]' in note
    assert '링크된 글의 짧은 설명입니다.' in note
    assert '![' not in note
    renderer = FakeRenderer({})
    assert backup(config, client=FakeClient(), renderer=renderer)['posts'] == {'success': 1}
    assert renderer.calls == []
    assert '> [![320](../../attachments/' in saved_path(config).read_text(encoding='utf-8')


def test_images_disabled_leaves_remote_thumbnail_link(config):
    client = FakeClient(data=RuntimeError('must not download'))
    run(replace(config, download_images=False), {MAIN: html(card())}, client)
    assert not client.calls
    assert '> [![320](https://example.test/' in saved_path(config).read_text(encoding='utf-8')


def test_card_excerpt_is_short_plain_text_and_cannot_escape_the_card():
    description = '# Heading\n\n<script>bad</script> ![fake](https://example.test/img) **fake** ' + '설명' * 150
    source = {'url': 'https://example.test/a(b)?query=ok', 'title': '제목 *별표* [괄호] <태그>', 'description': description}
    note = format_link_card(source, None)
    soup = BeautifulSoup(MarkdownIt('commonmark', {'html': False}).render(note), 'html.parser')
    assert soup.blockquote.find('a').get_text() == source['title']
    assert len(soup.find_all('blockquote')) == 1
    assert not soup.find_all('script') and not soup.find_all('h1') and not soup.find_all('img')
    assert '…' in soup.get_text()
    assert len(soup.get_text()) < 290


@pytest.mark.parametrize('description', ['- 첫 문장', '+ 첫 문장', '---', '1. 첫 문장', '~~취소선~~', '==강조=='])
def test_card_excerpt_markers_remain_plain_text(description):
    note = format_link_card({'url': 'https://example.test', 'title': '제목', 'description': description}, None)
    soup = BeautifulSoup(MarkdownIt().render(note), 'html.parser')
    assert not soup.select('ul, ol, hr, h1, s, strong')
    assert soup.blockquote.find_all('p')[1].get_text() == description


def seed_old_card(config):
    run(config, {MAIN: html(f'<p>앞</p><img src="{PHOTO}"><p>뒤</p>')})
    state = State(config.out_dir)
    row = state.get(*MAIN)
    document = json.loads(row['document'])
    token = document['images'][0]['token']
    document['markdown'] = ('앞\n\n' + token + '\n[**이전 링크 카드**\n\n이전 카드의 발췌\n\n'
                            'example.test](https://example.test/article)\n\n뒤')
    state.update(*MAIN, document=json.dumps(document))
    row = state.get(*MAIN)
    content, errors = format_note(config, state, row)
    assert not errors
    write_note(config, state, row, content)
    identity, archived = row['archive_id'], document['archived_at']
    state.close()
    return identity, archived


def test_existing_cached_card_is_reformatted_offline_and_keeps_id_timestamp_and_original_history(config):
    identity, archived = seed_old_card(config)
    before = saved_path(config).read_bytes()
    photos = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (config.out_dir / 'attachments').iterdir()}
    result = reformat_archive(config)
    assert result['output_counts']['cards'] == 1
    assert result['posts'] == {'success': 1}
    assert '[!info] [이전 링크 카드]' in saved_path(config).read_text(encoding='utf-8')
    assert photos == {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in photos}
    assert any(p.read_bytes() == before for p in (config.out_dir / 'history').rglob('*.md'))
    state = State(config.out_dir)
    row = state.get(*MAIN)
    assert row['archive_id'] == identity
    assert json.loads(row['document'])['archived_at'] == archived
    state.close()
    assert not reformat_archive(config)['output_counts'].get('cards')
    assert not inspect_archive(config, verify=True)['verification_problems']


def test_existing_card_upgrade_in_normal_backup_needs_no_page_or_image_download(config):
    seed_old_card(config)
    renderer, client = FakeRenderer({}), FakeClient()
    result = backup(config, renderer=renderer, client=client)
    assert result['output_counts']['cards'] == 1
    assert result['posts'] == {'success': 1}
    assert renderer.calls == client.calls == []


def test_user_edits_prevent_cached_card_upgrade_and_rewrite(config):
    seed_old_card(config)
    state = State(config.out_dir)
    original_doc = state.get(*MAIN)['document']
    state.close()
    path = saved_path(config)
    edited = path.read_bytes() + b'\nMy changes\n'
    path.write_bytes(edited)
    result = reformat_archive(config)
    assert not result['output_counts'].get('cards')
    assert result['posts'] == {'partial': 1}
    assert path.read_bytes() == edited
    state = State(config.out_dir)
    assert state.get(*MAIN)['document'] == original_doc
    state.close()
