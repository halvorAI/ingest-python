"""Command line: `halvorai-ingest demo` and `halvorai-ingest init` (or `python -m halvorai_ingest ...`)."""

from __future__ import annotations

import argparse
import getpass
import os
import sys

from .demo import run_demo
from .init import run_init


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="halvorai-ingest")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("demo", help="sign one event offline and verify it; no account needed")
    init = commands.add_parser(
        "init",
        help="register this machine as a source, write .env.local and send a test event",
    )
    init.add_argument("--api-key", help="hk_test_… or hk_live_… (default: HALVOR_API_KEY, else asks)")
    init.add_argument("--endpoint", help="API base URL (default: HALVOR_ENDPOINT, else from the key)")
    init.add_argument("--dashboard", help="dashboard origin for the printed link")
    init.add_argument("--name", help="source name (default: this machine's hostname)")
    init.add_argument("--cwd", default=".", help="folder for .env.local (default: current folder)")
    args = parser.parse_args(argv)

    if args.command == "demo":
        result = run_demo()
        return 0 if result.signed_verified and result.tampered_rejected else 1

    api_key = args.api_key or os.environ.get("HALVOR_API_KEY") or getpass.getpass("API key: ").strip()
    try:
        run_init(
            api_key,
            cwd=args.cwd,
            endpoint=args.endpoint or os.environ.get("HALVOR_ENDPOINT") or None,
            name=args.name,
            dashboard=args.dashboard,
        )
    except Exception as exc:
        print(f"halvorai-ingest init failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
