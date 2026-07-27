"""Minimal, dependency-free .env loader.

Reads KEY=VALUE lines from a .env file next to this module and populates
os.environ. Imported first by the CLI entrypoints so that module-level
os.environ reads (e.g. hubspot_client.TOKEN, llm.PROVIDER) see the values.

A real shell environment variable always wins over the .env file, so you can
still override any single value on the command line.
"""

import os


def load(path=None):
    if path is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip()
            if val[:1] in ("'", '"') and val[-1:] == val[:1]:
                val = val[1:-1]                     # quoted value: keep verbatim
            else:
                val = val.split(" #", 1)[0].strip()  # strip trailing inline comment
            os.environ.setdefault(key, val)          # shell env wins over .env


load()
