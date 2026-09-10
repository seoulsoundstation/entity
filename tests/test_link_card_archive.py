from dataclasses import replace
import json
import pytest
from urllib.parse import unquote

from bs4 import BeautifulSoup
from markdown_it import MarkdownIt

from naver_blog_archive.archive import backup, inspect_archive, reformat_archive, format_note, format_link_card, write_note
from naver_blog_archive.catalog import search_archive
from naver_blog_archive.network import Listing
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
    assert_local_card(config, MAIN, MAIN)


def assert_local_card(config, origin, target, original=None):
    path = saved_path(config, origin)
    soup = BeautifulSoup(MarkdownIt().render(path.read_text(encoding='utf-8')), 'html.parser')
    links = soup.blockquote.find_all('a')
    # Title and thumbnail are the primary actions, both pointing at the note.
    for link in (links[0], soup.blockquote.img.parent):
        assert not link['href'].startswith('http')
        assert (path.parent / unquote(link['href'])).resolve() == saved_path(config, target).resolve()
    original = original or f'https://blog.naver.com/{target[0]}/{target[1]}'
    web_link = soup.blockquote.find('a', href=original)
    assert web_link is not None and web_link.get_text().endswith(' ↗')


@pytest.mark.parametrize('url', [
    'https://blog.naver.com/demo/987654321',
    'https://m.blog.naver.com/demo/987654321/?from=post#link',
    'https://blog.naver.com/PostView.naver?blogId=demo&amp;logNo=987654321',
])
def test_card_links_to_later_saved_note_and_back_with_sources_disabled(config, url):
    target = ('demo', '987654321')
    config = replace(config, follow_sources=False)
    renderer = FakeRenderer({
        MAIN: html(card(url), title='같은 제목 #1 (생각)'),
        target: html(card('https://blog.naver.com/demo/123456789'), title='같은 제목 #1 (생각)'),
    })
    client = FakeClient(Listing([MAIN[1], target[1]], True, total=2))
    result = backup(config, renderer=renderer, client=client)
    assert renderer.calls == [MAIN, target]
    assert result['posts'] == {'success': 2}
    assert_local_card(config, MAIN, target, url.replace('&amp;', '&'))
    assert_local_card(config, target, MAIN)
    assert not inspect_archive(config, verify=True)['verification_problems']


def test_new_target_converts_existing_card_without_downloading_parent_again(config):
    target = ('demo', '987654321')
    run(config, {MAIN: html(card('https://blog.naver.com/demo/987654321'))})
    before = saved_path(config).read_bytes()
    renderer, client = FakeRenderer({target: html(title='나중에 받은 글')}), FakeClient(Listing([MAIN[1], target[1]], True))
    backup(config, renderer=renderer, client=client)
    assert renderer.calls == [target]
    assert client.calls == []
    assert_local_card(config, MAIN, target)
    assert any(p.read_bytes() == before for p in (config.out_dir / 'history').rglob('*.md'))


def test_existing_web_card_is_relinked_offline_and_second_reformat_does_not_rewrite(config, monkeypatch):
    target = ('demo', '987654321')
    with monkeypatch.context() as previous_version:
        previous_version.setattr('naver_blog_archive.archive.format_link_card',
                                 lambda source, image, local=None: format_link_card(source, image))
        run(config, {MAIN: html(card('https://blog.naver.com/demo/987654321')), target: html()},
            FakeClient(Listing([MAIN[1], target[1]], True)))
    before = saved_path(config).read_bytes()
    assert reformat_archive(config)['output_counts']['updated'] == 1
    assert_local_card(config, MAIN, target)
    after = saved_path(config).read_bytes(), saved_path(config).stat().st_mtime_ns
    assert after[0] != before
    assert reformat_archive(config)['output_counts']['updated'] == 0
    assert (saved_path(config).read_bytes(), saved_path(config).stat().st_mtime_ns) == after


@pytest.mark.parametrize('target_condition', ['edited', 'missing_image', 'missing_file'])
def test_card_destination_depends_on_saved_markdown_existence(config, target_condition):
    target = ('demo', '987654321')
    run(config, {MAIN: html(card('https://blog.naver.com/demo/987654321')),
                 target: html(f'<img src="{PHOTO}">')}, FakeClient(Listing([MAIN[1], target[1]], True)))
    target_path = saved_path(config, target)
    if target_condition == 'edited':
        target_path.write_bytes(target_path.read_bytes() + b'\nMy edits\n')
    elif target_condition == 'missing_image':
        for image in (config.out_dir / 'attachments').iterdir():
            image.unlink()
    else:
        target_path.unlink()
    expected = target_path.read_bytes() if target_path.exists() else None
    inspect_archive(config, verify=True)
    state = State(config.out_dir)
    try:
        assert not state.get(*target)['content_ok']
        content, _ = format_note(config, state, state.get(*MAIN))
    finally:
        state.close()
    soup = BeautifulSoup(MarkdownIt().render(content.decode('utf-8')), 'html.parser')
    href = soup.blockquote.a['href']
    if target_condition == 'missing_file':
        assert href == 'https://blog.naver.com/demo/987654321'
        assert '보관된 글 열기' not in soup.get_text()
    else:
        assert (saved_path(config).parent / unquote(href)).resolve() == target_path.resolve()
        assert target_path.read_bytes() == expected


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
