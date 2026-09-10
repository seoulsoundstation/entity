"""Run archive operations without calling Tk from a worker thread."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from queue import Queue
from threading import Thread

from .config import Config
from .progress import TaskControl


class JobRunner:
    def __init__(self):
        self.events: Queue[dict] = Queue()
        self._thread: Thread | None = None
        self.control: TaskControl | None = None

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, action: str, config: Config | None = None, *, refresh=False):
        if self.busy:
            raise RuntimeError('이미 작업을 실행하고 있습니다.')
        if action not in ('backup', 'verify', 'status', 'doctor', 'reformat'):
            raise ValueError('지원하지 않는 작업입니다.')
        if action != 'doctor' and config is None:
            raise ValueError('백업 설정이 필요합니다.')
        self.control = TaskControl(lambda event: self.events.put({'kind': 'progress', **event}))
        self._thread = Thread(target=self._work, args=(action, config, refresh, self.control),
                              name='archive-worker', daemon=False)
        self._thread.start()

    def cancel(self):
        if self.control:
            self.control.cancel()

    def _work(self, action, config, refresh, control):
        report = None
        try:
            if action == 'doctor':
                from .cli import doctor
                output = StringIO()
                with redirect_stdout(output), redirect_stderr(output):
                    code = doctor()
                control.emit('doctor', output.getvalue().strip())
                status = 'success' if code == 0 else 'error'
                message = '실행 환경이 준비되었습니다.' if code == 0 else '실행 환경을 확인하세요. 아래 실행 기록에 해결 방법이 표시됩니다.'
            else:
                from .archive import backup, inspect_archive, reformat_archive
                control.check()
                if action == 'backup':
                    report = backup(config, refresh=refresh, control=control)
                    status = report['last_run']['status']
                    message = '백업을 완료했습니다.' if status == 'success' else '백업이 일부 완료되었습니다. 실패 내역을 확인한 뒤 다시 백업하세요.'
                elif action == 'reformat':
                    report = reformat_archive(config, control=control)
                    counts = report['output_counts']
                    status = 'partial' if report.get('output_problems') or report.get('failures') else 'success'
                    message = (f'파일명 {counts["renamed"]:,}개 변경 · Markdown {counts["updated"]:,}회 갱신. '
                               + (f'블로그 메뉴 {counts["navigation_posts"]:,}개 글에서 제거. ' if counts.get('navigation_posts') else '')
                               + (f'링크 카드 {counts["cards"]:,}개 정리. ' if counts.get('cards') else '')
                               + ('확인할 항목을 확인하세요. 누락된 이미지는 백업 시 재시도합니다.' if status == 'partial'
                                  else '사진과 함께 읽기는 저장 글 검색에서 이용하세요.'))
                else:
                    report = inspect_archive(config, verify=action == 'verify', control=control)
                    if action == 'verify':
                        last = report.get('last_run') or {}
                        incomplete = (report.get('verification_problems') or report.get('failures')
                                      or not last.get('listing_complete'))
                        status = 'partial' if incomplete else 'success'
                        message = '복구 또는 추가 백업이 필요한 항목이 있습니다.' if incomplete else '저장 파일 검사를 통과했습니다.'
                    else:
                        status = 'success'
                        message = '저장된 백업 상태를 불러왔습니다.'
        except KeyboardInterrupt as exc:
            report = getattr(exc, 'report', None)
            status, message = 'interrupted', '중단했습니다. 같은 설정으로 백업하면 미완료 항목을 이어 처리합니다.'
        except (Exception, SystemExit) as exc:
            status, message = 'error', f'{type(exc).__name__}: {exc}'
        self.events.put({'kind': 'done', 'action': action, 'status': status,
                         'message': message, 'report': report})
