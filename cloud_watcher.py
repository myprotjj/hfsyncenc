"""
Backward-compatibility shim for services.cloud_watcher.
Redirects all imports to the modular services package.
"""
import sys
from services import cloud_watcher as _mod

sys.modules[__name__] = _mod
