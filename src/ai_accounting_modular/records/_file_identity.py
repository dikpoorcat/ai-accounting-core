"""File metadata from one handle; Windows ctime is creation, not change time."""

import os


def identity(path):
    if os.name != "nt":
        info = path.stat()
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns
    return _windows(path)


def _windows(path):
    import ctypes
    import msvcrt
    from ctypes import wintypes

    # https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_basic_info
    class BasicInfo(ctypes.Structure):
        _fields_ = [
            ("creation", ctypes.c_longlong),
            ("access", ctypes.c_longlong),
            ("write", ctypes.c_longlong),
            ("change", ctypes.c_longlong),
            ("attributes", wintypes.DWORD),
        ]

    api = ctypes.WinDLL("kernel32", use_last_error=True).GetFileInformationByHandleEx
    api.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    api.restype = wintypes.BOOL
    descriptor = os.open(path, os.O_RDONLY | os.O_BINARY)
    try:
        info = os.fstat(descriptor)
        basic = BasicInfo()
        if not api(msvcrt.get_osfhandle(descriptor), 0, ctypes.byref(basic), ctypes.sizeof(basic)):
            raise ctypes.WinError(ctypes.get_last_error())
        return info.st_dev, info.st_ino, info.st_size, basic.write, basic.change
    finally:
        os.close(descriptor)
