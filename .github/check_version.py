"""Refuse to publish a release whose tag is not the version in the code.

PyPI accepts a version number once. Not corrected afterwards, not replaced -
only yanked, which leaves it visible and installable by exact pin. So the one
check worth having before an upload is that the number about to be published is
the number the code says it is.
"""
import os
import pathlib
import re
import sys

init = pathlib.Path("simple_harness/__init__.py").read_text(encoding="utf-8")
found = re.search(r'^__version__ = "([^"]+)"', init, re.M)
if not found:
    sys.exit("simple_harness/__init__.py has no __version__")
version = found.group(1)
print(f"simple_harness.__version__ = {version}")

if os.environ.get("EVENT") == "release":
    tag = os.environ.get("TAG", "").lstrip("v")
    if tag != version:
        sys.exit(f"tag '{tag}' does not match __version__ '{version}' - "
                 f"fix one of them before releasing")
    print(f"tag matches: {tag}")
