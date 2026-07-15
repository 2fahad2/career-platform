"""Thin shim for the systemd timer — all logic lives in career.engine.cli."""

import sys

from career.engine.cli import main

if __name__ == "__main__":
    sys.exit(main())
