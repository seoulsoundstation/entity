"""Paginated, local archive search that keeps database work off Tk's thread."""
from __future__ import annotations

from pathlib import Path
from queue import Empty, Queue
from threading import Thread
import os
import subprocess
import sys
import tkinter as tk
from tkinter import messagebox, ttk
import webbrowser

from .files import inside


LABELS = {'success': '완료', 'partial': '부분 완료', 'failed': '실패',
          'pending': '대기', 'running': '진행 중'}


class LibraryPane(ttk.Frame):
    def __init__(self, parent, config_provider):
        super().__init__(parent, style='Card.TFrame', padding=10)
        self.config_provider = config_provider
        self.query = tk.StringVar()
        self.summary = tk.StringVar(value='설정한 블로그·채널의 저장 글을 ID·제목·본문으로 검색합니다.')
        self.page_note = tk.StringVar(value='0개')
        self.offset = 0
        self.limit = 50
        self.total = 0
        self.records = {}
        self.result_config = None
        self._request = 0
        self._criteria = None
        self._busy = False
        self._events = Queue()
        self._preview_events = Queue()
        self._preview_busy = False
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)
        search = ttk.Frame(self, style='Card.TFrame')
        search.grid(row=0, column=0, sticky='ew')
        search.columnconfigure(1, weight=1)
        ttk.Label(search, text='ID / 제목 / 본문', style='Card.TLabel').grid(row=0, column=0, padx=(0, 8))
        self.entry = ttk.Entry(search, textvariable=self.query)
        self.entry.grid(row=0, column=1, sticky='ew')
        self.entry.bind('<Return>', lambda _event: self.refresh() if not self._busy else None)
        self.search_button = ttk.Button(search, text='검색 / 새로고침', command=self.refresh)
        self.search_button.grid(row=0, column=2, padx=(8, 0))
        ttk.Label(self, textvariable=self.summary, style='CardMuted.TLabel', wraplength=920).grid(
            row=1, column=0, sticky='w', pady=(6, 8))
        table = ttk.Frame(self, style='Card.TFrame')
        table.grid(row=2, column=0, sticky='nsew')
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(table, columns=('id', 'title', 'blog', 'status'),
                                displaycolumns=('title', 'status', 'blog', 'id'),
                                show='headings', height=6, selectmode='browse')
        for key, label, width in [('title', '제목', 460), ('status', '상태', 90),
                                  ('blog', '블로그 / 채널', 180), ('id', '보관 ID', 190)]:
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, minwidth=220 if key == 'title' else 70,
                             stretch=key == 'title')
        self.tree.grid(row=0, column=0, sticky='nsew')
        scroll = ttk.Scrollbar(table, command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky='ns')
        horizontal = ttk.Scrollbar(table, orient='horizontal', command=self.tree.xview)
        horizontal.grid(row=1, column=0, sticky='ew')
        self.tree.configure(yscrollcommand=scroll.set, xscrollcommand=horizontal.set)
        self.tree.bind('<Double-1>', lambda _event: self.show_details())
        footer = ttk.Frame(self, style='Card.TFrame')
        footer.grid(row=3, column=0, sticky='ew', pady=(8, 0))
        self.preview_button = ttk.Button(footer, text='사진과 함께 읽기', command=self.preview_selected)
        self.preview_button.pack(side='left', padx=(0, 6))
        ttk.Button(footer, text='저장 파일 열기', command=self.open_selected).pack(side='left', padx=(0, 6))
        self.more_button = ttk.Menubutton(footer, text='더보기 ▾')
        self.more_menu = tk.Menu(self.more_button, tearoff=False)
        self.more_menu.add_command(label='글 정보', command=self.show_details)
        self.more_menu.add_command(label='ID 복사', command=self.copy_id)
        self.more_button.configure(menu=self.more_menu)
        self.more_button.pack(side='left')
        self.next_button = ttk.Button(footer, text='다음 ›', command=lambda: self.change_page(1), state='disabled')
        self.next_button.pack(side='right')
        ttk.Label(footer, textvariable=self.page_note, style='CardMuted.TLabel').pack(side='right', padx=12)
        self.previous_button = ttk.Button(footer, text='‹ 이전', command=lambda: self.change_page(-1), state='disabled')
        self.previous_button.pack(side='right')
        self.after(120, self._poll)

    def refresh(self, *, reset=True):
        try:
            config = self.config_provider()
        except (OSError, ValueError) as exc:
            self._request += 1
            self._clear()
            self.summary.set(str(exc))
            self._set_busy(False)
            return
        query = self.query.get().strip()
        criteria = (config.blog_id, str(config.out_dir), query)
        if reset or criteria != self._criteria:
            self.offset = 0
        self._criteria = criteria
        self._request += 1
        request = self._request
        offset = self.offset
        self._clear()
        self._set_busy(True)
        self.summary.set(f'{config.blog_id} · 저장 글을 확인하고 있습니다.')
        Thread(target=self._search, args=(request, config, query, offset),
               name='archive-library', daemon=True).start()

    def _search(self, request, config, query, offset):
        try:
            from .catalog import search_archive
            result = search_archive(config, query=query, limit=self.limit, offset=offset)
            self._events.put((request, config, result, None))
        except Exception as exc:
            self._events.put((request, config, None, str(exc)))

    def _clear(self):
        self.records = {}
        self.result_config = None
        self.total = 0
        self.tree.delete(*self.tree.get_children())
        self.page_note.set('0개')

    def _set_busy(self, busy):
        self._busy = busy
        self.search_button.configure(state='disabled' if busy else 'normal')
        self.previous_button.configure(state='normal' if not busy and self.offset > 0 else 'disabled')
        self.next_button.configure(state='normal' if not busy and self.offset + self.limit < self.total else 'disabled')

    def _poll(self):
        while True:
            try:
                request, config, result, error = self._events.get_nowait()
            except Empty:
                break
            if request != self._request:
                continue
            if error:
                self.summary.set(error)
                self._set_busy(False)
                continue
            self.result_config = config
            self.total = result['total']
            self.offset = result['offset']
            for row in result['items']:
                key = self.tree.insert('', 'end', values=(row['archive_id'], row['title'] or row['post_id'],
                                                         row['blog_id'], LABELS.get(row['status'], row['status'])))
                self.records[key] = row
            self.summary.set(f'{config.blog_id} · {config.out_dir} · 저장된 파일 기록 {self.total:,}개')
            if self.total:
                end = self.offset + len(result['items'])
                self.page_note.set(f'{self.offset + 1:,}–{end:,} / {self.total:,}')
            else:
                self.page_note.set('검색 결과 없음')
            self._set_busy(False)
        while True:
            try:
                config, path, error = self._preview_events.get_nowait()
            except Empty:
                break
            self._preview_busy = False
            self.preview_button.configure(state='normal')
            try:
                if error:
                    raise ValueError(error)
                if not webbrowser.open(path.as_uri()):
                    raise OSError('기본 브라우저를 열 수 없습니다. 브라우저 설정을 확인해 주세요.')
                if self.result_config == config:
                    self.summary.set('브라우저에서 저장된 본문과 사진을 열었습니다.')
            except (OSError, ValueError) as exc:
                messagebox.showerror('읽기 화면 열기 실패', str(exc), parent=self.winfo_toplevel())
        self.after(120, self._poll)

    def change_page(self, direction):
        if self._busy:
            return
        self.offset = max(0, self.offset + direction * self.limit)
        self.refresh(reset=False)

    def selected(self):
        selection = self.tree.selection()
        if not selection:
            messagebox.showinfo('글 선택', '저장 글 목록에서 글을 선택해 주세요.', parent=self.winfo_toplevel())
            return None
        return self.records.get(selection[0])

    def copy_id(self):
        row = self.selected()
        if row:
            self.clipboard_clear()
            self.clipboard_append(row['archive_id'])
            self.summary.set(f'보관 ID를 복사했습니다: {row["archive_id"]}')

    def show_details(self):
        row = self.selected()
        if not row:
            return
        details = (f'{row["title"] or row["post_id"]}\n\n보관 ID: {row["archive_id"]}'
                   f'\n네이버 글 ID: {row["post_id"]}\n블로그 / 채널: {row["blog_id"]}'
                   f'\n작성일: {row.get("published_at") or "원문 정보 없음"}'
                   f'\n보관일: {row.get("archived_at") or "기록 없음"}'
                   f'\n상태: {LABELS.get(row["status"], row["status"])}'
                   f'\n파일: {row["path"]}')
        messagebox.showinfo('저장 글 정보', details, parent=self.winfo_toplevel())

    def open_selected(self):
        row = self.selected()
        if not row or self.result_config is None:
            return
        try:
            path = inside(self.result_config.out_dir, row['path'])
            if path.suffix.lower() != '.md':
                raise ValueError('저장 글은 Markdown(.md) 파일만 열 수 있습니다.')
            if not path.is_file():
                raise ValueError('저장 파일이 없습니다. 백업을 실행하면 복구를 시도합니다.')
            if sys.platform == 'win32':
                os.startfile(str(path))
            elif sys.platform == 'darwin':
                subprocess.Popen(['open', str(path)])
            else:
                subprocess.Popen(['xdg-open', str(path)])
        except (OSError, ValueError) as exc:
            messagebox.showerror('파일 열기 실패', str(exc), parent=self.winfo_toplevel())

    def preview_selected(self):
        if self._preview_busy:
            return
        row = self.selected()
        if not row or self.result_config is None:
            return
        self._preview_busy = True
        self.preview_button.configure(state='disabled')
        self.summary.set('저장된 Markdown으로 사진과 함께 읽을 화면을 만들고 있습니다.')
        Thread(target=self._preview, args=(self.result_config, row),
               name='archive-reader', daemon=True).start()

    def _preview(self, config, row):
        try:
            from .reader import create_preview
            path = create_preview(config, row)
            self._preview_events.put((config, path, None))
        except Exception as exc:
            self._preview_events.put((config, None, str(exc)))
