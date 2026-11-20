#!/usr/bin/env python3
"""Local (no-Docker) launcher for development and for hosts without Docker.

    python scripts/run_local.py --data /var/lib/importer \
        --seed samples/image-v1.tar --demo samples --port 8080
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, default=Path("var/data"))
    parser.add_argument("--seed", type=Path, default=Path("samples/image-v1.tar"))
    parser.add_argument("--demo", type=Path, default=Path("samples"))
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    os.environ.setdefault("DATA_DIR", str(args.data.resolve()))
    if args.seed.exists():
        os.environ.setdefault("SEED_INITIAL_IMAGE", str(args.seed.resolve()))
    if args.demo.is_dir():
        os.environ.setdefault("DEMO_DIR", str(args.demo.resolve()))
        os.environ.setdefault("ENABLE_DEMO", "true")
    os.environ.setdefault("SEED_REQUIRED", "true")

    import uvicorn

    uvicorn.run("app.main:app", host=args.host, port=args.port, app_dir=str(Path(__file__).resolve().parents[1]))


if __name__ == "__main__":
    sys.exit(main())
