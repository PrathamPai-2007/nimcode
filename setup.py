# Legacy shim for `pip install -e .` on old tooling. All metadata (version,
# dependencies, package data, entry points) lives in pyproject.toml so the
# two can't drift.
from setuptools import setup

setup()
