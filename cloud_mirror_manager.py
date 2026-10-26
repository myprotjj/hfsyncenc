"""
Backward-compatibility shim for core.cloud_mirror_manager.
Redirects all imports to the modular core package.
"""
import sys
from core import cloud_mirror_manager as _mod

sys.modules[__name__] = _mod
