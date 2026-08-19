#!/usr/bin/env python3
"""Build system detection and macro extraction.

Detects build configuration files (CMake, Make, Spec, Meson, Autotools,
Kconfig, Bazel, MSBuild) and extracts compile definitions (-D flags),
target lists, source file lists, include directories, and target dependencies.

Used by code2database_builder to resolve #ifdef branches and improve accuracy.
"""

import os
import re
import sys
from typing import Optional


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

class BuildInfo:
    """Detected build system information."""

    def __init__(self, source_root: str):
        self.source_root = source_root
        self.build_system = ""        # cmake, make, spec, meson, autotools, kconfig, bazel, msbuild
        self.config_files = []        # paths to detected build files
        self.macros = {}              # macro_name → value (empty string for flag macros)
        self.targets = []             # [{"name", "type", "sources", "depends_on"}]
        self.include_dirs = []        # include directory paths
        self.build_types = {}         # config_name → {macro: value} (e.g. Debug/Release)
        self.config_h_files = []      # generated config header files found

    def __repr__(self):
        return (f"BuildInfo(system={self.build_system}, config_files={self.config_files}, "
                f"macros={len(self.macros)}, targets={len(self.targets)}, "
                f"include_dirs={len(self.include_dirs)}, build_types={list(self.build_types.keys())})")


# ---------------------------------------------------------------------------
# Build system detectors
# ---------------------------------------------------------------------------

class _CMakeDetector:
    """Parse CMakeLists.txt and *.cmake files for compile definitions."""

    _DEFINE_RE = re.compile(
        r'(?:target_compile_definitions|add_definitions|target_compile_options)\s*'
        r'\(.*?(-D\s*)(\w+(?:=\S+)?)[\s\)]', re.IGNORECASE | re.DOTALL)
    _CACHE_VAR_RE = re.compile(
        r'set\s*\(\s*(\w+)\s+("(?:[^"\\]|\\.)*"|\S+)\s+CACHE\s', re.IGNORECASE)
    _CONFIGURE_H_RE = re.compile(
        r'configure_file\s*\(\s*(\S+)[\s)]', re.IGNORECASE)
    _TARGET_RE = re.compile(
        r'add_(library|executable)\s*\(\s*(\w+)', re.IGNORECASE)
    _INCLUDE_DIR_RE = re.compile(
        r'target_include_directories\s*\(\s*\w+\s+(?:PUBLIC|PRIVATE|INTERFACE)\s+(.*?)(?:\))',
        re.IGNORECASE | re.DOTALL)
    _BUILD_TYPE_MACROS = {
        "Debug": {"_DEBUG": "", "DEBUG": "", "ENABLE_DEBUG": "1"},
        "Release": {"NDEBUG": "", "OPTIMIZE": ""},
        "RelWithDebInfo": {"NDEBUG": "", "RELWITHDEBINFO": ""},
        "MinSizeRel": {"NDEBUG": "", "MINSIZEREL": ""},
    }

    def detect(self, source_root: str) -> list[str]:
        hits = []
        for root, _dirs, files in os.walk(source_root):
            for f in files:
                if f == 'CMakeLists.txt' or f.endswith('.cmake'):
                    hits.append(os.path.join(root, f))
            # Don't recurse into build output directories
            _dirs[:] = [d for d in _dirs
                        if not d.startswith('.') and d not in
                        ('build', '_build', 'node_modules')
                        and not d.startswith('cmake-build-')]
        return hits

    def extract(self, config_files: list[str], source_root: str) -> BuildInfo:
        info = BuildInfo(source_root)
        info.build_system = "cmake"
        info.config_files = config_files

        for cf in config_files:
            try:
                with open(cf, 'r', errors='replace') as f:
                    text = f.read()
            except (IOError, OSError):
                continue

            # Extract -D definitions
            for m in self._DEFINE_RE.finditer(text):
                defn = m.group(2).strip()
                if '=' in defn:
                    k, v = defn.split('=', 1)
                    info.macros[k] = v
                else:
                    info.macros[defn] = ""

            # Extract cache variables (potential build config options)
            for m in self._CACHE_VAR_RE.finditer(text):
                k, v = m.group(1), m.group(2).strip('"')
                if k.startswith('CMAKE_') or k.startswith('ENABLE_') or k.startswith('HAVE_') or k.startswith('WITH_'):
                    info.macros[k] = v

            # Extract targets
            for m in self._TARGET_RE.finditer(text):
                target_type, name = m.group(1), m.group(2)
                info.targets.append({
                    "name": name,
                    "type": target_type,
                    "sources": [],
                    "depends_on": []
                })

            # Extract include dirs
            for m in self._INCLUDE_DIR_RE.finditer(text):
                dirs_text = m.group(1)
                for d in re.findall(r'"([^"]+)"', dirs_text):
                    info.include_dirs.append(d)
                for d in re.findall(r'(\$\{[^}]+\}|\S+)', dirs_text):
                    if not d.startswith('$') and d not in ('PUBLIC', 'PRIVATE', 'INTERFACE', 'SYSTEM', 'BEFORE'):
                        info.include_dirs.append(d)

            # Extract configure_file references
            for m in self._CONFIGURE_H_RE.finditer(text):
                config_h = m.group(1)
                if not config_h.startswith('$'):
                    info.config_h_files.append(config_h)

        # Add build type configurations
        info.build_types = dict(self._BUILD_TYPE_MACROS)
        # Merge project macros into all build types
        for bt_macros in info.build_types.values():
            for k, v in info.macros.items():
                if k not in bt_macros and k.startswith(('HAVE_', 'WITH_', 'ENABLE_')):
                    bt_macros[k] = v

        return info


