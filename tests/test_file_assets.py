from dataclasses import replace
import hashlib
from io import BytesIO
import os
from pathlib import Path
from urllib.parse import quote
from zipfile import ZipFile

import pytest
import requests

from naver_blog_archive.config import Config
from naver_blog_archive.file_assets import FileAssets
from naver_blog_archive.progress import OperationCancelled, TaskControl
from naver_blog_archive.state import State


URL = 'https://download.blog.naver.com/resource/report.pdf'
REFERER = 'https://m.blog.naver.com/demo/123'


def pdf_bytes():
    objects = [b'<</Type /Catalog /Pages 2 0 R>>',
               b'<</Type /Pages /Kids [3 0 R] /Count 1>>',
               b'<</Type /Page /Parent 2 0 R /MediaBox [0 0 100 100]>>']
    data, offsets = b'%PDF-1.4\n', [0]
    for number, body in enumerate(objects, 1):
        offsets.append(len(data))
        data += f'{number} 0 obj\n'.encode() + body + b'\nendobj\n'
    xref = len(data)
    data += b'xref\n0 4\n0000000000 65535 f \n'
    data += b''.join(f'{offset:010d} 00000 n \n'.encode() for offset in offsets[1:])
    return data + f'trailer\n<</Size 4 /Root 1 0 R>>\nstartxref\n{xref}\n%%EOF\n'.encode()


PDF = pdf_bytes()


class Response:
    def __init__(self, content=PDF, *, headers=None, status=200, chunks=None):
        self.content = content
        if headers is not None:
            self.headers = headers
        self.status_code = status
        self.chunks = chunks
        self.closed = False
        self.iterated = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def iter_content(self, size):
        self.iterated = True
        if self.chunks is not None:
            yield from self.chunks
        else:
            for offset in range(0, len(self.content), size):
                yield self.content[offset:offset + size]


class Client:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses[url]
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture
def archive(tmp_path):
    config = Config('demo', tmp_path / 'archive')
    state = State(config.out_dir)
    try:
        yield config, state
    finally:
        state.close()


def output_files(config):
    return [path for path in (config.out_dir / 'attachments').rglob('*') if path.is_file()]


def test_pdf_keeps_readable_name_and_request_referer(archive):
    config, state = archive
    response = Response(headers={'Content-Type': 'application/octet-stream'})
    client = Client({URL: response})
    relative = FileAssets(config, state, client).obtain(URL, REFERER, '투자 보고서.pdf')
    assert relative == f'attachments/files/{hashlib.sha256(PDF).hexdigest()}/투자 보고서.pdf'
    assert (config.out_dir / relative).read_bytes() == PDF
    assert dict(state.asset(URL)) == dict(url=URL, path=relative, file_hash=hashlib.sha256(PDF).hexdigest(),
                                         status='success', error='')
    assert client.calls == [(URL, {'headers': {'Referer': REFERER}, 'stream': True})]
    assert response.closed
    assert len(output_files(config)) == 1


def test_zip_is_saved_without_extracting_its_entries(archive):
    config, state = archive
    stream = BytesIO()
    with ZipFile(stream, 'w') as zipped:
        zipped.writestr('보고서.txt', '본문')
        zipped.writestr('../../outside.txt', '압축 해제하면 안 됩니다.')
    binary = stream.getvalue()
    client = Client({URL: Response(binary)})
    relative = FileAssets(config, state, client).obtain(URL, REFERER, '자료.zip')
    assert (config.out_dir / relative).read_bytes() == binary
    assert [path.name for path in output_files(config)] == ['자료.zip']
    assert not (config.out_dir.parent / 'outside.txt').exists()


def test_saved_file_is_reused_across_runs_without_request_or_mtime_change(archive):
    config, state = archive
    client = Client({URL: Response()})
    relative = FileAssets(config, state, client).obtain(URL, REFERER, '보고서.pdf')
    modified = (config.out_dir / relative).stat().st_mtime_ns
    assert FileAssets(config, state, Client({})).obtain(URL, REFERER, '새 이름.pdf') == relative
    assert (config.out_dir / relative).stat().st_mtime_ns == modified


