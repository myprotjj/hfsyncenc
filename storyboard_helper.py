"""
Backward-compatibility shim for core.storyboard_helper.
Redirects all imports to the modular core package.
"""
import sys
from core import storyboard_helper as _mod

sys.modules[__name__] = _mod