class _MakeDetector:
    """Parse Makefiles for CFLAGS/CXXFLAGS/CPPFLAGS/DEFINES."""

    _DEFINE_RE = re.compile(
        r'(?:CFLAGS|CXXFLAGS|CPPFLAGS|DEFINES|C_DEFS|CXX_DEFS)\s*[:+?]?=\s*(.*?)(?:\n\s*\n|\n[^ \t]|\Z)',
        re.DOTALL)
    _D_FLAG_RE = re.compile(r'-D\s*(\w+(?:=\S+)?)')
    _TARGET_RE = re.compile(r'^(\w[\w.-]*)\s*:\s*',
                            re.MULTILINE)
    _INCLUDE_RE = re.compile(r'-I\s*(\S+)')

    def detect(self, source_root: str) -> list[str]:
        hits = []
        for root, _dirs, files in os.walk(source_root):
            for f in files:
                if f in ('Makefile', 'GNUmakefile', 'makefile') or f.endswith('.mk'):
                    hits.append(os.path.join(root, f))
            _dirs[:] = [d for d in _dirs
                        if not d.startswith('.') and d not in
                        ('build', '_build', 'node_modules', 'out')]
        return hits

    def extract(self, config_files: list[str], source_root: str) -> BuildInfo:
        info = BuildInfo(source_root)
        info.build_system = "make"
        info.config_files = config_files

        for cf in config_files:
            try:
                with open(cf, 'r', errors='replace') as f:
                    text = f.read()
            except (IOError, OSError):
                continue

            for m in self._DEFINE_RE.finditer(text):
                for dm in self._D_FLAG_RE.finditer(m.group(1)):
                    defn = dm.group(1)
                    if '=' in defn:
                        k, v = defn.split('=', 1)
                        info.macros[k] = v
                    else:
                        info.macros[defn] = ""

            for m in self._INCLUDE_RE.finditer(text):
                info.include_dirs.append(m.group(1))

            # Targets from make rules
            seen = set()
            for m in self._TARGET_RE.finditer(text):
                name = m.group(1)
                if name not in seen and not name.startswith('.') and name not in (
                        'all', 'clean', 'install', 'uninstall', 'dist', 'distclean',
                        'check', 'test', 'help', 'phony'):
                    seen.add(name)
                    info.targets.append({
                        "name": name, "type": "target",
                        "sources": [], "depends_on": []
                    })

        # Build types from typical make patterns
        if "DEBUG" in info.macros or "_DEBUG" in info.macros:
            info.build_types["Debug"] = {"DEBUG": "", "_DEBUG": ""}
        if "NDEBUG" in info.macros:
            info.build_types["Release"] = {"NDEBUG": ""}

        return info


