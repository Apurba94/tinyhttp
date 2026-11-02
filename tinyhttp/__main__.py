"""Serve a folder: python -m tinyhttp [FOLDER] [--port 8000]"""
import argparse
import sys

from . import App


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("folder", nargs="?", default=".")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--host", default="127.0.0.1")
    a = p.parse_args()
    app = App()
    app.static("/", a.folder)
    app.run(a.host, a.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
