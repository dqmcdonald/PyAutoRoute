"""Build script for PyAutoRoute's optional Cython A* extension.

The package is otherwise configured in ``pyproject.toml``; this file exists only
to build the optional native A* core (``pyautoroute._astar_c``). The build is
**best-effort**: if Cython or numpy is unavailable at build time, the extension
is simply skipped and the package installs as pure Python (``router`` falls back
to its optimised Python A*).

Build the extension in place after installing the build deps::

    pip install -e ".[fast]"
    python setup.py build_ext --inplace
"""

from setuptools import setup

ext_modules = []
try:
    import numpy
    from Cython.Build import cythonize
    from setuptools import Extension

    ext_modules = cythonize(
        [
            Extension(
                "pyautoroute._astar_c",
                ["pyautoroute/_astar_c.pyx"],
                include_dirs=[numpy.get_include()],
                define_macros=[("NPY_NO_DEPRECATED_API", "NPY_1_7_API_VERSION")],
            )
        ],
        compiler_directives={"language_level": "3"},
    )
except Exception as exc:  # pragma: no cover - build-environment dependent
    import sys

    print(f"pyautoroute: skipping optional Cython A* extension ({exc})",
          file=sys.stderr)

setup(ext_modules=ext_modules)
