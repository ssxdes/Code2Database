#!/usr/bin/env bash
# Code2Database dependency setup script
#
# Usage:
#   bash scripts/setup.sh                          # install all languages + core
#   bash scripts/setup.sh --languages c,go         # install only C and Go grammars
#   bash scripts/setup.sh --languages c,cpp,rust   # install C, C++, Rust only
#   bash scripts/setup.sh --languages all          # same as default (all grammars)
#   bash scripts/setup.sh --with-optional          # also install libclang + z3-solver + igraph/leidenalg
#
# Engineers focused on a single language can skip the others — keeps the install
# lean and avoids pulling tree-sitter grammars they will never use.
#
# Environment:
#   C2D_LANGUAGES  Comma-separated language list (same as --languages)
set -e

echo "=== Code2Database setup ==="

# Check Python
if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 not found. Please install Python 3.9+."
    exit 1
fi

PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "Python version: $PY_VER"

# --- Argument parsing ---
LANGUAGES="${C2D_LANGUAGES:-all}"
WITH_OPTIONAL=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --languages=*)   LANGUAGES="${1#--languages=}" ;;
        --languages)     shift; LANGUAGES="$1" ;;
        --with-optional) WITH_OPTIONAL=1 ;;
        --help|-h)
            echo "Usage: setup.sh [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --languages LIST  Comma-separated language list (default: all)"
            echo "                    Valid: c, cpp, go, python, java, rust, all"
            echo "  --with-optional   Also install libclang, z3-solver, python-igraph, leidenalg"
            echo "  --help            Show this help"
            echo ""
            echo "Environment:"
            echo "  C2D_LANGUAGES  Same as --languages"
            echo ""
            echo "Examples:"
            echo "  bash scripts/setup.sh --languages c,go"
            echo "  bash scripts/setup.sh --languages c,cpp,rust --with-optional"
            exit 0
            ;;
        *) echo "Unknown argument: $1. Use --help for usage."; exit 1 ;;
    esac
    shift
done

# --- Normalize language list ---
LANG_LIST=$(echo "$LANGUAGES" | tr ',' ' ' | tr 'A-Z' 'a-z')
if [ "$LANG_LIST" = "all" ] || [ -z "$LANG_LIST" ]; then
    LANG_LIST="c cpp go python java rust"
fi

# --- Build the install list ---
GRAMMARS=()
for lang in $LANG_LIST; do
    case "$lang" in
        c)        GRAMMARS+=("tree-sitter-c>=0.21") ;;
        cpp|c++)  GRAMMARS+=("tree-sitter-cpp>=0.22") ;;
        go)       GRAMMARS+=("tree-sitter-go>=0.21") ;;
        python)   GRAMMARS+=("tree-sitter-python>=0.21") ;;
        java)     GRAMMARS+=("tree-sitter-java>=0.21") ;;
        rust)     GRAMMARS+=("tree-sitter-rust>=0.21") ;;
        *) echo "WARN: unknown language '$lang' (skipping). Valid: c, cpp, go, python, java, rust" ;;
    esac
done

echo "Languages selected: ${LANG_LIST}"
echo "Grammars to install: ${GRAMMARS[*]}"

# --- Install core + selected grammars ---
CORE_PKGS=("networkx>=3.0" "tree-sitter>=0.22")
ALL_PKGS=("${CORE_PKGS[@]}" "${GRAMMARS[@]}")

python3 -m pip install "${ALL_PKGS[@]}" -q 2>/dev/null || {
    echo "WARN: pinned-version install failed, retrying with unpinned names..."
    UNPINNED=("networkx" "tree-sitter")
    for g in "${GRAMMARS[@]}"; do
        # Strip version specifier (e.g. "tree-sitter-c>=0.21" → "tree-sitter-c")
        UNPINNED+=("$(echo "$g" | sed 's/[>=<].*//')")
    done
    python3 -m pip install "${UNPINNED[@]}" -q
}

# --- Optional advanced features ---
if [ "$WITH_OPTIONAL" -eq 1 ]; then
    echo ""
    echo "Installing optional advanced features (--with-optional)..."
    python3 -m pip install "python-igraph>=0.11" "leidenalg>=0.10" "libclang>=17.0" "z3-solver>=4.12" -q 2>/dev/null || {
        echo "WARN: some optional packages failed to install — continuing."
    }
fi

# --- Verify core imports ---
echo ""
echo "Verifying imports..."
python3 -c "import networkx; print(f'  networkx: {networkx.__version__}')" || echo "  WARNING: networkx not available"
python3 -c "import tree_sitter; print(f'  tree-sitter: {tree_sitter.__version__}')" || echo "  WARNING: tree-sitter base not available"

# --- Verify per-language grammars (only those the user requested) ---
for lang in $LANG_LIST; do
    case "$lang" in
        c)
            python3 -c "import tree_sitter_c; print('  tree-sitter-c: OK')" 2>/dev/null \
                || echo "  WARNING: tree-sitter-c not available (C scanning disabled)" ;;
        cpp|c++)
            python3 -c "import tree_sitter_cpp; print('  tree-sitter-cpp: OK')" 2>/dev/null \
                || echo "  WARNING: tree-sitter-cpp not available (C++ scanning disabled)" ;;
        go)
            python3 -c "import tree_sitter_go; print('  tree-sitter-go: OK')" 2>/dev/null \
                || echo "  WARNING: tree-sitter-go not available (Go scanning disabled)" ;;
        python)
            python3 -c "import tree_sitter_python; print('  tree-sitter-python: OK')" 2>/dev/null \
                || echo "  WARNING: tree-sitter-python not available (Python scanning disabled)" ;;
        java)
            python3 -c "import tree_sitter_java; print('  tree-sitter-java: OK')" 2>/dev/null \
                || echo "  WARNING: tree-sitter-java not available (Java scanning disabled)" ;;
        rust)
            python3 -c "import tree_sitter_rust; print('  tree-sitter-rust: OK')" 2>/dev/null \
                || echo "  WARNING: tree-sitter-rust not available (Rust scanning disabled)" ;;
    esac
done

# --- Optional packages status (informational, not required) ---
echo ""
echo "Optional packages (advanced features):"
python3 -c "import igraph; print(f'  python-igraph: {igraph.__version__}')" 2>/dev/null \
    || echo "  python-igraph: not installed (community detection will use domain fallback)"
python3 -c "import leidenalg; print('  leidenalg: OK')" 2>/dev/null \
    || echo "  leidenalg: not installed (community detection will use domain fallback)"
python3 -c "import clang.cindex; print('  libclang: OK')" 2>/dev/null \
    || echo "  libclang: not installed (cgdb clang backend disabled; tree-sitter-only mode)"
python3 -c "import z3; print(f'  z3-solver: {z3.get_version_string()}')" 2>/dev/null \
    || echo "  z3-solver: not installed (path-feasible uses heuristic fallback)"

echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "  - To enable cgdb clang backend (recommended for C/C++): pip install libclang==17.0.6"
echo "  - To enable sound path feasibility: pip install z3-solver"
echo "  - To re-run with different languages: bash scripts/setup.sh --languages c,cpp"
