import ctypes
import json
import os
import sys
from collections.abc import Iterator
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from claudex.domain.accounts import AccountError, Registration
from claudex.infrastructure.credentials import SERVICE, CredentialStore
from claudex.infrastructure.macos_credentials import MacOSCredentialBackend

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS Keychain")


@pytest.fixture
def private_keychain(tmp_path: Path) -> Iterator[tuple[MacOSCredentialBackend, ctypes.c_void_p]]:
    path = tmp_path / "claudex-test.keychain"
    backend = MacOSCredentialBackend(path)
    create = backend._security.SecKeychainCreate
    create.argtypes = [
        ctypes.c_char_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_ubyte,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    create.restype = ctypes.c_int32
    delete = backend._security.SecKeychainDelete
    delete.argtypes = [ctypes.c_void_p]
    delete.restype = ctypes.c_int32
    keychain = ctypes.c_void_p()
    fixture_password = b"synthetic keychain password"
    status = create(
        os.fsencode(path), len(fixture_password), fixture_password, 0, None, ctypes.byref(keychain)
    )
    assert status == 0
    try:
        yield backend, keychain
    finally:
        assert delete(keychain) == 0
        backend._core.CFRelease(keychain)


def test_native_macos_keeps_existing_registration_after_a_denied_update(
    private_keychain: tuple[MacOSCredentialBackend, ctypes.c_void_p],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, keychain = private_keychain
    registration_id = "fixture-registration"
    original = Registration(client_id="synthetic-client", subject="synthetic-subject")
    replacement = replace(original, needs_consent=True)
    service = SERVICE.encode()
    account = registration_id.encode()
    saved = json.dumps(asdict(original)).encode()
    assert (
        backend._security.SecKeychainAddGenericPassword(
            keychain, len(service), service, len(account), account, len(saved), saved, None
        )
        == 0
    )

    store = CredentialStore(backend)
    assert store.read(registration_id) == original
    with monkeypatch.context() as denied:
        denied.setattr(backend._security, "SecKeychainItemModifyContent", lambda *_: -25293)
        with pytest.raises(AccountError, match="macOS Keychain"):
            store.write(registration_id, replacement)

    reopened = CredentialStore(MacOSCredentialBackend(backend._keychain_path))
    assert reopened.read(registration_id) == original
    reopened.write(registration_id, replacement)
    assert reopened.read(registration_id) == replacement
    reopened.delete(registration_id)
    assert reopened.read(registration_id) is None
    reopened.write(registration_id, original)
    assert reopened.read(registration_id) == original
