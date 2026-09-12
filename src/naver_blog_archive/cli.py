from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path
import runpy
import sqlite3
import sys


def doctor() -> int:
    good = True
    for name in ('requests', 'bs4', 'markdownify', 'playwright.sync_api', 'PIL'):
        try:
            importlib.import_module(name)
            print(f'OK      {name}')
        except Exception as exc:
            good = False
            print(f'MISSING {name}: {exc}')
    if not good:
        print('프로젝트 폴더에서 python -m pip install -e ".[dev]"를 실행하세요.')
        return 1
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            browser.close()
        print('OK      Chromium 실행')
        return 0
    except Exception as exc:
        print(f'FAIL    Chromium: {exc}\npython -m playwright install chromium을 실행하세요.')
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='naver-blog-archive', description='네이버 블로그 Markdown 백업 및 복구')
    parser.add_argument('--version', action='version', version='%(prog)s 0.2.0')
    subs = parser.add_subparsers(dest='command', required=True)
    subs.add_parser('doctor', help='Python 의존성과 Chromium 실행 검사')
    subs.add_parser('prepare-transcription', help='로컬 음성 인식 패키지와 small 모델 준비 (최초 인터넷 다운로드)')
    gui = subs.add_parser('gui', help='블로그 백업 프로그램 창 열기')
    gui.add_argument('--config', type=Path, default=Path('config.toml'), help='불러올 설정 파일 (기본: config.toml)')
    for name, help_text in (('backup', '신규 글 백업 및 미완료 작업 재개'),
                            ('login', '전용 브라우저에서 네이버 프리미엄콘텐츠 로그인'),
                            ('reformat', '재다운로드 없이 저장 파일명을 제목으로 변경하고 Markdown 링크 정리'),
                            ('status', '마지막 실행 결과와 실패 항목 확인'),
                            ('verify', '저장 파일의 누락·변경 및 출처 연결 검사')):
        command = subs.add_parser(name, help=help_text)
        command.add_argument('--config', type=Path, default=Path('config.toml'), help='설정 파일 (기본: config.toml)')
        if name == 'backup':
            command.add_argument('--refresh', action='store_true', help='기존 글의 본문도 다시 수집 (사용자 수정 파일은 보호)')
    legacy = subs.add_parser('legacy', help='개발 저장소의 보존 스크립트 실행 (복구 기능 없음)')
    legacy.add_argument('--script', type=Path, help='보존 스크립트의 명시적 경로')
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == 'doctor':
            return doctor()
        if args.command == 'prepare-transcription':
            from .progress import TaskControl
            from .transcription import prepare_transcription
            prepare_transcription(control=TaskControl(lambda event: print(event.get('message', ''), flush=True)))
            print('로컬 음성 인식 준비가 끝났습니다. backup 명령으로 영상 텍스트화를 시작하세요.')
            return 0
        if args.command == 'gui':
            from .gui import launch
            return launch(config_path=args.config)
        if args.command == 'legacy':
            script = args.script or Path(__file__).resolve().parents[2] / 'legacy' / 'naver_blog_archive.py'
            if not script.is_file():
                raise ValueError('legacy는 개발 저장소용입니다. 정식 backup 명령 또는 --script 경로를 사용하세요.')
            print('기존 스크립트의 고정 설정으로 실행합니다. 설정·상태 저장은 backup 명령을 사용하세요.')
            runpy.run_path(str(script), run_name='__main__')
            return 0
        from .config import load_config
        from .archive import backup, inspect_archive, reformat_archive
        config = load_config(args.config)
        if args.command == 'login':
            from .premium_auth import login
            login(config)
            print('네이버 로그인을 저장했습니다. backup 명령으로 백업하세요.')
            return 0
        if args.command == 'backup':
            report = backup(config, refresh=args.refresh)
        elif args.command == 'reformat':
            report = reformat_archive(config)
        else:
            report = inspect_archive(config, args.command == 'verify')
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if args.command == 'backup':
            return 0 if report['last_run']['status'] == 'success' else 1
        if args.command == 'verify':
            return 1 if report['verification_problems'] or report['failures'] or not report['last_run'] or not report['last_run']['listing_complete'] else 0
        if args.command == 'reformat':
            return 1 if report['output_problems'] or report['failures'] else 0
        return 0
    except KeyboardInterrupt:
        message = ('음성 인식 준비를 중단했습니다. prepare-transcription 명령으로 다시 실행하세요.'
                   if args.command == 'prepare-transcription'
                   else '중단했습니다. 같은 backup 명령을 다시 실행하면 미완료 작업을 재개합니다.')
        print('\n' + message, file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError, ImportError, sqlite3.Error) as exc:
        print(f'오류: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
