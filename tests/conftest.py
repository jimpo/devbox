import sys
from pathlib import Path

# Import the devbox package from the repo root, whether or not it's installed
# editable in the current environment.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