def test_signed_url_changes_reuse_stable_asset_key_without_request(archive):
    config, state = archive
    key = 'file:resource/report.pdf'
    relative = FileAssets(config, state, Client({URL: Response()})).obtain(
        URL, REFERER, '보고서.pdf', asset_key=key)
    assert FileAssets(config, state, Client({})).obtain(
        URL + '?signature=NEW', REFERER, '보고서.pdf', asset_key=key) == relative
    assert state.asset(URL) is None
    assert state.asset(key)['path'] == relative


def test_refreshed_signature_can_retry_same_key_in_same_run(archive):
    config, state = archive
    fresh = URL + '?signature=NEW'
    client = Client({URL: Response(status=403), fresh: Response()})
    assets = FileAssets(config, state, client)
    assert assets.obtain(URL, REFERER, '자료.pdf', asset_key='file:report') is None
    relative = assets.obtain(fresh, REFERER, '자료.pdf', asset_key='file:report')
    assert relative and state.asset('file:report')['status'] == 'success'
    assert len(client.calls) == 2


def test_identical_files_from_different_urls_and_names_share_one_file(archive):
    config, state = archive
    second = URL + '?copy=2'
    client = Client({URL: Response(), second: Response()})
    assets = FileAssets(config, state, client)
    first = assets.obtain(URL, REFERER, '첫 보고서.pdf')
    modified = (config.out_dir / first).stat().st_mtime_ns
    assert assets.obtain(second, REFERER, '다른 이름.pdf') == first
    assert state.asset(second)['path'] == first
    assert len(output_files(config)) == 1
    assert (config.out_dir / first).stat().st_mtime_ns == modified


def test_different_files_with_same_name_do_not_overwrite_each_other(archive):
    config, state = archive
    second = URL + '?version=2'
    client = Client({URL: Response(), second: Response(PDF + b'\n% second revision')})
    assets = FileAssets(config, state, client)
    first = assets.obtain(URL, REFERER, '보고서.pdf')
    other = assets.obtain(second, REFERER, '보고서.pdf')
    assert first != other and Path(first).name == Path(other).name == '보고서.pdf'
    assert (config.out_dir / first).read_bytes() == PDF
    assert (config.out_dir / other).read_bytes() == PDF + b'\n% second revision'


def test_missing_saved_file_is_downloaded_again(archive):
    config, state = archive
    client = Client({URL: Response()})
    relative = FileAssets(config, state, client).obtain(URL, REFERER, '자료.pdf')
    (config.out_dir / relative).unlink()
    assert FileAssets(config, state, client).obtain(URL, REFERER, '자료.pdf') == relative
    assert (config.out_dir / relative).read_bytes() == PDF
    assert len(client.calls) == 2


@pytest.mark.parametrize('failure', [Response(status=403), requests.ConnectionError('offline')])
def test_failed_repair_keeps_metadata_and_manual_restore_needs_no_request(archive, failure):
    config, state = archive
    key = 'file:report.pdf'
    relative = FileAssets(config, state, Client({URL: Response()})).obtain(
        URL, REFERER, '자료.pdf', asset_key=key)
    path = config.out_dir / relative
    checksum = state.asset(key)['file_hash']
    path.unlink()
    assert FileAssets(config, state, Client({URL: failure})).obtain(
        URL, REFERER, '자료.pdf', asset_key=key) is None
    failed = state.asset(key)
    assert failed['status'] == 'failed'
    assert failed['path'] == relative and failed['file_hash'] == checksum
    path.write_bytes(PDF)
    restored_mtime = path.stat().st_mtime_ns
    client = Client({})
    assert FileAssets(config, state, client).obtain(
        URL, REFERER, '자료.pdf', asset_key=key) == relative
    assert client.calls == []
    assert state.asset(key)['status'] == 'success' and not state.asset(key)['error']
    assert path.stat().st_mtime_ns == restored_mtime


