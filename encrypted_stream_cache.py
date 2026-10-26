"""
Backward-compatibility shim for core.encrypted_stream_cache.
Redirects all imports to the modular core package.
"""
import sys
from core import encrypted_stream_cache as _mod

sys.modules[__name__] = _mod