class _SpecDetector:
    """Parse RPM .spec files for %define, %configure, %build -D flags."""

    _DEFINE_RE = re.compile(r'%define\s+(\w+)\s+(.*)', re.MULTILINE)
    _GLOBAL_RE = re.compile(r'%global\s+(\w+)\s+(.*)', re.MULTILINE)
    _CONFIGURE_D_RE = re.compile(r'-D\s*(\w+(?:=\S+)?)')
    _BUILD_SECTION_RE = re.compile(r'%build\s*(.*?)%(?:install|check|clean|pre|post|files|\Z)',
                                   re.DOTALL | re.IGNORECASE)

    def detect(self, source_root: str) -> list[str]:
        hits = []
        for root, _dirs, files in os.walk(source_root):
            for f in files:
                if f.endswith('.spec') or f.endswith('.spec.in'):
                    hits.append(os.path.join(root, f))
            _dirs[:] = [d for d in _dirs
                        if not d.startswith('.') and d not in
                        ('node_modules', 'SOURCES', 'SPECS', 'RPMS', 'SRPMS')]
        return hits

    def extract(self, config_files: list[str], source_root: str) -> BuildInfo:
        info = BuildInfo(source_root)
        info.build_system = "spec"
        info.config_files = config_files

        for cf in config_files:
            try:
                with open(cf, 'r', errors='replace') as f:
                    text = f.read()
            except (IOError, OSError):
                continue

            # Extract %define / %global
            for m in self._DEFINE_RE.finditer(text):
                k, v = m.group(1), m.group(2).strip()
                info.macros[k] = v
            for m in self._GLOBAL_RE.finditer(text):
                k, v = m.group(1), m.group(2).strip()
                info.macros[k] = v

            # Extract -D flags from %build section
            bm = self._BUILD_SECTION_RE.search(text)
            if bm:
                for dm in self._CONFIGURE_D_RE.finditer(bm.group(1)):
                    defn = dm.group(1)
                    if '=' in defn:
                        k, v = defn.split('=', 1)
                        info.macros[k] = v
                    else:
                        info.macros[defn] = ""

        # Spec files typically have debug/release builds
        info.build_types = {
            "Release": {"NDEBUG": ""},
            "Debug": {"DEBUG": "", "_DEBUG": ""},
        }

        return info


class _MesonDetector:
    """Parse meson.build and meson_options.txt for compile definitions."""

    _DEF_RE = re.compile(r'(?:add_project_arguments|add_global_arguments)\s*\(\s*.*?(-D\s*)(\w+(?:=\S+)?)[\s,)]',
                         re.IGNORECASE | re.DOTALL)
    _CONF_SET_RE = re.compile(r'conf\.set\s*\(\s*[\'"](\w+)[\'"]\s*,\s*(.*?)\)', re.IGNORECASE)
    _TARGET_RE = re.compile(r'(?:library|executable|shared_library|static_library)\s*\(\s*[\'"](\w+)[\'"]',
                            re.IGNORECASE)
    _INCLUDE_RE = re.compile(r'include_directories\s*\(\s*[\'"]([^\'"]+)[\'"]', re.IGNORECASE)

    def detect(self, source_root: str) -> list[str]:
        hits = []
        for root, _dirs, files in os.walk(source_root):
            for f in files:
                if f in ('meson.build', 'meson_options.txt'):
                    hits.append(os.path.join(root, f))
            _dirs[:] = [d for d in _dirs
                        if not d.startswith('.') and d not in
                        ('node_modules', 'builddir', 'build')]
        return hits

    def extract(self, config_files: list[str], source_root: str) -> BuildInfo:
        info = BuildInfo(source_root)
        info.build_system = "meson"
        info.config_files = config_files

        for cf in config_files:
            try:
                with open(cf, 'r', errors='replace') as f:
                    text = f.read()
            except (IOError, OSError):
                continue

            for m in self._DEF_RE.finditer(text):
                defn = m.group(2).strip()
                if '=' in defn:
                    k, v = defn.split('=', 1)
                    info.macros[k] = v
                else:
                    info.macros[defn] = ""

            for m in self._CONF_SET_RE.finditer(text):
                k = m.group(1)
                v = m.group(2).strip().strip("'\"")
                info.macros[k] = v

            for m in self._TARGET_RE.finditer(text):
                info.targets.append({
                    "name": m.group(1), "type": "target",
                    "sources": [], "depends_on": []
                })

            for m in self._INCLUDE_RE.finditer(text):
                info.include_dirs.append(m.group(1))

        return info


class _AutotoolsDetector:
    """Parse configure.ac and Makefile.am for AC_DEFINE and DEFINES."""

    _AC_DEFINE_RE = re.compile(r'AC_DEFINE\s*\(\s*\[?(\w+)\]?\s*,\s*\[?(\S+?)\]?\s*',
                               re.IGNORECASE)
    _CONFIGURE_D_RE = re.compile(r'-D\s*(\w+(?:=\S+)?)')

    def detect(self, source_root: str) -> list[str]:
        hits = []
        for root, _dirs, files in os.walk(source_root):
            for f in files:
                if f in ('configure.ac', 'configure.in', 'Makefile.am'):
                    hits.append(os.path.join(root, f))
            _dirs[:] = [d for d in _dirs
                        if not d.startswith('.') and d not in
                        ('node_modules', 'autom4te.cache')]
        return hits

    def extract(self, config_files: list[str], source_root: str) -> BuildInfo:
        info = BuildInfo(source_root)
        info.build_system = "autotools"
        info.config_files = config_files

        for cf in config_files:
            try:
                with open(cf, 'r', errors='replace') as f:
                    text = f.read()
            except (IOError, OSError):
                continue

            for m in self._AC_DEFINE_RE.finditer(text):
                k = m.group(1)
                v = m.group(2).strip('[]')
                if v == '':
                    info.macros[k] = ""
                else:
                    info.macros[k] = v

        # Check for generated config.h
        config_h = os.path.join(source_root, 'config.h')
        if os.path.exists(config_h):
            info.config_h_files.append(config_h)
            _parse_config_h(config_h, info.macros)
        config_h_in = os.path.join(source_root, 'config.h.in')
        if os.path.exists(config_h_in):
            info.config_h_files.append(config_h_in)

        return info


