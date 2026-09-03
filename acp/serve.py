"""Minimal standalone ACP server entrypoint.

Every other module in this package is exercised in-process, by its own
test suite or by an embedding caller that builds a `Coordinator` and
drives it directly. This module is the wiring that lets ACP run as a
standalone process for a genuine out-of-process client (a future
Phase-4 host adapter, or interface/v1's own conformance tests) to talk to
over a real socket -- mirroring `aalp/serve.py` exactly. It owns no
compression policy of its own: it only constructs `Coordinator` from
`--aalp-root`/`--root`, wraps `acp.http_api.build_handler(coordinator)`
in `acp.ingress.Ingress`, and blocks until interrupted.
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
from pathlib import Path

from .coordinator import Coordinator
from .http_api import build_handler
from .ingress import Ingress


def _default_aalp_root() -> str | None:
    return os.environ.get("AALP_HOME")


def build_ingress(
    aalp_root: str | Path | None = None,
    root: str | Path | None = None,
    host: str = "127.0.0.1",
    port: int = 0,
) -> Ingress:
    """Construct a `Coordinator` and the `Ingress` that serves it.

    Returned unstarted; the caller decides when to `start()`/`stop()`.

    `aalp_root` is ACP's own out-of-band knowledge of where AALP is
    rooted -- mirroring `acp.aalp_client.AalpClient`'s own constructor
    parameter, interface v1 defines no discovery operation for an
    unknown root. Unlike `aalp/serve.py`'s `--providers-dir` (which
    falls back to a real path inside its own repository),
    `aalp_root` never defaults to a guessed sibling directory: it must
    be supplied explicitly, or via the `AALP_HOME` environment variable
    (AALP's own variable, read here only as a convenience default, the
    same way `acp.aalp_client` never reads it directly but a caller of
    this function may reasonably want to).
    """
    resolved_aalp_root = aalp_root if aalp_root is not None else _default_aalp_root()
    if resolved_aalp_root is None:
        raise ValueError(
            "aalp_root is required: pass --aalp-root, or set the AALP_HOME "
            "environment variable to the AALP instance's root directory"
        )
    coordinator = Coordinator(resolved_aalp_root, root)
    return Ingress(
        build_handler(coordinator),
        root=root,
        host=host,
        port=port,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m acp", description=__doc__)
    parser.add_argument(
        "--aalp-root", type=str, default=None,
        help="Root directory of the AALP instance ACP compresses through, "
             "i.e. where its .aalp/ lives (default: AALP_HOME env var; "
             "required if unset -- ACP never guesses a sibling directory).")
    parser.add_argument(
        "--root", type=str, default=None,
        help="ACP state root, i.e. where .acp/ lives (default: ACP_HOME "
             "env var, else the current working directory).")
    parser.add_argument("--host", type=str, default="127.0.0.1")
    parser.add_argument(
        "--port", type=int, default=0,
        help="0 (default) binds an OS-assigned ephemeral port, published "
             "via .acp/state/ingress.json for clients to discover.")
    args = parser.parse_args(argv)

    try:
        ingress = build_ingress(
            aalp_root=args.aalp_root,
            root=args.root,
            host=args.host,
            port=args.port,
        )
    except ValueError as exc:
        print(f"acp: {exc}", file=sys.stderr)
        return 2

    ingress.start()
    print(f"acp: listening on {args.host}:{ingress.port}", file=sys.stderr)

    stop_event = threading.Event()

    def _handle_signal(signum: int, frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    stop_event.wait()
    ingress.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
