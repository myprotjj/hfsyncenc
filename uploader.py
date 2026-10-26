"""
Backward-compatibility runner and shim for services.uploader.
Redirects all imports and CLI executions to the modular services package.
"""
import sys
from services import uploader as _mod

sys.modules[__name__] = _mod

if __name__ == "__main__":
    if hasattr(_mod, "main"):
        _mod.main()