class _KconfigDetector:
    """Parse Kconfig and Kbuild for kernel config options."""

    _CONFIG_RE = re.compile(r'config\s+(\w+)', re.IGNORECASE)

    def detect(self, source_root: str) -> list[str]:
        hits = []
        for root, _dirs, files in os.walk(source_root):
            for f in files:
                if f in ('Kconfig', 'Kbuild'):
                    hits.append(os.path.join(root, f))
            _dirs[:] = [d for d in _dirs
                        if not d.startswith('.') and d not in ('node_modules',)]
        # Only if we also find a Makefile (to distinguish kernel projects)
        return hits

    def extract(self, config_files: list[str], source_root: str) -> BuildInfo:
        info = BuildInfo(source_root)
        info.build_system = "kconfig"
        info.config_files = config_files

        # Check for .config file (kernel build config)
        dot_config = os.path.join(source_root, '.config')
        if os.path.exists(dot_config):
            _parse_dot_config(dot_config, info.macros)

        for cf in config_files:
            try:
                with open(cf, 'r', errors='replace') as f:
                    text = f.read()
            except (IOError, OSError):
                continue
            for m in self._CONFIG_RE.finditer(text):
                info.macros[f"CONFIG_{m.group(1)}"] = ""

        return info


class _BazelDetector:
    """Parse BUILD, BUILD.bazel, WORKSPACE for copts defines."""

    _COPTS_RE = re.compile(r'copts\s*=\s*\[(.*?)\]', re.DOTALL)
    _D_FLAG_RE = re.compile(r'"-D\s*(\w+(?:=\S+)?)"')
    _TARGET_RE = re.compile(r'(?:cc_library|cc_binary|cc_test)\s*\(\s*name\s*=\s*"(.*?)"',
                            re.IGNORECASE)

    def detect(self, source_root: str) -> list[str]:
        hits = []
        for root, _dirs, files in os.walk(source_root):
            for f in files:
                if f in ('BUILD', 'BUILD.bazel', 'WORKSPACE', 'WORKSPACE.bazel'):
                    hits.append(os.path.join(root, f))
            _dirs[:] = [d for d in _dirs
                        if not d.startswith('.') and d not in
                        ('node_modules',) and not d.startswith('bazel-')]
        return hits

    def extract(self, config_files: list[str], source_root: str) -> BuildInfo:
        info = BuildInfo(source_root)
        info.build_system = "bazel"
        info.config_files = config_files

        for cf in config_files:
            try:
                with open(cf, 'r', errors='replace') as f:
                    text = f.read()
            except (IOError, OSError):
                continue

            for m in self._COPTS_RE.finditer(text):
                for dm in self._D_FLAG_RE.finditer(m.group(1)):
                    defn = dm.group(1)
                    if '=' in defn:
                        k, v = defn.split('=', 1)
                        info.macros[k] = v
                    else:
                        info.macros[defn] = ""

            for m in self._TARGET_RE.finditer(text):
                info.targets.append({
                    "name": m.group(1), "type": "target",
                    "sources": [], "depends_on": []
                })

        return info


# ---------------------------------------------------------------------------
# Helper: parse config.h / autoconf.h
# ---------------------------------------------------------------------------

def _parse_config_h(path: str, macros: dict):
    """Parse #define lines from a generated config.h file."""
    try:
        with open(path, 'r', errors='replace') as f:
            for line in f:
                line = line.strip()
                if line.startswith('#define'):
                    m = re.match(r'#define\s+(\w+)\s*(.*)', line)
                    if m:
                        k = m.group(1)
                        v = m.group(2).strip()
                        if k.startswith('_') and k.endswith('_'):
                            continue  # Skip include guards
                        macros[k] = v if v else ""
                elif line.startswith('#undef'):
                    m = re.match(r'#undef\s+(\w+)', line)
                    if m:
                        macros.pop(m.group(1), None)
    except (IOError, OSError):
        pass


