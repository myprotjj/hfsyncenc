"""
Backward-compatibility runner and shim for web.server.
Redirects all imports and CLI executions to the modular web package.
"""
import sys
from web import server as _mod

sys.modules[__name__] = _mod

if __name__ == "__main__":
    _mod.main()
