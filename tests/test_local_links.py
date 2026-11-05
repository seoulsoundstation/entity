import os
import re
from urllib.parse import quote, unquote

import pytest

from naver_blog_archive.archive import reformat_archive
from naver_blog_archive.config import Config
from naver_blog_archive.parser import local_href
from test_archive import FakeRenderer, saved_path
from test_video_archive import KEY, completed, video_page
from test_video_files import media, run


TITLE = '차트와 추세, 파동 + R&D @ 1;2=3 $'
# JavaScript's decodeURI keeps the escapes of these characters. Obsidian uses it
# to turn a local Markdown link destination into a vault path.
RESERVED_ESCAPE = re.compile(r'%(?:3B|2F|3F|3A|40|26|3D|2B|24|2C|23)', re.I)


def decode_uri(value: str) -> str:
    parts = RESERVED_ESCAPE.split(value)
    escapes = RESERVED_ESCAPE.findall(value)
    result = unquote(parts[0])
    for escape, part in zip(escapes, parts[1:]):
        result += escape + unquote(part)
    return result


@pytest.fixture
def config(tmp_path):
    return Config(KEY[0], tmp_path / 'archive', delay=0, retries=1, download_videos=True)


@pytest.fixture(autouse=True)
def transcript(monkeypatch):
    monkeypatch.setattr('naver_blog_archive.archive.transcribe_video', completed)


def target(note, href, decode=None):
    # Normalize first: the unresolved '..' form can exceed Windows MAX_PATH.
    return type(note)(os.path.normpath(note.parent / (decode or decode_uri)(href)))


def video_link(config):
    note = saved_path(config, KEY)
    match = re.search(r'\[동영상 파일: [^\]]+\]\(([^)]+)\)', note.read_text(encoding='utf-8'))
    return note, match[1]


def test_local_href_writes_reserved_characters_literally(tmp_path):
    href = local_href(tmp_path / 'attachments' / 'videos' / f'{TITLE} (2).mp4', tmp_path / 'posts' / 'demo')
    assert href.startswith('../../attachments/videos/')
    assert not RESERVED_ESCAPE.search(href)
    assert ', ' not in href and ',%20' in href and '+' in href and '&' in href and '$' in href
    assert '%28' in href and '%29' in href
    assert decode_uri(href) == unquote(href) == f'../../attachments/videos/{TITLE} (2).mp4'


def test_hash_and_question_mark_stay_escaped(tmp_path):
    href = local_href(tmp_path / 'a#b.md', tmp_path)
    assert href == 'a%23b.md'


def test_video_named_with_comma_and_plus_resolves_in_obsidian(config, monkeypatch):
    media(monkeypatch)
    report, _ = run(config, renderer=FakeRenderer({KEY: video_page().replace('영상 제목', TITLE)}))
    assert report['posts'] == {'success': 1}
    note, link = video_link(config)
    assert target(note, link).is_file() and target(note, link, unquote).is_file()
    assert target(note, link).name == TITLE + '.mp4'


def test_reformat_replaces_links_saved_with_reserved_escapes(config, monkeypatch):
    media(monkeypatch)
    old = lambda path, base: quote(local_href(path, base), safe='/%')
    monkeypatch.setattr('naver_blog_archive.archive.local_href', old)
    run(config, renderer=FakeRenderer({KEY: video_page().replace('영상 제목', TITLE)}))
    note, link = video_link(config)
    assert '%2C' in link and not target(note, link).exists()
    monkeypatch.undo()
    monkeypatch.setattr('naver_blog_archive.archive.transcribe_video', completed)
    report = reformat_archive(config)
    assert report['output_counts']['updated'] == 1
    note, link = video_link(config)
    assert target(note, link).is_file()
    assert list((config.out_dir / 'history').rglob('*.md'))
