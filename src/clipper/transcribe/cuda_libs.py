"""Make the pip-installed NVIDIA CUDA 12 libraries visible to CTranslate2 on Windows.

faster-whisper's GPU path loads cuBLAS 12 and cuDNN 9 at runtime (`cublas64_12.dll`,
`cudnn64_9.dll`). A display driver alone does not ship them, so they come from the
`nvidia-cublas-cu12` / `nvidia-cudnn-cu12` wheels, which put the DLLs under
`site-packages/nvidia/<lib>/bin`. Windows does not search there by itself.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

REQUIRED_DLLS = ("cublas64_12.dll", "cublasLt64_12.dll", "cudnn64_9.dll")
_SUBDIRS = ("cublas", "cudnn", "cuda_nvrtc")
_done = False


def nvidia_bin_dirs() -> list[Path]:
    """`bin` folders of the installed nvidia-* wheels (empty when none are installed)."""
    spec = importlib.util.find_spec("nvidia")
    if spec is None or not spec.submodule_search_locations:
        return []
    out: list[Path] = []
    for root in spec.submodule_search_locations:
        for sub in _SUBDIRS:
            d = Path(root) / sub / "bin"
            if d.is_dir():
                out.append(d)
    return out


def ensure_cuda_dlls() -> list[Path]:
    """Add the wheel DLL folders to the Windows DLL search path (once). No-op elsewhere."""
    global _done
    if sys.platform != "win32":
        return []
    dirs = nvidia_bin_dirs()
    if _done:
        return dirs
    for d in dirs:
        os.add_dll_directory(str(d))
    if dirs:  # CTranslate2 loads cuBLAS lazily with plain LoadLibrary, which reads PATH
        os.environ["PATH"] = os.pathsep.join([*map(str, dirs), os.environ.get("PATH", "")])
    _done = True
    return dirs


def missing_cuda_dlls() -> list[str]:
    """Required GPU DLLs that cannot be found in the wheels or on PATH (Windows only)."""
    if sys.platform != "win32":
        return []
    search = [
        *nvidia_bin_dirs(),
        *map(Path, filter(None, os.environ.get("PATH", "").split(os.pathsep))),
    ]
    return [name for name in REQUIRED_DLLS if not any((d / name).is_file() for d in search)]
