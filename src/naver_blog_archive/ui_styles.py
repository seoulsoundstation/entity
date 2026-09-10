"""Scoped ttk controls with a clear check mark and tabs that stay in place."""
from __future__ import annotations

from dataclasses import dataclass
from itertools import count
import math
import tkinter as tk
from tkinter import ttk


_INSTALLATIONS = count(1)
_GREEN = '#087f62'
_INK = '#172c43'
_MUTED = '#61748a'
_BG = '#f3f6fa'


@dataclass(frozen=True)
class ControlStyles:
    """Keep this object alive as long as its controls to retain Tk image objects."""

    checkbutton: str
    notebook: str
    images: tuple[tk.PhotoImage, ...]


def _rgb(color: str) -> tuple[int, int, int]:
    return tuple(int(color[index:index + 2], 16) for index in (1, 3, 5))


def _segment_distance(x, y, start, end):
    dx, dy = end[0] - start[0], end[1] - start[1]
    projection = max(0, min(1, ((x - start[0]) * dx + (y - start[1]) * dy) / (dx * dx + dy * dy)))
    return math.hypot(x - start[0] - projection * dx, y - start[1] - projection * dy)


def _indicator(root, size: int, *, fill: str, border: str, mark: str = '') -> tk.PhotoImage:
    """Rasterize a small vector check mark independently of the platform font."""
    # Coordinates use an 18px design, scaled to the current Tk display density.
    # The transparent tail separates the indicator from its text.
    image = tk.PhotoImage(master=root, width=size + round(size / 3), height=size)
    scale = size / 18
    fill_rgb, border_rgb = _rgb(fill), _rgb(border)
    background = (255, 255, 255)
    for row in range(size):
        pixels = []
        for column in range(size):
            totals = [0, 0, 0]
            for sy in (0.25, 0.75):
                for sx in (0.25, 0.75):
                    x, y = (column + sx) / scale, (row + sy) / scale
                    # Rounded 16px square; signed distance determines the border.
                    qx, qy = abs(x - 9) - 5.8, abs(y - 9) - 5.8
                    edge = math.hypot(max(qx, 0), max(qy, 0)) + min(max(qx, qy), 0) - 2.2
                    color = background if edge > 0 else border_rgb if edge > -1.4 else fill_rgb
                    if mark == 'check' and min(
                        _segment_distance(x, y, (4.8, 9.1), (7.6, 12.0)),
                        _segment_distance(x, y, (7.6, 12.0), (13.4, 5.9)),
                    ) <= 1.05:
                        color = background
                    elif mark == 'dash' and 4.7 <= x <= 13.3 and 8 <= y <= 10:
                        color = background
                    for channel, value in enumerate(color):
                        totals[channel] += value
            pixels.append('#' + ''.join(f'{round(value / 4):02x}' for value in totals))
        image.put('{' + ' '.join(pixels) + '}', to=(0, row))
    return image


def install_control_styles(root: tk.Misc, style: ttk.Style) -> ControlStyles:
    """Install styles in the active theme and return their names and image owners.

    Call after selecting the theme.  Each installation has independent names, so
    separate application windows can be constructed in the same Tcl interpreter.
    The caller applies ``result.checkbutton`` and ``result.notebook`` to widgets.
    """
    prefix = f'ArchiveControls{next(_INSTALLATIONS)}'
    checkbutton = f'{prefix}.TCheckbutton'
    notebook = f'{prefix}.TNotebook'
    indicator_element = f'{prefix}.Checkbutton.indicator'
    size = max(16, round(18 * float(root.tk.call('tk', 'scaling')) / (4 / 3)))
    specifications = (
        ('white', '#8192a4', ''),
        ('#edf8f3', _GREEN, ''),
        ('#deefe7', '#05624c', ''),
        ('#f3f5f7', '#b8c2cd', ''),
        (_GREEN, _GREEN, 'check'),
        ('#066e55', '#066e55', 'check'),
        ('#055640', '#055640', 'check'),
        ('#91b5a9', '#91b5a9', 'check'),
        (_GREEN, _GREEN, 'dash'),
        ('#91b5a9', '#91b5a9', 'dash'),
    )
    images = tuple(_indicator(root, size, fill='#ffffff' if fill == 'white' else fill,
                              border=border, mark=mark) for fill, border, mark in specifications)
    normal, hovered, pressed, disabled, checked, checked_hover, checked_press, checked_disabled, mixed, mixed_disabled = images
    style.element_create(
        indicator_element, 'image', normal,
        ('disabled', 'selected', checked_disabled),
        ('disabled', 'alternate', mixed_disabled),
        ('disabled', disabled),
        ('pressed', 'selected', checked_press),
        ('active', 'selected', checked_hover),
        ('selected', checked),
        ('alternate', mixed),
        ('pressed', pressed),
        ('active', hovered),
        sticky='w',
    )
    style.layout(checkbutton, [
        ('Checkbutton.padding', {'sticky': 'nswe', 'children': [
            (indicator_element, {'side': 'left', 'sticky': ''}),
            ('Checkbutton.focus', {'side': 'left', 'sticky': 'w', 'children': [
                ('Checkbutton.label', {'sticky': 'nswe'}),
            ]}),
        ]}),
    ])
    style.configure(checkbutton, background='white', foreground=_INK,
                    padding=(0, 5), focuscolor=_GREEN)
    style.map(checkbutton, background=[('', 'white')],
              foreground=[('disabled', '#82909e'), ('', _INK)])

    style.configure(notebook, background=_BG, borderwidth=0, tabmargins=(0, 0, 0, 0))
    tab_style = notebook + '.Tab'
    padding = (18, 9, 18, 9)
    style.configure(tab_style, padding=padding, borderwidth=0, relief='flat',
                    background='#e7edf3', foreground=_MUTED, focuscolor=_GREEN)
    backgrounds = [
        ('disabled', 'selected', '#aac7bd'),
        ('disabled', '#eef1f4'),
        ('selected', 'active', '#066e55'),
        ('selected', _GREEN),
        ('pressed', '#cce5da'),
        ('active', '#dcefe6'),
        ('', '#e7edf3'),
    ]
    # clam adds vertical selected padding by default. Override its *state map*,
    # not just configure(), so clicking or keyboard navigation cannot resize tabs.
    style.map(tab_style, background=backgrounds, lightcolor=backgrounds,
              darkcolor=backgrounds, bordercolor=backgrounds,
              foreground=[('disabled', 'selected', 'white'), ('disabled', '#8c98a6'),
                          ('selected', 'white'), ('active', _INK), ('', _MUTED)],
              padding=[('', padding)], expand=[('', (0, 0, 0, 0))],
              focuscolor=[('selected', 'white'), ('', _GREEN)])
    return ControlStyles(checkbutton, notebook, images)
