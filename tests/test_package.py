from __future__ import annotations

import importlib.metadata

import ohw26_oisst_icechunk as m


def test_version() -> None:
    assert importlib.metadata.version("ohw26_oisst_icechunk") == m.__version__
