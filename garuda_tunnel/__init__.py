"""Public package entry point. Only ``__version__`` is exposed."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("garuda-tunnel")
except PackageNotFoundError:  # source checkout without install
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