def test_modified_saved_file_is_protected_when_redownload_would_overwrite_it(archive):
    config, state = archive
    client = Client({URL: Response()})
    relative = FileAssets(config, state, client).obtain(URL, REFERER, '자료.pdf')
    path = config.out_dir / relative
    path.write_bytes(b'user changes')
    assert FileAssets(config, state, client).obtain(URL, REFERER, '자료.pdf') is None
    assert path.read_bytes() == b'user changes'
    assert '기존 파일을 보호' in state.asset(URL)['error']
    assert len(output_files(config)) == 1


def test_unregistered_collision_is_preserved(archive):
    config, state = archive
    path = config.out_dir / f'attachments/files/{hashlib.sha256(PDF).hexdigest()}/자료.pdf'
    path.parent.mkdir(parents=True)
    path.write_bytes(b'unrelated')
    result = FileAssets(config, state, Client({URL: Response()})).obtain(URL, REFERER, '자료.pdf')
    assert result is None
    assert path.read_bytes() == b'unrelated'
    assert '기존 파일을 보호' in state.asset(URL)['error']


@pytest.mark.parametrize('filename, expected', [
    ('../../secret.pdf', 'secret.pdf'),
    (r'C:\Users\someone\자료.pdf', '자료.pdf'),
    ('NUL.pdf', '_NUL.pdf'),
    ('CON .pdf', '_CON.pdf'),
    ('COM¹.txt', '_COM¹.txt'),
    ('자료<>:"|?*\x00.pdf', '자료.pdf'),
    ('  .. 보고서.pdf.  ', '보고서.pdf'),
    ('자료\u202e.txt', '자료.txt'),
])
def test_server_names_are_safe_on_windows(archive, filename, expected):
    config, state = archive
    relative = FileAssets(config, state, Client({URL: Response()})).obtain(URL, REFERER, filename)
    path = (config.out_dir / relative).resolve()
    assert path.is_relative_to(config.out_dir)
    assert path.name == expected
    assert path.read_bytes() == PDF


def test_unicode_long_name_preserves_extension_and_windows_path_budget(archive):
    config, state = archive
    relative = FileAssets(config, state, Client({URL: Response()})).obtain(
        URL, REFERER, '한글📄' * 100 + '.pdf')
    path = config.out_dir / relative
    assert path.name.startswith('한글📄') and path.suffix == '.pdf'
    assert len(str(path).encode('utf-16-le')) // 2 <= 240
    assert len(path.name.encode('utf-16-le')) // 2 <= 120


@pytest.mark.parametrize('header', [
    'attachment; filename="' + quote('원본 보고서.pdf') + '"',
    "attachment; filename*=UTF-8''" + quote('원본 보고서.pdf'),
    "attachment; filename=\"fallback.pdf\"; filename*=UTF-8''" + quote('원본 보고서.pdf'),
])
def test_content_disposition_supplies_real_unicode_name(archive, header):
    config, state = archive
    response = Response(headers={'content-disposition': header})
    relative = FileAssets(config, state, Client({URL: response})).obtain(URL, REFERER, '다운로드')
    assert Path(relative).name == '원본 보고서.pdf'


def test_url_filename_is_used_when_name_and_disposition_are_absent(archive):
    config, state = archive
    url = 'https://example.test/' + quote('파일 이름.pdf')
    relative = FileAssets(config, state, Client({url: Response()})).obtain(url, REFERER, '')
    assert Path(relative).name == '파일 이름.pdf'


def test_header_size_limit_prevents_reading_response_body(archive):
    config, state = archive
    response = Response(headers={'Content-Length': str(1024 * 1024 + 1)})
    result = FileAssets(replace(config, max_file_mb=1), state, Client({URL: response})).obtain(
        URL, REFERER, '자료.pdf')
    assert result is None and not response.iterated and response.closed
    assert '크기 제한' in state.asset(URL)['error']
    assert output_files(config) == []


