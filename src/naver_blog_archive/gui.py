"""Korean desktop interface; all archive and browser work stays off the Tk thread."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime
import os
from pathlib import Path
from queue import Empty
import sqlite3
import subprocess
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .addresses import channel_url, is_premium
from .config import Config, config_from_mapping, load_config, normalize_blog_id, save_config
from .jobs import JobRunner
from .library_ui import LibraryPane
from .profiles import ProfileStore
from .ui_styles import install_control_styles


BG = '#f3f6fa'
INK = '#172c43'
MUTED = '#61748a'
GREEN = '#087f62'
STATUS_NAMES = {'success': '완료', 'partial': '부분 완료', 'failed': '실패',
                'pending': '대기', 'running': '진행 중', 'interrupted': '중단', 'error': '오류'}
ACTION_NAMES = {'backup': '백업', 'verify': '파일 검사', 'status': '상태 확인', 'doctor': '환경 진단',
                'reformat': '저장 결과 정리', 'login': '네이버 로그인',
                'prepare-transcription': '음성 인식 준비'}


def _source_label(blog_id: str) -> str:
    return channel_url(blog_id) if is_premium(blog_id) else blog_id


def open_folder(path: Path):
    if not path.is_dir():
        raise ValueError('저장 폴더가 아직 없습니다. 백업 후 다시 열어 주세요.')
    if sys.platform == 'win32':
        os.startfile(str(path))
    elif sys.platform == 'darwin':
        subprocess.Popen(['open', str(path)])
    else:
        subprocess.Popen(['xdg-open', str(path)])


class ArchiveApp:
    def __init__(self, root: tk.Tk, config_path: str | Path | None = None, *, runner=None):
        self.root = root
        self.config_path = Path(config_path or 'config.toml').expanduser().resolve()
        self.runner = runner or JobRunner()
        self.profile_store = ProfileStore(self.config_path.parent / 'blog_profiles.sqlite')
        self.saved_profiles = {}
        self.profile_choice = tk.StringVar()
        self.running = False
        self.stopping = False
        self.close_when_done = False
        self.current_action = None
        self.last_report = None
        self._progress_phase = None
        self._input_widgets = []
        self._advanced = asdict(Config('', self.config_path.parent / 'naver_blog_backup'))
        self.blog = tk.StringVar()
        self.folder = tk.StringVar(value=str(self.config_path.parent / 'naver_blog_backup'))
        self.images = tk.BooleanVar(value=True)
        self.files = tk.BooleanVar(value=True)
        self.videos = tk.BooleanVar(value=True)
        self.sources = tk.BooleanVar(value=True)
        self.refresh = tk.BooleanVar(value=False)
        self.login_note = tk.StringVar()
        self.phase = tk.StringVar(value='백업할 블로그 또는 채널을 입력해 주세요')
        self.detail = tk.StringVar(value='공개 블로그와 구독 중인 프리미엄 채널을 Markdown으로 저장합니다. 이미지·첨부파일도 함께 보관할 수 있습니다.')
        self.settings_note = tk.StringVar()
        self.run_summary = tk.StringVar(value='기본 백업: 기존 파일 확인 후 건너뛰기 · 새 글과 미완료 항목 저장')
        self.counts = {key: tk.StringVar(value='0') for key in ('success', 'partial', 'failed', 'pending')}
        self._build()
        if self.config_path.is_file():
            try:
                self._apply_config(load_config(self.config_path))
                self.phase.set('백업 준비가 되었습니다')
            except (OSError, ValueError) as exc:
                self.phase.set('설정 파일을 확인해 주세요')
                self.detail.set(str(exc))
                self._log(f'설정 불러오기 실패: {exc}')
        self._update_settings_note()
        self._reload_profiles()
        self.blog.trace_add('write', self._update_login_button)
        self._update_login_button()
        self.root.protocol('WM_DELETE_WINDOW', self.on_close)
        self.root.after(120, self._poll)

    def _build(self):
        self.root.title('네이버 콘텐츠 보관함')
        width = min(1120, self.root.winfo_screenwidth() - 60)
        height = min(860, self.root.winfo_screenheight() - 100)
        self.root.geometry(f'{width}x{height}+{max(0, (self.root.winfo_screenwidth() - width) // 2)}+30')
        self.root.minsize(min(1020, width), min(760, height))
        self.root.configure(bg=BG)
        self.root.option_add('*Font', ('맑은 고딕', 10))
        style = ttk.Style(self.root)
        style.theme_use('clam')
        style.configure('.', font=('맑은 고딕', 10), foreground=INK)
        style.configure('TFrame', background=BG)
        style.configure('Card.TFrame', background='white')
        style.configure('TLabel', background=BG, foreground=INK)
        style.configure('Card.TLabel', background='white')
        style.configure('Muted.TLabel', foreground=MUTED, background=BG)
        style.configure('CardMuted.TLabel', foreground=MUTED, background='white')
        style.configure('TButton', padding=(12, 8), background='white', borderwidth=1)
        style.map('TButton', background=[('active', '#e8edf3')], foreground=[('disabled', '#98a4b3')])
        style.configure('Primary.TButton', background=GREEN, foreground='white', borderwidth=0,
                        font=('맑은 고딕', 11, 'bold'), padding=(22, 10))
        style.map('Primary.TButton', background=[('disabled', '#b7cec7'), ('active', '#066950')],
                  foreground=[('disabled', 'white')])
        style.configure('TEntry', padding=7, fieldbackground='white')
        style.configure('TCheckbutton', background='white', padding=(0, 5))
        style.configure('Horizontal.TProgressbar', troughcolor='#e7edf2', background=GREEN,
                        bordercolor='#e7edf2', lightcolor=GREEN, darkcolor=GREEN)
        style.configure('TNotebook', background=BG, borderwidth=0)
        style.configure('TNotebook.Tab', padding=(18, 8))
        style.configure('Treeview', rowheight=30, fieldbackground='white', background='white', borderwidth=0)
        style.configure('Treeview.Heading', background='#e9eef5', padding=8)
        self.control_styles = install_control_styles(self.root, style)

        shell = ttk.Frame(self.root, padding=20)
        shell.pack(fill='both', expand=True)
        shell.columnconfigure(0, weight=3)
        shell.columnconfigure(1, weight=2)
        shell.rowconfigure(3, weight=1)
        header = ttk.Frame(shell)
        header.grid(row=0, column=0, columnspan=2, sticky='ew', pady=(0, 12))
        ttk.Label(header, text='네이버 콘텐츠 보관함', font=('맑은 고딕', 23, 'bold')).pack(side='left')
        ttk.Label(header, text='내 PC에 차곡차곡', style='Muted.TLabel').pack(side='right', pady=(14, 0))

        settings = ttk.Frame(shell, style='Card.TFrame', padding=14)
        settings.grid(row=1, column=0, sticky='nsew', padx=(0, 14))
        settings.columnconfigure(1, weight=1)
        ttk.Label(settings, text='01  백업 설정', style='Card.TLabel', font=('맑은 고딕', 12, 'bold')).grid(
            row=0, column=0, columnspan=3, sticky='w', pady=(0, 12))
        ttk.Label(settings, text='저장한 주소', style='Card.TLabel').grid(row=1, column=0, sticky='w', padx=(0, 16))
        self.profile_box = ttk.Combobox(settings, textvariable=self.profile_choice, state='readonly')
        self.profile_box.grid(row=1, column=1, sticky='ew', padx=(0, 8), pady=(0, 7))
        self.profile_box.bind('<<ComboboxSelected>>', self.select_profile)
        self.profile_button = ttk.Button(settings, text='현재 주소 저장', command=self.save_profile)
        self.profile_button.grid(row=1, column=2, sticky='ew', pady=(0, 7))
        self._input_widgets.append(self.profile_button)
        ttk.Label(settings, text='블로그 / 채널 주소', style='Card.TLabel').grid(row=2, column=0, sticky='w', padx=(0, 16))
        self.blog_entry = ttk.Entry(settings, textvariable=self.blog)
        self.blog_entry.grid(row=2, column=1, columnspan=2, sticky='ew', pady=4)
        self._input_widgets.append(self.blog_entry)
        ttk.Label(settings, text='블로그 ID 또는 네이버 프리미엄 채널 주소', style='CardMuted.TLabel').grid(
            row=3, column=1, columnspan=2, sticky='w', pady=(0, 7))
        ttk.Label(settings, text='저장 폴더', style='Card.TLabel').grid(row=4, column=0, sticky='w')
        folder_entry = ttk.Entry(settings, textvariable=self.folder)
        folder_entry.grid(row=4, column=1, sticky='ew', padx=(0, 8))
        self._input_widgets.append(folder_entry)
        choose = ttk.Button(settings, text='폴더 선택', command=self.choose_folder)
        choose.grid(row=4, column=2, sticky='ew')
        self._input_widgets.append(choose)
        options = ttk.Frame(settings, style='Card.TFrame')
        options.grid(row=5, column=0, columnspan=3, sticky='ew', pady=(7, 0))
        for index, (label, variable) in enumerate([
                ('이미지 저장', self.images), ('첨부파일 저장', self.files),
                ('인용 원본 저장', self.sources), ('기존 글도 최신 내용으로 갱신', self.refresh),
                ('영상 텍스트화', self.videos)]):
            widget = ttk.Checkbutton(options, text=label, variable=variable, style=self.control_styles.checkbutton)
            widget.grid(row=index // 2, column=index % 2, sticky='w', padx=(0, 14))
            self._input_widgets.append(widget)
        self.prepare_button = ttk.Button(options, text='음성 인식 준비', command=lambda: self.start('prepare-transcription'))
        self.prepare_button.grid(row=2, column=1, sticky='w', padx=(0, 14), pady=(3, 0))
        self._input_widgets.append(self.prepare_button)
        setting_actions = ttk.Frame(settings, style='Card.TFrame')
        setting_actions.grid(row=6, column=0, columnspan=3, sticky='ew', pady=(7, 0))
        for label, command in [('세부 설정', self.advanced_settings), ('설정 불러오기', self.load_settings), ('설정 저장', self.save_settings)]:
            button = ttk.Button(setting_actions, text=label, command=command)
            button.pack(side='left', padx=(0, 6))
            self._input_widgets.append(button)
        self.login_button = ttk.Button(setting_actions, text='네이버 로그인', command=lambda: self.start('login'), state='disabled')
        self.login_button.pack(side='left')
        ttk.Label(settings, textvariable=self.login_note, style='CardMuted.TLabel', wraplength=430).grid(
            row=7, column=0, columnspan=3, sticky='w', pady=(9, 0))

        actions = ttk.Frame(shell)
        actions.grid(row=2, column=0, columnspan=2, sticky='ew', pady=14)
        self.start_button = ttk.Button(actions, text='백업 시작 / 이어받기', style='Primary.TButton', command=lambda: self.start('backup'))
        self.start_button.pack(side='left', padx=(0, 8))
        self.stop_button = ttk.Button(actions, text='중단', command=self.stop, state='disabled')
        self.stop_button.pack(side='left', padx=(0, 14))
        self._operation_buttons = [self.start_button]
        for label, action in [('저장 결과 정리', 'reformat'), ('저장 파일 검사', 'verify'), ('이전 결과 확인', 'status'), ('환경 진단', 'doctor')]:
            button = ttk.Button(actions, text=label, command=lambda value=action: self.start(value))
            button.pack(side='left', padx=(0, 6))
            self._operation_buttons.append(button)
        ttk.Button(actions, text='결과 폴더 열기', command=self.show_folder).pack(side='right')

        progress = ttk.Frame(shell, style='Card.TFrame', padding=16)
        progress.grid(row=1, column=1, sticky='nsew')
        progress.columnconfigure(0, weight=1)
        ttk.Label(progress, text='02  진행 상태', style='Card.TLabel', font=('맑은 고딕', 12, 'bold')).grid(row=0, column=0, sticky='w')
        ttk.Label(progress, textvariable=self.phase, style='Card.TLabel', font=('맑은 고딕', 12, 'bold'), wraplength=340).grid(row=1, column=0, sticky='w', pady=(12, 0))
        self.progress = ttk.Progressbar(progress, mode='determinate', maximum=100)
        self.progress.grid(row=2, column=0, sticky='ew', pady=(10, 8))
        ttk.Label(progress, textvariable=self.detail, style='CardMuted.TLabel', wraplength=340).grid(row=3, column=0, sticky='w')
        counters = ttk.Frame(progress, style='Card.TFrame')
        counters.grid(row=4, column=0, sticky='ew', pady=(12, 0))
        for column, (key, label) in enumerate([('success', '완료'), ('partial', '부분 완료'), ('failed', '실패'), ('pending', '대기 / 진행')]):
            counters.columnconfigure(column % 2, weight=1)
            cell = ttk.Frame(counters, style='Card.TFrame')
            cell.grid(row=column // 2, column=column % 2, sticky='ew', pady=4)
            ttk.Label(cell, text=label, style='CardMuted.TLabel').pack(side='left')
            ttk.Label(cell, textvariable=self.counts[key], style='Card.TLabel', font=('맑은 고딕', 17, 'bold')).pack(side='left', padx=12)
        ttk.Label(progress, textvariable=self.run_summary, style='CardMuted.TLabel', wraplength=340).grid(
            row=5, column=0, sticky='w', pady=(8, 0))

        self.tabs = ttk.Notebook(shell, style=self.control_styles.notebook)
        self.tabs.grid(row=3, column=0, columnspan=2, sticky='nsew')
        logs = ttk.Frame(self.tabs, style='Card.TFrame')
        logs.rowconfigure(0, weight=1)
        logs.columnconfigure(0, weight=1)
        self.log = tk.Text(logs, height=7, wrap='word', relief='flat', padx=12, pady=10,
                           bg='white', fg=INK, state='disabled', font=('맑은 고딕', 10))
        self.log.grid(row=0, column=0, sticky='nsew')
        log_scroll = ttk.Scrollbar(logs, command=self.log.yview)
        log_scroll.grid(row=0, column=1, sticky='ns')
        self.log.configure(yscrollcommand=log_scroll.set)
        self.tabs.add(logs, text='실행 기록')
        issues = ttk.Frame(self.tabs, style='Card.TFrame')
        issues.rowconfigure(0, weight=1)
        issues.columnconfigure(0, weight=1)
        self.issues = ttk.Treeview(issues, columns=('item', 'status', 'error'), show='headings', height=6)
        for key, label, width in [('item', '글 / 이미지 / 첨부파일', 240), ('status', '상태', 90), ('error', '내용 · 더블클릭하면 전체 보기', 570)]:
            self.issues.heading(key, text=label)
            self.issues.column(key, width=width, minwidth=70, stretch=key == 'error')
        self.issues.grid(row=0, column=0, sticky='nsew')
        issues_scroll = ttk.Scrollbar(issues, command=self.issues.yview)
        issues_scroll.grid(row=0, column=1, sticky='ns')
        self.issues.configure(yscrollcommand=issues_scroll.set)
        self.issues.bind('<Double-1>', self.show_issue)
        self.tabs.add(issues, text='확인할 항목 (0)')
        self.library = LibraryPane(self.tabs, self.read_config)
        self.tabs.add(self.library, text='저장 글 검색')
        self.tabs.bind('<<NotebookTabChanged>>', self._tab_changed)
        ttk.Label(shell, textvariable=self.settings_note, style='Muted.TLabel', wraplength=1040).grid(row=4, column=0, columnspan=2, sticky='w', pady=(10, 0))

    def _update_settings_note(self):
        self.settings_note.set(f'설정: {self.config_path}  ·  블로그·채널마다 별도 저장 폴더를 사용해 주세요.')

    def _update_login_button(self, *_args):
        try:
            premium = is_premium(normalize_blog_id(self.blog.get()))
        except ValueError:
            premium = False
        self.login_button.configure(state='normal' if premium and not self.running else 'disabled')
        self.login_note.set('전용 브라우저에서 구독 계정으로 로그인한 뒤 백업하세요.' if premium
                            else '블로그·채널별 폴더·옵션은 백업 시작 시 저장합니다.')

    def _apply_config(self, config):
        self._advanced = asdict(config)
        self.blog.set(_source_label(config.blog_id))
        self.folder.set(str(config.out_dir))
        self.images.set(config.download_images)
        self.files.set(config.download_files)
        self.videos.set(config.transcribe_videos)
        self.sources.set(config.follow_sources)

    def _reload_profiles(self):
        try:
            self.saved_profiles = {_source_label(config.blog_id): config for config in self.profile_store.list()}
            self.profile_box.configure(values=list(self.saved_profiles))
            try:
                current_blog = _source_label(normalize_blog_id(self.blog.get()))
            except ValueError:
                current_blog = ''
            self.profile_choice.set(current_blog if current_blog in self.saved_profiles else '')
        except (OSError, ValueError, sqlite3.Error) as exc:
            self._log(f'저장한 블로그 목록을 불러올 수 없습니다: {exc}')

    def save_profile(self):
        try:
            config = self.read_config()
            self.profile_store.save(config)
            self._reload_profiles()
            self.profile_choice.set(_source_label(config.blog_id))
            self._log(f'주소를 저장했습니다: {_source_label(config.blog_id)} · {config.out_dir}')
        except (OSError, ValueError, sqlite3.Error) as exc:
            messagebox.showerror('블로그 저장 실패', str(exc), parent=self.root)

    def select_profile(self, _event=None):
        if self.running:
            return
        config = self.saved_profiles.get(self.profile_choice.get())
        if config is None:
            return
        self._apply_config(config)
        self.refresh.set(False)
        self.last_report = None
        self._show_counts({})
        self.issues.delete(*self.issues.get_children())
        self.tabs.tab(1, text='확인할 항목 (0)')
        self.phase.set(f'{_source_label(config.blog_id)} · 백업 준비')
        self.detail.set('저장한 폴더와 옵션을 불러왔습니다. 백업을 시작하면 기존 파일을 확인하고 이어 처리합니다.')
        self.run_summary.set('기본 백업: 기존 파일 확인 후 건너뛰기 · 새 글과 미완료 항목 저장')
        self._log(f'주소 선택: {_source_label(config.blog_id)} · {config.out_dir}')
        if self.tabs.index(self.tabs.select()) == 2:
            self.library.refresh()

    def _tab_changed(self, _event=None):
        if self.tabs.select() and self.tabs.index(self.tabs.select()) == 2:
            self.library.refresh()

    def read_config(self):
        values = dict(self._advanced)
        values.update(blog_id=normalize_blog_id(self.blog.get()), out_dir=self.folder.get().strip(),
                      download_images=self.images.get(), follow_sources=self.sources.get(),
                      download_files=self.files.get(), transcribe_videos=self.videos.get())
        return config_from_mapping(values, base_dir=self.config_path.parent)

    def choose_folder(self):
        path = filedialog.askdirectory(parent=self.root, title='백업 저장 폴더 선택', initialdir=self.folder.get(), mustexist=False)
        if path:
            self.folder.set(path)

    def load_settings(self):
        path = filedialog.askopenfilename(parent=self.root, title='설정 불러오기', filetypes=[('TOML 설정', '*.toml'), ('모든 파일', '*.*')])
        if not path:
            return
        try:
            config = load_config(path)
            self._apply_config(config)
            self.config_path = Path(path).resolve()
            label = _source_label(config.blog_id)
            self.profile_choice.set(label if label in self.saved_profiles else '')
            self._update_settings_note()
            self._log(f'설정을 불러왔습니다: {path}')
        except (OSError, ValueError) as exc:
            messagebox.showerror('설정 오류', str(exc), parent=self.root)

    def save_settings(self):
        try:
            config = self.read_config()
            save_config(config, self.config_path)
            self.profile_store.save(config)
            self._reload_profiles()
            self._log(f'설정을 저장했습니다: {self.config_path}')
        except (OSError, ValueError, sqlite3.Error) as exc:
            messagebox.showerror('설정 저장 실패', str(exc), parent=self.root)

    def advanced_settings(self):
        dialog = tk.Toplevel(self.root)
        dialog.title('세부 설정')
        dialog.transient(self.root)
        dialog.resizable(False, False)
        form = ttk.Frame(dialog, padding=24)
        form.pack(fill='both', expand=True)
        ttk.Label(form, text='기본값으로도 백업할 수 있습니다.', style='Muted.TLabel').grid(row=0, column=0, columnspan=2, sticky='w', pady=(0, 16))
        fields = {}
        specs = [('delay', '요청 간격 (초, 0 이상)', float), ('timeout', '요청 제한 시간 (초, 0 초과)', float),
                 ('retries', '최대 시도 횟수 (1~10)', int), ('source_depth', '인용 원본 추적 깊이 (0~3)', int),
                 ('max_image_mb', '이미지 최대 크기 (MiB, 1~500)', int),
                 ('max_file_mb', '첨부파일 최대 크기 (MiB, 1~2048)', int),
                 ('max_pages', '목록 페이지 상한 (1~100000)', int)]
        for row, (key, label, converter) in enumerate(specs, 1):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky='w', padx=(0, 24), pady=6)
            fields[key] = tk.StringVar(value=str(self._advanced[key]))
            ttk.Entry(form, textvariable=fields[key], width=13).grid(row=row, column=1, pady=6)

        def apply():
            values = dict(self._advanced)
            try:
                for key, label, converter in specs:
                    try:
                        values[key] = converter(fields[key].get())
                    except ValueError:
                        raise ValueError(f'{label}에 올바른 숫자를 입력해 주세요.') from None
                # Validate advanced fields even before a blog is entered.
                config_from_mapping({**values, 'blog_id': 'settings_validation', 'out_dir': str(values['out_dir'])})
                self._advanced = values
                dialog.destroy()
            except ValueError as exc:
                messagebox.showerror('세부 설정 오류', str(exc), parent=dialog)

        ttk.Button(form, text='적용', style='Primary.TButton', command=apply).grid(
            row=len(specs) + 1, column=1, sticky='e', pady=(16, 0))
        dialog.grab_set()

    def _set_busy(self, busy):
        self.running = busy
        self.stopping = False
        for widget in self._input_widgets + self._operation_buttons:
            widget.configure(state='disabled' if busy else 'normal')
        self.profile_box.configure(state='disabled' if busy else 'readonly')
        self.stop_button.configure(state='normal' if busy and self.current_action != 'doctor' else 'disabled')
        self._update_login_button()

    def start(self, action):
        if self.running:
            return
        try:
            config = None if action in ('doctor', 'prepare-transcription') else self.read_config()
            if action == 'login' and not is_premium(config.blog_id):
                raise ValueError('네이버 프리미엄 채널 주소를 먼저 입력하세요.')
            if action == 'backup':
                save_config(config, self.config_path)
                self.profile_store.save(config)
                self._reload_profiles()
            self.runner.start(action, config, refresh=self.refresh.get() if action == 'backup' else False)
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            messagebox.showerror('실행할 수 없습니다', str(exc), parent=self.root)
            return
        self.current_action = action
        self._set_busy(True)
        self.phase.set(f'{ACTION_NAMES[action]} 준비 중')
        self.detail.set('중단을 요청하면 현재 요청이 끝나는 대로 진행 내용을 보존하고 멈춥니다.' if action != 'doctor' else 'Python 패키지와 백업용 브라우저 실행을 확인하고 있습니다.')
        if action == 'login':
            self.detail.set('전용 브라우저에서 네이버에 직접 로그인하세요. 비밀번호는 프로그램에 입력하지 않습니다.')
        elif action == 'prepare-transcription':
            self.detail.set('처음에는 음성 인식 패키지와 모델(약 500 MB)을 내려받습니다. 영상·음성은 외부로 전송하지 않고 이 PC에서 처리합니다.')
        self._progress_phase = None
        self.progress.configure(mode='indeterminate', value=0)
        self.progress.start(12)
        if action not in ('doctor', 'login', 'prepare-transcription'):
            self.last_report = None
            self._show_counts({})
            self.issues.delete(*self.issues.get_children())
            self.tabs.tab(1, text='확인할 항목 (0)')
            self.run_summary.set('이번 실행 · 저장/복구 0개 · 기존 파일 건너뜀 0개 · 확인 필요 0개')
        self._log(f'{ACTION_NAMES[action]} 시작' + (f' · {config.blog_id} · {config.out_dir}' if config else ''))

    def stop(self):
        if not self.running:
            return
        self.runner.cancel()
        self.stopping = True
        self.stop_button.configure(state='disabled')
        self.phase.set('중단 요청을 처리하고 있습니다')
        self.detail.set('진행 중인 네트워크 요청은 설정한 제한 시간까지 기다릴 수 있습니다. 창을 닫지 말고 잠시 기다려 주세요.')
        if self.current_action == 'prepare-transcription':
            self.detail.set('음성 인식 준비 작업을 종료하고 있습니다. 완료 안내가 나올 때까지 잠시 기다려 주세요.')
            self._log('음성 인식 준비 중단을 요청했습니다.')
        else:
            self._log('중단을 요청했습니다. 저장된 글과 완료된 영상 텍스트는 다음 백업에서 재사용합니다.')

    def _show_counts(self, counts):
        for key in ('success', 'partial', 'failed'):
            self.counts[key].set(str(counts.get(key, 0)))
        self.counts['pending'].set(str(counts.get('pending', 0) + counts.get('running', 0)))

    def _log(self, text):
        if not text:
            return
        self.log.configure(state='normal')
        self.log.insert('end', f'{datetime.now():%H:%M:%S}  {text}\n')
        # Keep long archives responsive while retaining the full machine-readable report on disk.
        lines = int(self.log.index('end-1c').split('.')[0])
        if lines > 2000:
            self.log.delete('1.0', f'{lines - 2000}.0')
        self.log.see('end')
        self.log.configure(state='disabled')

    def _show_report(self, report):
        self.last_report = report
        self._show_counts(report.get('posts', {}))
        if 'run_counts' in report:
            self._show_run_counts(report['run_counts'])
        elif 'output_counts' in report:
            output = report['output_counts']
            self.run_summary.set(f'저장 결과 · 제목 파일명 {output.get("renamed", 0):,}개 변경 · Markdown {output.get("updated", 0):,}회 갱신')
            if output.get('cards'):
                self.run_summary.set(self.run_summary.get() + f' · 링크 카드 {output["cards"]:,}개 정리')
            if output.get('files'):
                self.run_summary.set(self.run_summary.get() + f' · 첨부 링크 {output["files"]:,}개 정리')
        self.issues.delete(*self.issues.get_children())
        for item in report.get('failures', []):
            self.issues.insert('', 'end', values=(f'{item["blog_id"]}/{item["post_id"]}', STATUS_NAMES.get(item['status'], item['status']), item.get('error') or '다음 백업에서 다시 처리합니다.'))
        for item in report.get('assets', []):
            kind = '첨부파일' if item.get('kind') == 'file' else '이미지'
            self.issues.insert('', 'end', values=(item['url'], kind, item.get('error') or f'{kind} 저장 미완료'))
        for problem in report.get('verification_problems', []):
            self.issues.insert('', 'end', values=('저장 파일 검사', '확인 필요', problem))
        for problem in report.get('output_problems', []):
            self.issues.insert('', 'end', values=('저장 결과 정리', '확인 필요', problem))
        last = report.get('last_run') or {}
        if last.get('error'):
            self.issues.insert('', 'end', values=('마지막 실행', STATUS_NAMES.get(last.get('status'), ''), last['error']))
        if last and not last.get('listing_complete'):
            self.issues.insert('', 'end', values=('글 목록', '미완료', '글 목록을 끝까지 확인하지 못했습니다. 백업을 다시 실행해 주세요.'))
        size = len(self.issues.get_children())
        self.tabs.tab(1, text=f'확인할 항목 ({size})')
        if last:
            self._log(f'마지막 백업: {STATUS_NAMES.get(last.get("status"), last.get("status"))} · 목록 {last.get("listed", 0)}개 · 시작 {last.get("started", "")}')

    def _show_run_counts(self, counts):
        self.run_summary.set(f'이번 실행 · 저장/복구 {counts.get("saved", 0):,}개 · 기존 파일 건너뜀 {counts.get("reused", 0):,}개 · 확인 필요 {counts.get("failed", 0):,}개')

    def show_issue(self, _event=None):
        selection = self.issues.selection()
        if selection:
            item, status, error = self.issues.item(selection[0], 'values')
            messagebox.showinfo(f'{status} · 상세 내용', f'{item}\n\n{error}', parent=self.root)

    def show_folder(self):
        try:
            if not self.folder.get().strip():
                raise ValueError('저장 폴더를 먼저 선택해 주세요.')
            path = Path(self.folder.get()).expanduser()
            open_folder((self.config_path.parent / path).resolve())
        except (OSError, ValueError) as exc:
            messagebox.showerror('폴더 열기 실패', str(exc), parent=self.root)

    def _handle_event(self, event):
        if event.get('kind') == 'done':
            self.progress.stop()
            self.progress.configure(mode='determinate', value=100 if event['status'] == 'success' else 0)
            self._set_busy(False)
            self.phase.set(f'{ACTION_NAMES[event["action"]]} · {STATUS_NAMES.get(event["status"], event["status"])}')
            self.detail.set(event['message'])
            self._log(event['message'])
            if event.get('report'):
                self._show_report(event['report'])
            if event['action'] not in ('login', 'prepare-transcription') and self.tabs.index(self.tabs.select()) == 2:
                self.library.refresh()
            if self.close_when_done:
                self.root.destroy()
            return
        message = event.get('message', '')
        if message:
            if not self.stopping:
                self.phase.set(message[:95])
            self._log(message)
        if 'counts' in event:
            self._show_counts(event['counts'])
        if 'run_counts' in event:
            self._show_run_counts(event['run_counts'])
        phase = event.get('phase')
        if phase != self._progress_phase and phase in ('starting', 'listing', 'linking', 'doctor', 'transcription-setup', 'video-download', 'transcribing'):
            self.progress.stop()
            self.progress.configure(mode='indeterminate', value=0)
            self.progress.start(12)
        self._progress_phase = phase
        if phase in ('transcribing', 'video-download'):
            current_key, total_key = ('seconds', 'total_seconds') if phase == 'transcribing' else ('bytes', 'total_bytes')
            current, total = event.get(current_key), event.get(total_key)
            if isinstance(current, (int, float)) and isinstance(total, (int, float)) and total > 0:
                self.progress.stop()
                self.progress.configure(mode='determinate', maximum=100, value=max(0, min(100, current / total * 100)))
                if not self.stopping:
                    if phase == 'transcribing':
                        self.detail.set(f'로컬 음성 인식 · {current:,.0f} / {total:,.0f}초 처리 · 영상·음성 외부 전송 없음')
                    else:
                        self.detail.set(f'텍스트 변환용 임시 영상 다운로드 · {current / 1048576:,.1f} / {total / 1048576:,.1f} MiB')
            return
        total = event.get('total')
        if isinstance(total, (int, float)) and total > 0 and 'completed' in event:
            self.progress.stop()
            self.progress.configure(mode='determinate', maximum=100, value=min(100, event['completed'] / total * 100))
            if not self.stopping:
                self.detail.set(f'처리 {event["completed"]} / 발견 {total}개 · 인용 원본을 발견하면 전체 건수가 늘어날 수 있습니다.')

    def _poll(self):
        for _ in range(100):
            try:
                self._handle_event(self.runner.events.get_nowait())
            except Empty:
                break
            if self.close_when_done and not self.running:
                return
        self.root.after(120, self._poll)

    def on_close(self):
        if not self.running:
            self.root.destroy()
        elif self.current_action == 'doctor':
            self.close_when_done = True
            self.phase.set('환경 진단이 끝나면 창을 닫습니다')
        elif messagebox.askyesno('중단 후 닫기', '진행 내용을 보존하고 작업을 중단한 뒤 창을 닫을까요?', parent=self.root):
            self.close_when_done = True
            self.stop()


def launch(config_path: str | Path | None = None) -> int:
    root = None
    try:
        root = tk.Tk()
        ArchiveApp(root, config_path)
        root.mainloop()
        return 0
    except Exception as exc:
        if root is not None:
            root.destroy()
        _startup_error(exc)
        return 2


def _startup_error(exc):
    if sys.platform == 'win32':
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, str(exc), '네이버 블로그 보관함 · 실행 오류', 0x10)
    elif sys.stderr:
        print(str(exc), file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description='네이버 블로그 보관함')
    parser.add_argument('--config', type=Path, default=Path('config.toml'))
    args = parser.parse_args()
    try:
        return launch(args.config)
    except Exception as exc:
        _startup_error(exc)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
