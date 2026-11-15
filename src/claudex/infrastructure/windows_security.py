import ctypes
from pathlib import Path
from typing import Any, cast


def make_private(path: Path) -> None:
    from ctypes import wintypes

    windows_ctypes = cast(Any, ctypes)
    library = windows_ctypes.WinDLL
    win_error = windows_ctypes.WinError
    last_error = windows_ctypes.get_last_error
    advapi = library("advapi32", use_last_error=True)
    kernel = library("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]
    advapi.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise win_error(last_error())
    sid_string = wintypes.LPWSTR()
    descriptor = ctypes.c_void_p()
    try:
        size = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        if not size.value:
            raise win_error(last_error())
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
            raise win_error(last_error())
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p)).contents.value
        if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(sid_string)):
            raise win_error(last_error())
        sddl = f"D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;{sid_string.value})"
        if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, ctypes.byref(descriptor), None
        ):
            raise win_error(last_error())
        if not advapi.SetFileSecurityW(str(path), 0x80000004, descriptor):
            raise win_error(last_error())
    finally:
        if descriptor.value:
            kernel.LocalFree(descriptor)
        if sid_string.value:
            kernel.LocalFree(ctypes.cast(sid_string, wintypes.HLOCAL))
        kernel.CloseHandle(token)
