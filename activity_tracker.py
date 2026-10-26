"""
Backward-compatibility shim for core.activity_tracker.
Redirects all imports to the modular core package.
"""
import sys
from core import activity_tracker as _mod

sys.modules[__name__] = _mod