def test_stream_size_limit_applies_when_header_is_missing(archive):
    config, state = archive
    response = Response(chunks=[b'X' * 65536] * 17)
    result = FileAssets(replace(config, max_file_mb=1), state, Client({URL: response})).obtain(
        URL, REFERER, '자료.bin')
    assert result is None and response.closed
    assert '크기 제한' in state.asset(URL)['error']
    assert output_files(config) == []


def test_response_exactly_at_size_limit_is_allowed(archive):
    config, state = archive
    response = Response(b'A' * (1024 * 1024))
    relative = FileAssets(replace(config, max_file_mb=1), state, Client({URL: response})).obtain(
        URL, REFERER, '자료.bin')
    assert (config.out_dir / relative).stat().st_size == 1024 * 1024


def test_stream_cancel_cleans_only_its_own_temporary_and_propagates(archive):
    config, state = archive
    root = config.out_dir / 'attachments/files'
    root.mkdir(parents=True)
    unrelated = root / '.someone-else.tmp'
    unrelated.write_bytes(b'keep')
    control = TaskControl()

    def chunks():
        yield PDF
        control.cancel()
        yield b'later'

    response = Response(chunks=chunks())
    with pytest.raises(OperationCancelled):
        FileAssets(config, state, Client({URL: response}), control).obtain(URL, REFERER, '자료.pdf')
    assert response.closed and state.asset(URL) is None
    assert output_files(config) == [unrelated]
    assert unrelated.read_bytes() == b'keep'


def test_cancel_before_request_does_not_touch_state(archive):
    config, state = archive
    control, client = TaskControl(), Client({})
    control.cancel()
    with pytest.raises(OperationCancelled):
        FileAssets(config, state, client, control).obtain(URL, REFERER, '자료.pdf')
    assert not client.calls and state.asset(URL) is None


@pytest.mark.parametrize('response, message', [
    (Response(status=404), 'HTTP 404'),
    (Response(b''), '비어'),
    (Response(b'<html><form>Login required</form></html>'), 'HTML'),
    (Response(b'<!DOCTYPE html><title>404</title>'), 'HTML'),
    (Response(b'<!-- error -->\n<html><body>Login required</body></html>'), 'HTML'),
    (Response('<html><body>Login required</body></html>'.encode('utf-16')), 'HTML'),
    (Response(b'Access denied', headers={'Content-Type': 'text/html; charset=utf-8'}), 'HTML'),
    (Response(headers={'Content-Length': str(len(PDF) + 1)}), '불완전'),
])
def test_errors_are_recorded_without_saving_fake_files(archive, response, message):
    config, state = archive
    result = FileAssets(config, state, Client({URL: response})).obtain(URL, REFERER, '자료.pdf')
    assert result is None and response.closed
    assert state.asset(URL)['status'] == 'failed'
    assert message in state.asset(URL)['error']
    assert output_files(config) == []


@pytest.mark.parametrize('name, headers', [
    ('자료.html', {'Content-Type': 'text/html'}),
    ('다운로드', {'Content-Type': 'text/html', 'Content-Disposition': 'attachment; filename="report.htm"'}),
])
def test_explicit_html_attachment_is_saved_as_data(archive, name, headers):
    config, state = archive
    body = b'<!doctype html><html><body>Saved report</body></html>'
    relative = FileAssets(config, state, Client({URL: Response(body, headers=headers)})).obtain(
        URL, REFERER, name)
    assert (config.out_dir / relative).read_bytes() == body


def test_compressed_content_length_is_not_compared_to_decompressed_bytes(archive):
    config, state = archive
    response = Response(headers={'Content-Encoding': 'gzip', 'Content-Length': str(len(PDF) - 100)})
    assert FileAssets(config, state, Client({URL: response})).obtain(URL, REFERER, '자료.pdf')