def _parse_dot_config(path: str, macros: dict):
    """Parse Linux kernel .config file for CONFIG_* options."""
    try:
        with open(path, 'r', errors='replace') as f:
            for line in f:
                line = line.strip()
                if line.startswith('CONFIG_'):
                    if '=' in line:
                        k, v = line.split('=', 1)
                        macros[k] = v
                    elif line.startswith('# CONFIG_'):
                        # Disabled option: # CONFIG_FOO is not set
                        pass
    except (IOError, OSError):
        pass


# ---------------------------------------------------------------------------
# Main detector class
# ---------------------------------------------------------------------------

class BuildDetector:
    """Detect build systems and extract macro definitions."""

    def __init__(self):
        self._detectors = [
            ("cmake", _CMakeDetector()),
            ("make", _MakeDetector()),
            ("spec", _SpecDetector()),
            ("meson", _MesonDetector()),
            ("autotools", _AutotoolsDetector()),
            ("kconfig", _KconfigDetector()),
            ("bazel", _BazelDetector()),
        ]

    def detect(self, source_root: str) -> BuildInfo:
        """Auto-detect build system and extract information.

        Returns BuildInfo with the best matching build system.
        If multiple build systems are found, merges macros from all.
        """
        best_info = BuildInfo(source_root)
        all_infos = []

        for name, detector in self._detectors:
            config_files = detector.detect(source_root)
            if config_files:
                info = detector.extract(config_files, source_root)
                all_infos.append(info)

        if not all_infos:
            # Check for generated config.h files even without build system detection
            self._scan_config_headers(source_root, best_info.macros)
            return best_info

        # Use the first detected system as primary
        best_info = all_infos[0]

        # Merge macros from additional build systems
        for info in all_infos[1:]:
            for k, v in info.macros.items():
                if k not in best_info.macros:
                    best_info.macros[k] = v
            best_info.config_files.extend(info.config_files)
            best_info.targets.extend(info.targets)
            best_info.include_dirs.extend(info.include_dirs)

        # Also scan for config.h files
        self._scan_config_headers(source_root, best_info.macros)

        # Deduplicate include dirs
        seen = set()
        best_info.include_dirs = [d for d in best_info.include_dirs
                                  if d not in seen and not seen.add(d)]

        return best_info

    def _scan_config_headers(self, source_root: str, macros: dict):
        """Scan for generated config.h / autoconf.h / configuration.h files."""
        config_names = ('config.h', 'config.hpp', 'autoconf.h', 'configuration.h',
                        'build-config.h', 'project-config.h', 'llvm-config.h')
        for root, _dirs, files in os.walk(source_root):
            for f in files:
                if f in config_names:
                    path = os.path.join(root, f)
                    # Only parse if it looks like a generated file (has #define)
                    _parse_config_h(path, macros)
            _dirs[:] = [d for d in _dirs
                        if not d.startswith('.') and d not in
                        ('node_modules', 'build', '_build', 'out', 'dist', 'target')
                        and not d.startswith('cmake-build-')]

    def prompt_user(self, info: BuildInfo) -> dict:
        """Interactive prompt when build configuration is ambiguous.

        Returns final macro bindings dict.

        Note: This method blocks on `input()` and must ONLY be called from an
        interactive terminal. For non-interactive contexts (CI, MCP server,
        background scripts, kernel scans launched by an agent), use
        `select_macros_auto(info, ...)` instead — it never blocks.
        """
        if not info.build_system:
            print(f"\nNo build system configuration found in {info.source_root}")
            print("How should #ifdef branches be handled?")
            print("  [1] Parse all branches (default — may include dead code)")
            print("  [2] Enable all explicitly-seen macros in source code #ifdefs")
            print("  [3] Disable all macros (only unconditional code)")
            print("  [4] Specify macros manually: -D MACRO1 -D MACRO2=VALUE")
            choice = input("Select [1-4] (default: 1): ").strip() or "1"
            if choice == "2":
                return {"_all_source_macros": True}
            elif choice == "3":
                return {"_no_macros": True}
            elif choice == "4":
                macros_str = input("Enter macros (e.g., NDEBUG FEATURE_X=1): ").strip()
                return _parse_user_macros(macros_str)
            else:
                return {}  # Parse all branches

        # Build system detected — show info
        print(f"\nDetected build system: {info.build_system} ({', '.join(os.path.basename(f) for f in info.config_files)})")

        if info.macros:
            macro_list = sorted(info.macros.items())
            print(f"Found {len(macro_list)} compile definitions:")
            for k, v in macro_list[:20]:
                print(f"  {k}={v}" if v else f"  {k}")
            if len(macro_list) > 20:
                print(f"  ... and {len(macro_list) - 20} more")

        if info.build_types:
            print("\nBuild configurations:")
            for i, (name, macros) in enumerate(info.build_types.items(), 1):
                macro_names = [k for k in macros if not k.startswith(('HAVE_', 'WITH_', 'ENABLE_'))]
                print(f"  [{i}] {name} — defines: {', '.join(macro_names[:8])}")
            all_idx = len(info.build_types) + 1
            none_idx = all_idx + 1
            manual_idx = none_idx + 1
            print(f"  [{all_idx}] All macros enabled (union of all configurations)")
            print(f"  [{none_idx}] No macros (ignore all #ifdef branches)")
            print(f"  [{manual_idx}] Specify macros manually")

            default = "2" if "Release" in info.build_types else "1"
            choice = input(f"Select [1-{manual_idx}] (default: {default}): ").strip() or default

            try:
                idx = int(choice)
            except ValueError:
                idx = int(default)

            if 1 <= idx <= len(info.build_types):
                config_name = list(info.build_types.keys())[idx - 1]
                selected = info.build_types[config_name]
                # Merge project macros
                for k, v in info.macros.items():
                    if k.startswith(('HAVE_', 'WITH_', 'ENABLE_')) and k not in selected:
                        selected[k] = v
                selected["_selected_config"] = config_name
                return selected
            elif idx == all_idx:
                # Union of all macros
                merged = dict(info.macros)
                for bt_macros in info.build_types.values():
                    merged.update(bt_macros)
                merged["_selected_config"] = "all"
                return merged
            elif idx == none_idx:
                return {"_no_macros": True}
            elif idx == manual_idx:
                macros_str = input("Enter macros (e.g., NDEBUG FEATURE_X=1): ").strip()
                result = _parse_user_macros(macros_str)
                result["_selected_config"] = "manual"
                return result
            else:
                return dict(info.macros)

        # No build types — just use detected macros
        if info.macros:
            print("\nNo build configurations found. Using detected macros as-is.")
            print("Press Enter to accept, or type macros to override (e.g., NDEBUG FEATURE_X=1):")
            override = input("> ").strip()
            if override:
                return _parse_user_macros(override)
            return dict(info.macros)

        # No macros at all
        print("\nNo compile definitions found in build system.")
        print("  [1] Parse all #ifdef branches (default)")
        print("  [2] Specify macros manually")
        choice = input("Select [1-2] (default: 1): ").strip() or "1"
        if choice == "2":
            macros_str = input("Enter macros (e.g., NDEBUG FEATURE_X=1): ").strip()
            return _parse_user_macros(macros_str)
        return {}

    def select_macros_auto(self, info: BuildInfo,
                           prefer_build_type: Optional[str] = None,
                           extra_macros: Optional[str] = None,
                           strategy: str = "auto") -> dict:
        """Non-interactive macro selection — never blocks on input().

        Args:
            info: BuildInfo from `detect()`.
            prefer_build_type: If set and present in `info.build_types`, select
                this build type (e.g., "Release", "Debug"). None = auto-pick.
            extra_macros: Optional space-separated macros (e.g. "NDEBUG FOO=1")
                merged on top of selected macros.
            strategy: "auto" (default) | "all" (union of all build types) |
                "none" (no macros — parse only unconditional code).

        Returns:
            Macro bindings dict, same shape as `prompt_user`.
        """
        if strategy == "none":
            return {"_no_macros": True}
        if strategy == "all":
            merged = dict(info.macros)
            for bt_macros in info.build_types.values():
                merged.update(bt_macros)
            merged["_selected_config"] = "all"
            if extra_macros:
                merged.update(_parse_user_macros(extra_macros))
            return merged

        # "auto" strategy
        if prefer_build_type and prefer_build_type in info.build_types:
            selected = dict(info.build_types[prefer_build_type])
            for k, v in info.macros.items():
                if k.startswith(('HAVE_', 'WITH_', 'ENABLE_')) and k not in selected:
                    selected[k] = v
            selected["_selected_config"] = prefer_build_type
        elif "Release" in info.build_types:
            selected = dict(info.build_types["Release"])
            for k, v in info.macros.items():
                if k.startswith(('HAVE_', 'WITH_', 'ENABLE_')) and k not in selected:
                    selected[k] = v
            selected["_selected_config"] = "Release"
        elif info.build_types:
            first_name = next(iter(info.build_types))
            selected = dict(info.build_types[first_name])
            for k, v in info.macros.items():
                if k.startswith(('HAVE_', 'WITH_', 'ENABLE_')) and k not in selected:
                    selected[k] = v
            selected["_selected_config"] = first_name
        elif info.macros:
            selected = dict(info.macros)
            selected["_selected_config"] = "detected"
        else:
            selected = {}  # Parse all branches

        if extra_macros:
            selected.update(_parse_user_macros(extra_macros))
        return selected


