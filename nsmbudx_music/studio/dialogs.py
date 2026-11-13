"""Native Windows open/save dialogs through comdlg32 (any thread, no Tk needed)."""
import ctypes
import os
import sys
import threading
import time
from ctypes import wintypes

OFN_OVERWRITEPROMPT = 0x2
OFN_NOCHANGEDIR = 0x8
OFN_PATHMUSTEXIST = 0x800
OFN_FILEMUSTEXIST = 0x1000
OFN_EXPLORER = 0x80000


class OPENFILENAMEW(ctypes.Structure):
    _fields_ = [("lStructSize", wintypes.DWORD), ("hwndOwner", wintypes.HWND),
                ("hInstance", wintypes.HINSTANCE), ("lpstrFilter", wintypes.LPCWSTR),
                ("lpstrCustomFilter", wintypes.LPWSTR), ("nMaxCustFilter", wintypes.DWORD),
                ("nFilterIndex", wintypes.DWORD), ("lpstrFile", wintypes.LPWSTR),
                ("nMaxFile", wintypes.DWORD), ("lpstrFileTitle", wintypes.LPWSTR),
                ("nMaxFileTitle", wintypes.DWORD), ("lpstrInitialDir", wintypes.LPCWSTR),
                ("lpstrTitle", wintypes.LPCWSTR), ("Flags", wintypes.DWORD),
                ("nFileOffset", wintypes.WORD), ("nFileExtension", wintypes.WORD),
                ("lpstrDefExt", wintypes.LPCWSTR), ("lCustData", wintypes.LPARAM),
                ("lpfnHook", ctypes.c_void_p), ("lpTemplateName", wintypes.LPCWSTR),
                ("pvReserved", ctypes.c_void_p), ("dwReserved", wintypes.DWORD),
                ("FlagsEx", wintypes.DWORD)]


FILTERS = {
    "bfsar": "Sound archive (*.bfsar)\0*.bfsar\0All files\0*.*\0\0",
    "wav": "WAV audio (*.wav)\0*.wav\0All files\0*.*\0\0",
    "bfwav": "BFWAV (*.bfwav)\0*.bfwav\0WAV audio (*.wav)\0*.wav\0All files\0*.*\0\0",
    "txt": "Text (*.txt)\0*.txt\0All files\0*.*\0\0",
    "any": "All files\0*.*\0\0",
}


class DialogError(RuntimeError):
    pass


def _bring_to_front(hwnd):
    """Raise a window of this (background) process over the focused app.

    Windows only lets the process that has focus give focus away, and the Studio's
    window belongs to Edge, so a dialog opened by the server would sit behind it.
    Joining the focused thread's input state lifts that for the moment it takes."""
    u = ctypes.windll.user32
    u.GetForegroundWindow.restype = wintypes.HWND
    u.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.c_void_p)
    u.SetWindowPos.argtypes = (wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                               ctypes.c_int, ctypes.c_uint)
    fg = u.GetForegroundWindow()
    fg_thread = u.GetWindowThreadProcessId(fg, None)
    me = u.GetWindowThreadProcessId(hwnd, None)
    attached = fg_thread and fg_thread != me and u.AttachThreadInput(me, fg_thread, True)
    try:
        flags = 0x0001 | 0x0002 | 0x0040            # no size, no move, show
        u.SetWindowPos(hwnd, -1, 0, 0, 0, 0, flags)  # topmost, so it shows above in any case
        u.SetWindowPos(hwnd, -2, 0, 0, 0, 0, flags)  # then back to a normal window
        u.SetForegroundWindow(hwnd)
        u.BringWindowToTop(hwnd)
    finally:
        if attached:
            u.AttachThreadInput(me, fg_thread, False)


def _raise_when_shown(title):
    u = ctypes.windll.user32
    u.FindWindowW.restype = wintypes.HWND
    for _ in range(100):
        time.sleep(0.05)
        hwnd = u.FindWindowW("#32770", title)
        if hwnd:
            _bring_to_front(hwnd)
            return


def _dialog(save, kind, title, initial):
    if sys.platform != "win32":
        raise DialogError("file dialogs need Windows: type the path instead")
    # the dialog wants a single-threaded COM apartment on its thread
    ctypes.windll.ole32.CoInitializeEx(None, 0x2)
    buf = ctypes.create_unicode_buffer(initial or "", 1024)
    # the filter is NUL-separated pairs ending in two NULs; a plain str would be cut at
    # the first NUL, so copy it character by character into a buffer of its own
    text = FILTERS.get(kind, FILTERS["any"])
    filt = (ctypes.c_wchar * (len(text) + 1))(*text)
    ofn = OPENFILENAMEW()
    ofn.lStructSize = ctypes.sizeof(OPENFILENAMEW)
    ofn.hwndOwner = ctypes.windll.user32.GetForegroundWindow()
    ofn.lpstrFilter = ctypes.cast(filt, wintypes.LPCWSTR)
    ofn.lpstrFile = ctypes.cast(buf, wintypes.LPWSTR)
    ofn.nMaxFile = 1024
    ofn.lpstrTitle = title
    if initial and os.path.isdir(os.path.dirname(initial)):
        ofn.lpstrInitialDir = os.path.dirname(initial)
    ext = {"bfsar": "bfsar", "wav": "wav", "txt": "txt", "bfwav": "bfwav"}.get(kind)
    ofn.lpstrDefExt = ext
    ofn.Flags = OFN_EXPLORER | OFN_NOCHANGEDIR | OFN_PATHMUSTEXIST | \
        (OFN_OVERWRITEPROMPT if save else OFN_FILEMUSTEXIST)
    fn = ctypes.windll.comdlg32.GetSaveFileNameW if save else ctypes.windll.comdlg32.GetOpenFileNameW
    threading.Thread(target=_raise_when_shown, args=(title,), daemon=True).start()
    if not fn(ctypes.byref(ofn)):
        err = ctypes.windll.comdlg32.CommDlgExtendedError()
        if err:                          # 0 means the user cancelled
            raise DialogError(f"the file dialog failed (error 0x{err:04X})")
        return None
    return buf.value


def open_file(kind="any", title="Open", initial=None):
    return _dialog(False, kind, title, initial)


def save_file(kind="any", title="Save as", initial=None):
    return _dialog(True, kind, title, initial)
