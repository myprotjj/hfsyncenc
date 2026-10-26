"""
Backward-compatibility shim for core.account_manager.
Redirects all imports to the modular core package.
"""
import sys
from core import account_manager as _mod

sys.modules[__name__] = _mod