def _parse_user_macros(text: str) -> dict:
    """Parse user-supplied macro string like 'NDEBUG FEATURE_X=1 -DFOO'."""
    macros = {}
    for token in text.split():
        token = token.strip()
        if token.startswith('-D'):
            token = token[2:]
        if not token:
            continue
        if '=' in token:
            k, v = token.split('=', 1)
            macros[k] = v
        else:
            macros[token] = ""
    return macros


# ---------------------------------------------------------------------------
# Preprocessor condition evaluator
# ---------------------------------------------------------------------------

def evaluate_pp_condition(condition: str, directive: str, macro_bindings: dict) -> bool:
    """Evaluate a preprocessor condition with given macro bindings.

    Args:
        condition: The text after #if/#ifdef/#ifndef/#elif (e.g., "FEATURE_X" or "MODE > 0")
        directive: The directive type: "if", "ifdef", "ifndef", "elif"
        macro_bindings: dict of macro_name → value

    Returns:
        True if the condition is alive (should be parsed), False if dead.
        Conservative: returns True if unsure.
    """
    if not macro_bindings:
        return True  # No bindings — parse everything

    if macro_bindings.get("_no_macros"):
        # Disable all macros — only #if 1 is alive
        cond = condition.strip()
        if cond == "1" or cond == "true":
            return True
        if directive in ("ifdef", "ifndef"):
            return False
        return False

    if macro_bindings.get("_all_source_macros"):
        # Enable all source-seen macros — assume all #ifdef MACRO are alive
        return True

    cond = condition.strip()

    if directive == "ifdef":
        macro_name = cond.split()[0] if cond.split() else cond
        macro_name = macro_name.lstrip('!')
        return macro_name in macro_bindings

    if directive == "ifndef":
        macro_name = cond.split()[0] if cond.split() else cond
        macro_name = macro_name.lstrip('!')
        return macro_name not in macro_bindings

    if directive in ("if", "elif"):
        # Try to evaluate simple expressions
        # Handle: MACRO, !MACRO, MACRO == value, MACRO != value, defined(MACRO)
        result = _eval_pp_expr(cond, macro_bindings)
        return result

    return True  # Conservative


