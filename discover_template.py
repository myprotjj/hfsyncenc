"""
Backward-compatibility shim for web.templates.discover_template.
Redirects all imports to the modular web.templates package.
"""
import sys
from web.templates import discover_template as _mod

sys.modules[__name__] = _mod
