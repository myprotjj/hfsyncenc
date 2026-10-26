"""
Backward-compatibility shim for core.staging_manager.
Redirects all imports to the modular core package.
"""
import sys
from core import staging_manager as _mod

sys.modules[__name__] = _mod
