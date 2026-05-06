#!/usr/bin/env python3
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from app.stage_a.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
