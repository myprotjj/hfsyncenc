"""
Backward-compatibility shim for core.crypto_engine.
Redirects all imports to the modular core package.
"""
import sys
from core import crypto_engine as _mod

sys.modules[__name__] = _mod
