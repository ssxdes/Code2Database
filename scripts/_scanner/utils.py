"""Shared utilities for code graph scanners.

Language-independent helpers: domain classification, extension mapping,
Python version compatibility.
"""

import os
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Python version compatibility
# ---------------------------------------------------------------------------

MIN_PYTHON = (3, 8)

def check_python_version():
    """Exit with message if Python version is too old."""
    if sys.version_info < MIN_PYTHON:
        sys.exit(
            f"Error: Code2Database requires Python >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]}, "
            f"got {sys.version_info.major}.{sys.version_info.minor}.\n"
            f"Please upgrade Python or use a virtual environment with Python 3.8+."
        )


# ---------------------------------------------------------------------------
# Architecture domain classification (language-independent)
# ---------------------------------------------------------------------------

def classify_domain(filepath: str, source_root: str) -> str:
    try:
        rel = os.path.relpath(filepath, source_root)
    except ValueError:
        rel = filepath
    parts = Path(rel).parts
    domain_parts = list(parts[:-1]) if len(parts) > 1 else ["root"]
    return ".".join(domain_parts)


# ---------------------------------------------------------------------------
# File extension → language mapping
# ---------------------------------------------------------------------------

EXTENSION_MAP = {
    ".c": "c", ".h": "c",
    ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".h++": "cpp",
    ".go": "go",
    ".py": "python", ".pyw": "python",
    ".java": "java",
    ".rs": "rust",
    ".s": "asm", ".S": "asm", ".asm": "asm",
}

LANG_EXTENSIONS = {
    "c": {".c", ".h"},
    "cpp": {".cpp", ".cc", ".cxx", ".hpp", ".h++"},
    "go": {".go"},
    "python": {".py", ".pyw"},
    "java": {".java"},
    "rust": {".rs"},
    "asm": {".s", ".S", ".asm"},
}
