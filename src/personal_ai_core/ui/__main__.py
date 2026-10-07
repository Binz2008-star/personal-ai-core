from __future__ import annotations

import argparse
from pathlib import Path

from .server import serve


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Personal AI Core local web UI")
    parser.add_argument("--port", type=int, default=0, help="local port; 0 chooses a free port")
    parser.add_argument("--data-dir", type=Path, default=None, help="app-owned document directory")
    args = parser.parse_args()
    serve(data_dir=args.data_dir, port=args.port)


if __name__ == "__main__":
    main()