def _has_depth0_op(expr: str, op: str) -> bool:
    """Check if operator appears at parenthesis depth 0."""
    depth = 0
    i = 0
    while i < len(expr):
        if expr[i] == '(':
            depth += 1
        elif expr[i] == ')':
            depth -= 1
        elif depth == 0 and expr[i:i+len(op)] == op:
            return True
        i += 1
    return False


def _split_depth0(expr: str, op: str) -> list:
    """Split expr on first occurrence of operator at parenthesis depth 0."""
    depth = 0
    i = 0
    while i < len(expr):
        if expr[i] == '(':
            depth += 1
        elif expr[i] == ')':
            depth -= 1
        elif depth == 0 and expr[i:i+len(op)] == op:
            return [expr[:i], expr[i+len(op):]]
        i += 1
    return [expr]


def _eval_pp_expr(expr: str, bindings: dict) -> bool:
    """Evaluate a simple preprocessor expression.

    Handles:
    - defined(MACRO) / defined MACRO
    - !defined(MACRO)
    - MACRO (truthy if defined and non-zero)
    - MACRO == value / MACRO != value
    - value1 && value2, value1 || value2 (simple)
    """
    expr = expr.strip()

    # Strip outer parentheses: (A || B) && C → A || B && C at this level
    while expr.startswith('(') and expr.endswith(')'):
        depth = 0
        matched = True
        for i, ch in enumerate(expr):
            if ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
            if depth == 0 and i < len(expr) - 1:
                matched = False
                break
        if matched:
            expr = expr[1:-1].strip()
        else:
            break

    # Single-pass depth-0 operator search: find the first || or && at
    # parenthesis depth 0. || has lower precedence, so if found, split
    # there; otherwise split on &&. Replaces the previous 4-pass approach
    # (_has_depth0_op + _split_depth0 for each of || and &&) with 1 pass.
    depth = 0
    or_pos = -1
    and_pos = -1
    i = 0
    while i < len(expr):
        c = expr[i]
        if c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
        elif depth == 0:
            if i + 1 < len(expr) and expr[i:i+2] == '||':
                or_pos = i
                break  # || is lowest precedence, split here immediately
            if and_pos < 0 and i + 1 < len(expr) and expr[i:i+2] == '&&':
                and_pos = i
        i += 1

    if or_pos >= 0:
        return _eval_pp_expr(expr[:or_pos].strip(), bindings) or \
               _eval_pp_expr(expr[or_pos+2:].strip(), bindings)
    if and_pos >= 0:
        return _eval_pp_expr(expr[:and_pos].strip(), bindings) and \
               _eval_pp_expr(expr[and_pos+2:].strip(), bindings)

    # defined(MACRO) or defined MACRO
    m = re.match(r'!?defined\s*\(\s*(\w+)\s*\)', expr)
    if m:
        defined = m.group(1) in bindings
        if expr.startswith('!'):
            return not defined
        return defined

    m = re.match(r'!?defined\s+(\w+)', expr)
    if m:
        defined = m.group(1) in bindings
        if expr.startswith('!'):
            return not defined
        return defined

    # MACRO == value
    m = re.match(r'(\w+)\s*==\s*(\d+|0x[\da-fA-F]+|\w+)', expr)
    if m:
        macro = m.group(1)
        rhs = m.group(2)
        val = bindings.get(macro)
        if val is None:
            return False  # Undefined macro — condition false
        return val.strip() == rhs or _numeric_eq(val, rhs)

    # MACRO != value
    m = re.match(r'(\w+)\s*!=\s*(\d+|0x[\da-fA-F]+|\w+)', expr)
    if m:
        macro = m.group(1)
        rhs = m.group(2)
        val = bindings.get(macro)
        if val is None:
            return True  # Undefined != value — might be true
        return not _numeric_eq(val, rhs)

    # MACRO > value / MACRO >= value / MACRO < value / MACRO <= value
    m = re.match(r'(\w+)\s*([><]=?)\s*(\d+)', expr)
    if m:
        macro = m.group(1)
        op = m.group(2)
        rhs = int(m.group(3))
        val = bindings.get(macro)
        if val is None:
            return True  # Conservative
        try:
            lhs = int(val, 0)  # BUG 247 fix: handle 0x hex prefix
        except (ValueError, TypeError):
            return True
        if op == '>': return lhs > rhs
        if op == '>=': return lhs >= rhs
        if op == '<': return lhs < rhs
        if op == '<=': return lhs <= rhs

    # Bare macro name (truthy if defined and non-zero)
    m = re.match(r'^!?(\w+)$', expr)
    if m:
        macro = m.group(1)
        is_negated = expr.startswith('!')
        if macro in bindings:
            val = bindings[macro]
            # BUG 246 fix: check for zero in any base (0x0, 0b0, 0, etc.)
            if val == "":
                alive = False
            else:
                try:
                    alive = int(val, 0) != 0
                except (ValueError, TypeError):
                    alive = bool(val)  # Non-numeric non-empty string is truthy
        else:
            alive = False  # Undefined macro is falsy in #if
        return not alive if is_negated else alive

    # Conservative: can't evaluate
    return True


