"""Native Windows open/save dialogs through comdlg32 (any thread, no Tk needed)."""
import ctypes
import os
import sys
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


def _dialog(save, kind, title, initial):
    if sys.platform != "win32":
        return None
    buf = ctypes.create_unicode_buffer(initial or "", 1024)
    ofn = OPENFILENAMEW()
    ofn.lStructSize = ctypes.sizeof(OPENFILENAMEW)
    ofn.hwndOwner = ctypes.windll.user32.GetForegroundWindow()
    ofn.lpstrFilter = FILTERS.get(kind, FILTERS["any"])
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
    if not fn(ctypes.byref(ofn)):
        return None
    return buf.value


def open_file(kind="any", title="Open", initial=None):
    return _dialog(False, kind, title, initial)


def save_file(kind="any", title="Save as", initial=None):
    return _dialog(True, kind, title, initial)
