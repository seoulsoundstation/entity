"""Check stateful styles without operating a user's application window."""
import gc

import pytest

tk = pytest.importorskip('tkinter', reason='GUI tests require Python with tkinter')
from tkinter import ttk

from naver_blog_archive.ui_styles import install_control_styles


@pytest.fixture
def controls(desktop):
    root = tk.Toplevel(desktop)
    root.withdraw()
    style = ttk.Style(root)
    style.theme_use('clam')
    installed = install_control_styles(root, style)
    yield root, style, installed
    root.destroy()


def _numbers(root, value):
    return tuple(int(part) for part in root.tk.splitlist(value))


def test_tab_selection_hover_and_disabled_states_are_distinguishable(controls):
    _, style, installed = controls
    tab = installed.notebook + '.Tab'
    assert style.lookup(tab, 'background', ('selected',)) == '#087f62'
    assert style.lookup(tab, 'foreground', ('selected',)) == 'white'
    assert style.lookup(tab, 'background', ('active',)) == '#dcefe6'
    assert style.lookup(tab, 'background', ()) == '#e7edf3'
    assert style.lookup(tab, 'background', ('disabled', 'active')) == '#eef1f4'
    assert style.lookup(tab, 'foreground', ('disabled', 'selected')) == 'white'
    assert style.lookup(tab, 'focuscolor', ('selected', 'focus')) == 'white'


def test_tab_geometry_does_not_change_when_selected_or_pressed(controls):
    root, style, installed = controls
    tab = installed.notebook + '.Tab'
    notebook = ttk.Notebook(root, style=installed.notebook)
    for label in ('First tab', 'Longer second tab'):
        notebook.add(ttk.Frame(notebook, width=200, height=80), text=label)
    notebook.pack()
    root.update_idletasks()
    original_size = notebook.winfo_reqwidth(), notebook.winfo_reqheight()
    for index in (1, 0, 1):
        notebook.select(index)
        root.update_idletasks()
        assert (notebook.winfo_reqwidth(), notebook.winfo_reqheight()) == original_size
    for state in ((), ('selected',), ('active',), ('selected', 'active'), ('pressed',), ('disabled',)):
        assert _numbers(root, style.lookup(tab, 'padding', state)) == (18, 9, 18, 9)
        assert _numbers(root, style.lookup(tab, 'expand', state)) == (0, 0, 0, 0)


def test_checkbutton_retains_standard_toggle_and_disabled_semantics(controls):
    root, style, installed = controls
    selected = tk.BooleanVar(root, False)
    widget = ttk.Checkbutton(root, text='Save images', variable=selected, style=installed.checkbutton)
    widget.pack()
    widget.invoke()
    assert selected.get() and widget.instate(('selected',))
    widget.invoke()
    assert not selected.get() and not widget.instate(('selected',))
    widget.state(['disabled'])
    widget.invoke()
    assert not selected.get()
    assert style.lookup(installed.checkbutton, 'foreground', ('disabled',)) == '#82909e'
    # Keeping the style result retains all image objects across collection.
    gc.collect()
    names = root.tk.call('image', 'names')
    assert all(str(item) in names for item in installed.images)


def test_multiple_windows_can_install_controls_without_element_collisions(controls):
    root, style, first = controls
    second_window = tk.Toplevel(root)
    second_window.withdraw()
    second = install_control_styles(second_window, style)
    assert first.checkbutton != second.checkbutton
    assert first.notebook != second.notebook
    assert style.layout(first.checkbutton) != style.layout(second.checkbutton)
    original = ttk.Checkbutton(root, style=first.checkbutton)
    original.invoke()
    second_window.destroy()
    root.update_idletasks()
    assert style.lookup(first.notebook + '.Tab', 'background', ('selected',)) == '#087f62'
