"""
Backward-compatibility shim for core.companion_helper.
Redirects all imports to the modular core package.
"""
import sys
from core import companion_helper as _mod

sys.modules[__name__] = _mod