def _numeric_eq(a: str, b: str) -> bool:
    """Compare two numeric strings for equality."""
    try:
        a_val = int(a, 0)  # Handles 0x prefix
        b_val = int(b, 0)
        return a_val == b_val
    except (ValueError, TypeError):
        return a.strip() == b.strip()


# ---------------------------------------------------------------------------
# CLI test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json

    if len(sys.argv) < 2:
        print("Usage: build_detector.py <source_root> [--json] [--interactive]")
        sys.exit(1)

    source_root = sys.argv[1]
    output_json = "--json" in sys.argv
    interactive = "--interactive" in sys.argv

    detector = BuildDetector()
    info = detector.detect(source_root)

    if interactive:
        macros = detector.prompt_user(info)
        print(f"\nSelected macros: {macros}")
    else:
        if info.build_system:
            print(f"Build system: {info.build_system}")
            print(f"Config files: {info.config_files}")
            print(f"Macros: {json.dumps(info.macros, indent=2)}")
            print(f"Targets: {json.dumps(info.targets, indent=2)}")
            print(f"Include dirs: {info.include_dirs}")
            print(f"Build types: {list(info.build_types.keys())}")
        else:
            print("No build system detected")

    if output_json:
        result = {
            "build_system": info.build_system,
            "config_files": info.config_files,
            "macros": info.macros,
            "targets": info.targets,
            "include_dirs": info.include_dirs,
            "build_types": info.build_types,
        }
        print(json.dumps(result, indent=2))
