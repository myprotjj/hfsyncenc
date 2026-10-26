"""
Backward-compatibility shim for core.metadata_helper.
Redirects all imports to the modular core package.
"""
import sys
from core import metadata_helper as _mod

sys.modules[__name__] = _mod
