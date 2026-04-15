# Monorepo namespace-package guard.
#
# When the repo root is on sys.path, Python finds this outer project
# directory before the pip-installed (editable) package. This file
# loads the real inner package and replaces this module in sys.modules,
# while preserving the outer directory in __path__ so pytest can
# still discover tests/ as a subpackage.
import importlib.util as _ilu
import os as _os
import sys as _sys

_outer = _os.path.dirname(_os.path.abspath(__file__))
_inner = _os.path.join(_outer, "dossier_engine")

if _os.path.isdir(_inner) and _os.path.isfile(_os.path.join(_inner, "__init__.py")):
    _spec = _ilu.spec_from_file_location(
        "dossier_engine",
        _os.path.join(_inner, "__init__.py"),
        submodule_search_locations=[_inner, _outer],
    )
    _mod = _ilu.module_from_spec(_spec)
    _mod.__path__ = [_inner, _outer]  # inner first for code, outer for tests
    _sys.modules[__name__] = _mod
    _spec.loader.exec_module(_mod)