def test_network_failure_after_partial_data_leaves_no_download_file(archive):
    config, state = archive

    def chunks():
        yield PDF[:100]
        raise requests.ConnectionError('connection lost')

    response = Response(chunks=chunks())
    assert FileAssets(config, state, Client({URL: response})).obtain(URL, REFERER, '자료.pdf') is None
    assert response.closed and 'connection lost' in state.asset(URL)['error']
    assert output_files(config) == []


def test_repeated_failed_url_attempts_once_per_run_and_retries_next_run(archive):
    config, state = archive
    client = Client({URL: Response(status=403)})
    assets = FileAssets(config, state, client)
    assert assets.obtain(URL, REFERER, '자료.pdf') is None
    assert assets.obtain(URL, REFERER, '자료.pdf') is None
    assert len(client.calls) == 1
    client.responses[URL] = Response()
    assert FileAssets(config, state, client).obtain(URL, REFERER, '자료.pdf')
    assert len(client.calls) == 2


@pytest.mark.parametrize('url', ['file:///C:/private.pdf', 'data:text/plain,secret', 'javascript:alert(1)', 'http://['])
def test_non_http_and_invalid_urls_do_not_reach_client(archive, url):
    config, state = archive
    client = Client({})
    assert FileAssets(config, state, client).obtain(url, REFERER, '자료.pdf') is None
    assert not client.calls and state.asset(url)['status'] == 'failed'


def test_disabled_downloads_make_no_requests_or_failures(archive):
    config, state = archive
    client = Client({})
    assert FileAssets(replace(config, download_files=False), state, client).obtain(URL, REFERER, '자료.pdf') is None
    assert not client.calls and state.asset(URL) is None


def test_untrusted_database_path_cannot_reuse_or_change_file_outside_archive(archive):
    config, state = archive
    outside = config.out_dir.parent / 'outside.pdf'
    outside.write_bytes(PDF)
    state.save_asset(URL, '../outside.pdf', hashlib.sha256(PDF).hexdigest(), 'success')
    relative = FileAssets(config, state, Client({URL: Response()})).obtain(URL, REFERER, '자료.pdf')
    assert relative.startswith('attachments/files/')
    assert (config.out_dir / relative).is_relative_to(config.out_dir)
    assert outside.read_bytes() == PDF


def test_percent_character_in_rfc5987_filename_is_decoded_once(archive):
    config, state = archive
    response = Response(headers={'Content-Disposition': "attachment; filename*=UTF-8''rate%2520.pdf"})
    relative = FileAssets(config, state, Client({URL: response})).obtain(URL, REFERER, '자료.pdf')
    assert Path(relative).name == 'rate%20.pdf'


def test_atomic_publish_preserves_file_created_by_another_process(archive, monkeypatch):
    config, state = archive
    original = os.link

    def raced_link(source, target):
        target.write_bytes(b'created while downloading')
        original(source, target)

    monkeypatch.setattr('naver_blog_archive.file_assets.os.link', raced_link)
    assert FileAssets(config, state, Client({URL: Response()})).obtain(URL, REFERER, '자료.pdf') is None
    assert len(output_files(config)) == 1
    assert output_files(config)[0].read_bytes() == b'created while downloading'


@pytest.mark.skipif(os.name != 'nt', reason='Windows rename guarantees no replacement')
def test_windows_filesystem_without_hard_links_can_save_atomically(archive, monkeypatch):
    config, state = archive

    def no_hard_links(*args):
        raise OSError('Filesystem does not support hard links')

    monkeypatch.setattr('naver_blog_archive.file_assets.os.link', no_hard_links)
    relative = FileAssets(config, state, Client({URL: Response()})).obtain(URL, REFERER, '자료.pdf')
    assert (config.out_dir / relative).read_bytes() == PDF
    assert len(output_files(config)) == 1
