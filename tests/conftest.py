"""Share one Tcl interpreter; each GUI test still owns an isolated window."""
import pytest


@pytest.fixture(scope='session')
def desktop():
    # Keep non-GUI tests usable on Python installations without tkinter.
    tk = pytest.importorskip('tkinter', reason='GUI tests require Python with tkinter')
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f'Tk desktop unavailable: {exc}')
    root.withdraw()
    yield root
    for callback in root.tk.call('after', 'info'):
        root.tk.call('after', 'cancel', callback)
    root.destroy()
