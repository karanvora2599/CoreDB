"""Only supplies ext_modules - project metadata lives in pyproject.toml.
Standard pybind11 pattern (see pybind11's own "python_example" template):
setup.py + pyproject.toml coexist, setuptools reads both.

coredb._native is optional acceleration, not a hard dependency - if no C++
toolchain is present, `pip install -e .` still succeeds without it (the
build_ext step just doesn't produce the extension), and coredb/signal.py
falls back to pure Python transparently. See Documentation/ARCHITECTURE.md's
Performance section.
"""
from pybind11.setup_helpers import Pybind11Extension, build_ext
from setuptools import setup

ext_modules = [
    Pybind11Extension(
        "coredb._native",
        ["native/segmentation.cpp"],
        cxx_std=17,
    ),
]

setup(ext_modules=ext_modules, cmdclass={"build_ext": build_ext})
