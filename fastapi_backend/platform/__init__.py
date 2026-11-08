"""Reusable platform boundary.

This package intentionally uses the architecture term ``platform``. Because
Python also has a standard-library module named ``platform``, expose stdlib
platform attributes here so third-party libraries importing ``platform`` keep
working when the backend root is first on ``sys.path``.
"""

import importlib.util
import sysconfig
from pathlib import Path


_stdlib_platform_path = Path(sysconfig.get_path("stdlib")) / "platform.py"
_spec = importlib.util.spec_from_file_location("_stdlib_platform", _stdlib_platform_path)
if _spec and _spec.loader:
    _stdlib_platform = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_stdlib_platform)
    for _name in dir(_stdlib_platform):
        if _name not in globals():
            globals()[_name] = getattr(_stdlib_platform, _name)
