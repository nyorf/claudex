import ctypes
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from claudex.domain.accounts import AccountError

_NOT_FOUND = -25300
_DUPLICATE = -25299
_AUTHORIZATION_ERRORS = {-128, -25293, -25308}


def _check(status: int) -> None:
    if status in _AUTHORIZATION_ERRORS:
        raise AccountError(
            f"macOS Keychain did not authorize Claudex (error {status}). "
            "Run Claudex from your macOS desktop session and approve access "
            "if Keychain prompts you.",
            status_code=503,
        )
    if status:
        raise AccountError(
            f"macOS Keychain returned error {status}. Check Keychain Access and try again.",
            status_code=503,
        )


class MacOSCredentialBackend:
    def __init__(self, keychain_path: Path | None = None) -> None:
        self._keychain_path = keychain_path
        self._security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
        self._core = ctypes.CDLL(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        )
        pointer = ctypes.c_void_p
        uint = ctypes.c_uint32
        self._security.SecKeychainOpen.argtypes = [ctypes.c_char_p, ctypes.POINTER(pointer)]
        self._security.SecKeychainOpen.restype = ctypes.c_int32
        self._security.SecKeychainFindGenericPassword.argtypes = [
            pointer,
            uint,
            ctypes.c_char_p,
            uint,
            ctypes.c_char_p,
            ctypes.POINTER(uint),
            ctypes.POINTER(pointer),
            ctypes.POINTER(pointer),
        ]
        self._security.SecKeychainFindGenericPassword.restype = ctypes.c_int32
        self._security.SecKeychainAddGenericPassword.argtypes = [
            pointer,
            uint,
            ctypes.c_char_p,
            uint,
            ctypes.c_char_p,
            uint,
            pointer,
            ctypes.POINTER(pointer),
        ]
        self._security.SecKeychainAddGenericPassword.restype = ctypes.c_int32
        self._security.SecKeychainItemModifyContent.argtypes = [pointer, pointer, uint, pointer]
        self._security.SecKeychainItemModifyContent.restype = ctypes.c_int32
        self._security.SecKeychainItemDelete.argtypes = [pointer]
        self._security.SecKeychainItemDelete.restype = ctypes.c_int32
        self._security.SecKeychainItemFreeContent.argtypes = [pointer, pointer]
        self._security.SecKeychainItemFreeContent.restype = ctypes.c_int32
        self._core.CFRelease.argtypes = [pointer]
        self._core.CFRelease.restype = None

    @contextmanager
    def _keychain(self) -> Iterator[ctypes.c_void_p | None]:
        if self._keychain_path is None:
            yield None
            return
        keychain = ctypes.c_void_p()
        _check(
            self._security.SecKeychainOpen(os.fsencode(self._keychain_path), ctypes.byref(keychain))
        )
        try:
            yield keychain
        finally:
            self._core.CFRelease(keychain)

    @staticmethod
    def _names(service: str, username: str) -> tuple[bytes, bytes]:
        service_bytes = service.encode("utf-8")
        username_bytes = username.encode("utf-8")
        if not service_bytes or not username_bytes:
            raise AccountError("The Keychain item name is invalid.", status_code=503)
        return service_bytes, username_bytes

    def get_password(self, service: str, username: str) -> str | None:
        service_bytes, username_bytes = self._names(service, username)
        length = ctypes.c_uint32()
        data = ctypes.c_void_p()
        with self._keychain() as keychain:
            status = self._security.SecKeychainFindGenericPassword(
                keychain,
                len(service_bytes),
                service_bytes,
                len(username_bytes),
                username_bytes,
                ctypes.byref(length),
                ctypes.byref(data),
                None,
            )
        if status == _NOT_FOUND:
            return None
        _check(status)
        try:
            return ctypes.string_at(data, length.value).decode("utf-8")
        finally:
            self._security.SecKeychainItemFreeContent(None, data)

    def _find_item(
        self, keychain: ctypes.c_void_p | None, service: bytes, username: bytes
    ) -> ctypes.c_void_p | None:
        item = ctypes.c_void_p()
        status = self._security.SecKeychainFindGenericPassword(
            keychain, len(service), service, len(username), username, None, None, ctypes.byref(item)
        )
        if status == _NOT_FOUND:
            return None
        _check(status)
        return item

    def set_password(self, service: str, username: str, password: str) -> None:
        service_bytes, username_bytes = self._names(service, username)
        encoded = password.encode("utf-8")
        data = ctypes.create_string_buffer(encoded, len(encoded) + 1)
        with self._keychain() as keychain:
            item = self._find_item(keychain, service_bytes, username_bytes)
            if item is None:
                status = self._security.SecKeychainAddGenericPassword(
                    keychain,
                    len(service_bytes),
                    service_bytes,
                    len(username_bytes),
                    username_bytes,
                    len(encoded),
                    data,
                    None,
                )
                if status != _DUPLICATE:
                    _check(status)
                    return
                item = self._find_item(keychain, service_bytes, username_bytes)
                if item is None:
                    _check(status)
            try:
                _check(self._security.SecKeychainItemModifyContent(item, None, len(encoded), data))
            finally:
                self._core.CFRelease(item)

    def delete_password(self, service: str, username: str) -> None:
        service_bytes, username_bytes = self._names(service, username)
        with self._keychain() as keychain:
            item = self._find_item(keychain, service_bytes, username_bytes)
            if item is None:
                return
            try:
                _check(self._security.SecKeychainItemDelete(item))
            finally:
                self._core.CFRelease(item)
