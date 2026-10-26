"""
Backward-compatibility runner and shim for services.watcher_service.
Redirects all imports and CLI executions to the modular services package.
"""
import sys
from services import watcher_service as _mod

sys.modules[__name__] = _mod

if __name__ == "__main__":
    _mod.main()
