"""
Backward-compatibility shim for core.hf_rate_limiter.
Redirects all imports to the modular core package.
"""
import sys
from core import hf_rate_limiter as _mod

sys.modules[__name__] = _mod
