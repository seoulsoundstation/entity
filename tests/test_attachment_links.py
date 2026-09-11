from copy import deepcopy
import json
from urllib.parse import quote

import pytest

from naver_blog_archive.attachment_links import (
    attachment_identity, file_links_from_html, is_naver_attachment, upgrade_file_links,
)
from naver_blog_archive.parser import extract_post


SIGNATURE = 'abcdef0123456789' * 3
NAME = '좋은 기업은 SWEET하다.pdf'
URL = f'https://download.blog.naver.com/open/{SIGNATURE}/resource-id/{quote(NAME)}'
OLD_URL = f'https://download.blog.naver.com/{SIGNATURE}/20201027_225_blogfile/owner_123_pdf/report.pdf'


def article(body):
    return f'<div class="se-title-text">본문 제목</div><div class="se-main-container">{body}</div>'


def modern(url=URL):
    return f'''<div class="se-component se-file"><div class="se-module se-module-file">
      <span class="se-file-icon"><strong class="se-blind">첨부파일</strong></span>
      <div class="se-file-name-container"><span class="se-file-name">좋은 기업은 SWEET하다</span><span class="se-file-extension">.pdf</span></div>
      <a class="se-file-save-button __se_link" data-linktype="file" href="{url}"><span class="se-blind">파일 다운로드</span></a>
      </div></div>'''


def document(markdown):
    return {'title': '제목', 'blog_id': 'demo', 'post_id': '1', 'markdown': markdown,
            'images': [], 'sources': [], 'archived_at': '2026-09-10T00:00:00+00:00'}


def test_modern_file_component_is_extracted_once_in_body_order():
    result = extract_post(article('<p>앞 문단</p>' + modern() + '<p>뒤 문단</p>'), 'demo', '1')
    assert len(result['files']) == 1
    item = result['files'][0]
    assert item == {'token': item['token'], 'url': URL, 'name': NAME}
    assert result['markdown'] == f'앞 문단\n\n{item["token"]}\n\n뒤 문단'
    assert result['images'] == result['sources'] == []
    assert file_links_from_html(article(modern()), 'demo', '1') == [{'url': URL, 'name': NAME}]


def test_legacy_top_file_uses_complete_filename_instead_of_truncated_label():
    result = extract_post('<div id="postViewArea"><div class="wrap_file_area">'
                          f'<a class="file_name_area" title="파일 다운로드" href="{OLD_URL}">'
                          '<span class="file_name">repo...pdf</span><span class="btn_file_down"></span></a>'
                          '</div><p>본문</p></div>', 'demo', '1')
    assert result['files'][0]['name'] == 'report.pdf'
    assert result['markdown'] == result['files'][0]['token'] + '\n\n본문'


def test_explicit_file_link_data_is_used_when_href_is_javascript():
    html = article('<div class="se-module-file"><a data-linktype="file" href="javascript:void(0)" '
                   f"data-linkdata='{json.dumps({'link': URL})}'>파일 다운로드</a></div>")
    assert extract_post(html, 'demo', '1')['files'][0]['url'] == URL


def test_file_components_take_priority_over_legacy_citation_wrappers():
    result = extract_post(article('<div class="se_file"><div class="se_sectionArea">'
                                  f'<a href="{OLD_URL}">첨부</a></div></div>'), 'demo', '1')
    assert len(result['files']) == 1
    assert result['sources'] == []


def test_multiple_repeated_files_get_distinct_tokens_in_order():
    result = extract_post(article(modern() + '<p>사이</p>' + modern()), 'demo', '1')
    assert len(result['files']) == 2
    first, second = result['files']
    assert first['url'] == second['url']
    assert result['markdown'] == f'{first["token"]}\n\n사이\n\n{second["token"]}'
    assert first['token'] != second['token']


def test_generic_external_pdf_and_preview_photo_remain_unchanged():
    result = extract_post(article('<p><a href="https://example.com/paper.pdf">논문 참고</a></p>'
                                  f'<a href="{URL}"><img src="/preview.png"></a>'
                                  f'<div class="se-oglink"><a href="{URL}">파일 소개 카드</a></div>'), 'demo', '1')
    assert result['files'] == []
    assert '[논문 참고](https://example.com/paper.pdf)' in result['markdown']
    assert len(result['images']) == len(result['sources']) == 1


def test_explicit_external_download_is_supported_but_ordinary_url_is_not():
    result = extract_post(article('<a href="https://example.com/archive.zip" download="자료.zip">자료 받기</a>'), 'demo', '1')
    assert result['files'][0]['url'] == 'https://example.com/archive.zip'
    assert result['files'][0]['name'] == '자료.zip'


def test_authored_html_code_example_is_not_extracted_as_a_file():
    result = extract_post(article(f'<pre><code><a href="{URL}">예제</a></code></pre>'), 'demo', '1')
    assert result['files'] == []


