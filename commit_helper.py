"""
Backward-compatibility shim for core.commit_helper.
Redirects all imports to the modular core package.
"""
import sys
from core import commit_helper as _mod

sys.modules[__name__] = _mod
