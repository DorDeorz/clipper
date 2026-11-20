from __future__ import annotations

import sys
from pathlib import Path

import pytest

from clipper.transcribe import cuda_libs


def test_missing_dlls_reported_when_not_found(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cuda_libs.sys, "platform", "win32")
    monkeypatch.setattr(cuda_libs, "nvidia_bin_dirs", lambda: [tmp_path])
    monkeypatch.setenv("PATH", "")
    assert cuda_libs.missing_cuda_dlls() == list(cuda_libs.REQUIRED_DLLS)
    for name in cuda_libs.REQUIRED_DLLS:
        (tmp_path / name).write_bytes(b"")
    assert cuda_libs.missing_cuda_dlls() == []


def test_noop_off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cuda_libs.sys, "platform", "linux")
    assert cuda_libs.ensure_cuda_dlls() == []
    assert cuda_libs.missing_cuda_dlls() == []


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DLL yolu")
def test_installed_wheels_provide_required_dlls() -> None:
    assert cuda_libs.missing_cuda_dlls() == []
