"""
Backward-compatibility shim for web.templates.storyboard_test_template.
Redirects all imports to the modular web.templates package.
"""
import sys
from web.templates import storyboard_test_template as _mod

sys.modules[__name__] = _mod
