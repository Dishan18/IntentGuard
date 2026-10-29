"""Run the demo store: python -m intentguard.storefront [--host 127.0.0.1] [--port 8766]

The gateway must be running too (python -m intentguard.gateway).
"""

import argparse

import uvicorn

from intentguard.storefront.app import build_app

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--host", default="127.0.0.1")
parser.add_argument("--port", type=int, default=8766)
args = parser.parse_args()

uvicorn.run(build_app(), host=args.host, port=args.port)
