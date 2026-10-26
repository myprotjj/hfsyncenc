"""
Backward-compatibility shim for core.screens_helper.
Redirects all imports to the modular core package.
"""
import sys
from core import screens_helper as _mod

sys.modules[__name__] = _mod
