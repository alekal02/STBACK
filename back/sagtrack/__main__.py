"""Entrada que o serviço do Render já usa: python -m sagtrack servir."""

from __future__ import annotations

import argparse

from inlog_agent.server import servir


def main() -> None:
    parser = argparse.ArgumentParser(prog="sagtrack")
    sub = parser.add_subparsers(dest="comando")
    web = sub.add_parser("servir")
    web.add_argument("--porta", type=int, default=None)
    web.add_argument("--host", default=None)
    args = parser.parse_args()
    if args.comando not in (None, "servir"):
        parser.error("No servidor use: python -m sagtrack servir")
    servir(porta=getattr(args, "porta", None), host=getattr(args, "host", None))


if __name__ == "__main__":
    main()