@pytest.mark.parametrize('url', ['javascript:alert(1)', 'file:///C:/private.pdf',
                                'https://user@download.blog.naver.com/file.pdf', 'http://[', ''])
def test_invalid_file_url_is_never_promoted(url):
    result = extract_post(article(modern(url)), 'demo', '1')
    assert result['files'] == []
    assert '파일 다운로드' in result['markdown']


def test_cached_modern_file_is_recovered_without_changing_other_content_or_metadata():
    source = document(f'앞 문단\n\n**첨부파일**\n\n{NAME}\n\n[파일 다운로드]({URL})\n\n뒤 문단')
    before = deepcopy(source)
    result, count = upgrade_file_links(source)
    assert count == 1
    assert source == before
    item = result['files'][0]
    assert item['name'] == NAME and item['url'] == URL
    assert result['markdown'] == f'앞 문단\n\n{item["token"]}\n\n뒤 문단'
    assert result['archived_at'] == source['archived_at']
    assert result['images'] == source['images'] and result['sources'] == source['sources']
    assert upgrade_file_links(result) == (result, 0)


def test_cached_legacy_title_attribute_and_truncated_label_use_full_url_name():
    result, count = upgrade_file_links(document(f'[repo...pdf]({OLD_URL} "파일 다운로드")'))
    assert count == 1
    assert result['files'][0]['name'] == 'report.pdf'
    assert result['markdown'] == result['files'][0]['token']


def test_cached_escaped_filename_and_windows_newlines():
    filename = '[1] report_name (2).xlsx'
    url = URL.rsplit('/', 1)[0] + '/' + quote(filename)
    text = '**첨부파일**\r\n\r\n' + r'\[1\] report\_name (2).xlsx' + f'\r\n\r\n[파일 다운로드]({url})'
    result, count = upgrade_file_links(document(text))
    assert count == 1
    assert result['files'][0]['name'] == filename
    assert result['markdown'] == result['files'][0]['token']


@pytest.mark.parametrize('template', [
    '```markdown\n[파일 다운로드]({url})\n```', '~~~\n[파일 다운로드]({url})\n~~~',
    '    [파일 다운로드]({url})', '`[파일 다운로드]({url})`',
    '본문 속 [파일 다운로드]({url}) 링크', '![사진]({url})',
    '> [파일 다운로드]({url})', '- [파일 다운로드]({url})',
    '[**제목**\n\n설명\n\ndownload.blog.naver.com]({url})',
    '[논문](https://example.com/paper.pdf)',
])
def test_ambiguous_or_ordinary_cached_content_is_preserved(template):
    source = document(template.format(url=URL))
    assert upgrade_file_links(source) == (source, 0)


def test_cached_filename_header_is_preserved_if_not_the_actual_widget_name():
    result, count = upgrade_file_links(document(f'**첨부파일**\n\n작성자가 적은 설명\n\n[파일 다운로드]({URL})'))
    assert count == 1
    assert result['markdown'].startswith('**첨부파일**\n\n작성자가 적은 설명\n\n')


def test_multiple_cached_files_preserve_existing_files_and_relative_order():
    source = document(f'EXISTINGFILE\n\n[파일 다운로드]({URL})\n\n중간\n\n[old]({OLD_URL})')
    source['files'] = [{'token': 'EXISTINGFILE', 'url': 'https://example.com/file.pdf', 'name': 'existing.pdf'}]
    result, count = upgrade_file_links(source)
    assert count == 2
    assert result['files'][0] == source['files'][0]
    assert result['markdown'] == f'EXISTINGFILE\n\n{result["files"][1]["token"]}\n\n중간\n\n{result["files"][2]["token"]}'


@pytest.mark.parametrize('url', [URL, OLD_URL])
def test_expiring_naver_signature_does_not_change_resource_identity(url):
    assert attachment_identity(url) == attachment_identity(url.replace(SIGNATURE, '0123456789abcdef' * 3))
    assert attachment_identity(url) != attachment_identity(url.replace('.pdf', '.xlsx'))
    assert attachment_identity(url + '?x=1') != attachment_identity(url + '?x=2')


@pytest.mark.parametrize('url', ['https://example.com/' + SIGNATURE + '/folder/file.pdf',
                                'https://download.blog.naver.com/ordinary/folder/file.pdf'])
def test_unknown_url_layout_does_not_lose_path_segments(url):
    assert attachment_identity(url) == url


@pytest.mark.parametrize('url', ['https://download.blog.naver.com.example.com/file.pdf',
                                'https://example.com/file.pdf', 'http://[', 'javascript:bad'])
def test_host_validation_excludes_lookalikes(url):
    assert not is_naver_attachment(url)


def test_metadata_extraction_rejects_missing_body():
    with pytest.raises(ValueError, match='본문 영역'):
        file_links_from_html('<title>비공개</title>', 'demo', '1')
