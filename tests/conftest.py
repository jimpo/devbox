import importlib.util
import sys
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location("devbox", Path(__file__).parent.parent / "devbox.py")
devbox = importlib.util.module_from_spec(_SPEC)
sys.modules["devbox"] = devbox
_SPEC.loader.exec_module(devbox)
