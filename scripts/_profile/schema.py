"""Profile schema: load, validate, merge, and convert project profiles.

A project profile is a JSON file that declares all project-specific knowledge
the scanner/builder need. The schema is versioned for forward compatibility.

Key operations:
  - load(path): Load a profile JSON, merge with _default.json
  - validate(): Check required fields and types
  - to_scanner_config(): Produce dict consumed by scan_c_file()
  - to_builder_config(): Produce dict consumed by _classify_endpoint() etc.
  - effective_skip_names(): Universal skip + profile additions - visible externals
"""

import json
import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Profile JSON schema version we support
# ---------------------------------------------------------------------------
_SUPPORTED_VERSION = 1

# ---------------------------------------------------------------------------
# Built-in profile directory
# ---------------------------------------------------------------------------
_PROFILE_DIR = Path(__file__).resolve().parent.parent / "config" / "profiles"

# ---------------------------------------------------------------------------
# Default profile embedded as Python dict (avoids reading _default.json at
# import time; used as the base layer that project profiles override).
# ---------------------------------------------------------------------------
_DEFAULT_PROFILE = {
    "version": 1,
    "project": {
        "name": "",
        "language": "c",
        "detected_frameworks": [],
    },
    # C/C++ extraction backend: 'auto' (dual: tree-sitter+clang), 'clang'
    # (cgdb-only, requires libclang), or 'tree-sitter' (legacy). Per cgdb
    # architecture Phase 1. Default 'auto' — uses clang when available,
    # falls back to tree-sitter if libclang missing/unavailable.
    "extraction_backend": "auto",
    "skip_names": {
        "add": [],
        "external_lib_prefixes": {},
        "test_framework_prefixes": [],
        # Project-specific identifiers (macros, logging wrappers, foreach
        # iterators, etc.) that should be filtered out of scenario chains
        # because they aren't real semantic endpoints. The tool stays
        # project-agnostic; project-specific noise names belong here.
        "scenario_noise_names": [],
    },
    "api_detection": {
        "public_prefixes": [],
        "internal_patterns": ["_unit_", "_ut_", "_test_", "_perf_",
                              "_verify_", "_example_", "_internal",
                              "_priv", "_stub", "_mock"],
        "public_header_paths": [],
        "export_macros": [],
        "struct_op_types": [],
        "auto_detect": False,
    },
    "callback_detection": {
        "static_patterns": [],
        "generic_cb_suffixes": ["_cb", "_fn", "_handler", "_callback"],
        "skip_call_prefixes": [],
        "skip_callees": [],
    },
    "endpoint_classification": {
        "lib_prefix_map": {},
        "endpoint_rules": [],
    },
    "macro_heuristics": {
        "macro_condition_prefixes": [],
    },
    "macro_dispatch": {
        "registration_macros": [],
        "token_paste_macros": [],
        "macro_aliases": {},
    },
    "struct_embeddings": {
        "container_of_macros": [],
        "manual_entries": [],
    },
    "threading_models": {},
    "scan_hints": {
        "pre_scan_dirs": ["doc", "examples", "app"],
        "test_dirs": ["test", "ut"],
        "header_priority_dirs": ["include"],
        "vtable_module_keys": [],
        "domain_rules": [],
        "skip_dirs": [],  # Extra directories to skip (in addition to built-in _SKIP_DIRS)
        # RPT-KERNEL-D9: optional subsystem filter — when set, only files
        # whose path (relative to --source) starts with `<subsystem>/` are
        # scanned. Useful for scanning a monorepo but limiting to specific
        # subsystems (e.g., Linux kernel: ['fs', 'mm', 'block', 'kernel', 'lib']).
        # Empty list = scan everything (default).
        "scan_subsystems": [],
    },
    "dispatch_tuning": {
        # Max vtable dispatch targets per call site. Default 50; raise for kernel
        # file_operations where some fields have >50 registrations.
        "max_vtable_dispatch_per_call": 50,
        # Per-field overrides: {"write_iter": 100, "read_iter": 100}
        "max_vtable_dispatch_per_field": {},
        # Regex patterns matching inline wrapper function names. The first
        # capture group is the underlying field name. Default covers call_*,
        # invoke_*; kernel projects should add __foo, do_foo, foo_inline, etc.
        "inline_wrapper_patterns": [r"^(?:__)?(?:call|invoke)_(\\w+)$"],
        # Macro bridge patterns: list of {"pattern": "X", "impl": "Y"} where
        # X is the macro-name regex (with one capture group) and Y is the
        # implementation-name template (use {1} for the captured group).
        # Default bridges X → __X. Kernel should add X → _X, X → do_X, X → __X_inner.
        "macro_bridge_patterns": [
            {"pattern": "^(\\w+)$", "impl": "__{1}"}
        ],
        # Whether to require same-domain for macro bridge (default True).
        # Set False for kernel where headers and impls sometimes live in
        # different subdirectories.
        "macro_bridge_require_same_domain": True,
        # Tighten fn_ptr_call name heuristic: when True, only treat callee as
        # fn_ptr_call if there's evidence of `&func` address-of or struct
        # assignment in the same function. When False, use legacy name-suffix
        # heuristic (callback/handler/fn/notify prefixes).
        "fn_ptr_call_require_evidence": False,
    },
    "project_boundaries": {
        # Source-path substrings that mark non-API code (tools/, tests/, docs/).
        # Functions whose source_file contains any of these are NOT labeled
        # API_entry even if they appear in export_symbols / vtable_registrations.
        # EMPTY by default — auto-profile detects project-specific paths
        # (e.g., kernel's tools/, scripts/, selftests/).
        "non_api_paths": [],
        # Source-path substrings that mark test code (used by _is_test_source).
        # These are generic cross-project conventions; rarely need overriding.
        "test_path_patterns": ["/test/", "/tests/", "/unit/", "/ut/",
                               "/unittest/", "/fuzz/", "\\test\\",
                               "\\tests\\", "\\unit\\"],
        # Test file suffixes used by _is_test_source — generic conventions.
        "test_file_suffixes": ["_test.c", "_test.cpp", "_ut.c", "_ut.cpp",
                               "_unittest.c", "_unittest.cpp",
                               "test_.c", "test_.cpp"],
        # Domain segments that mark test/unit domains (used by _is_test_domain).
        # Generic conventions; override for project-specific test domains.
        "test_domain_segments": ["ut", "ut_mock", "unit", "test", "fuzz"],
        # Domain prefixes that mark vendor/external code (used by _is_external_domain).
        # EMPTY by default — auto-profile detects project-specific vendor prefixes
        # (e.g., ["huawei", "vendor_x"]). The old hardcoded "huawei.*" pattern
        # is removed; projects that need it must declare it here.
        "vendor_domain_prefixes": [],
        # Common external directory names (vendor/, third_party/, etc.) — these
        # are generic cross-project conventions; rarely need overriding.
        "external_dir_prefixes": ["vendor", "third_party", "thirdparty",
                                  "external", "3rdparty", "contrib"],
    },
    "concurrency_patterns": {
        # Lock acquire/release regex patterns (compiled by builder).
        # Each entry is a raw regex string with one capture group for the lock
        # variable (or no group for locks like rcu_read_lock).
        # EMPTY by default — auto-profile detects project-specific lock APIs
        # by scanning source code for common lock function names.
        # Built-in reference profiles (linux_kernel.json, spdk.json) populate
        # these with project-appropriate patterns.
        "lock_acquire_patterns": [],
        "lock_release_patterns": [],
    },
    "io_classification": {
        # Keywords for classifying functions as I/O-side (storage/network/IO
        # backends) vs I/O-main (front-end handlers). Used by io-path command.
        # EMPTY by default — auto-profile detects project-specific terminology
        # by analyzing function names in I/O-related source directories.
        # Built-in reference profiles populate these for their domain.
        "io_main_keywords": [],
        "io_side_keywords": [],
    },
    "syscall_maps": {},  # arch → {nr: name}, e.g. {"x86_64": {0: "sys_read", ...}}
    "asm_entry_macros": [],  # ASM macros that generate entry points, e.g. [{"name": "idtentry", "func_start_param": 1, "call_params": [2]}]
    "phases": {
        "prescan_completed": False,
        "test_scan_completed": False,
        "llm_header_analysis_completed": False,
        "llm_result_check_completed": False,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base. Lists are replaced, not appended.

    - dict keys: recurse
    - list/other types: override wins
    - keys only in override: added
    - keys only in base: kept
    """
    result = dict(base)
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


# ---------------------------------------------------------------------------
# Profile migration: forward-compatible schema upgrades
# ---------------------------------------------------------------------------
# Each entry: (from_version, to_version, migrate_fn)
# migrate_fn takes the raw dict and returns an upgraded copy.
# Add new entries here when bumping _SUPPORTED_VERSION.

def _migrate_v0_to_v1(d: dict) -> dict:
    """Upgrade a pre-version profile (no 'version' field) to version 1.

    Pre-version profiles lacked the 'dispatch_tuning' section and several
    sub-keys. We don't try to infer them — we just stamp version=1 and let
    the merge with _DEFAULT_PROFILE fill in the missing sections.
    """
    result = dict(d)
    result["version"] = 1
    return result


_MIGRATIONS = [
    (0, 1, _migrate_v0_to_v1),
]


def _migrate_profile(d: dict) -> dict:
    """Apply migrations to bring a profile up to _SUPPORTED_VERSION.

    Walks _MIGRATIONS from the profile's current version (0 if missing)
    up to _SUPPORTED_VERSION. Returns the migrated dict (original unchanged
    if no migrations apply).
    """
    current = d.get("version", 0)
    if current == _SUPPORTED_VERSION:
        return d
    result = dict(d)
    # Build a lookup of from_version → migrate_fn for fast iteration
    by_from = {frm: (to, fn) for frm, to, fn in _MIGRATIONS}
    # Apply migrations in sequence
    for _ in range(len(_MIGRATIONS) + 1):
        if result.get("version", 0) == _SUPPORTED_VERSION:
            break
        frm = result.get("version", 0)
        if frm in by_from:
            _, fn = by_from[frm]
            result = fn(result)
        else:
            break  # No migration from this version — caller will raise
    return result



class ProfileSchema:
    """Represents a merged, validated project profile.

    Usage:
        profile = ProfileSchema.load("path/to/spdk.json")
        # or
        profile = ProfileSchema.from_dict({"project": {"name": "myproj"}, ...})

    The profile is always merged with _default.json (universal C defaults),
    so project profiles only need to declare what differs from defaults.
    """

    def __init__(self, data: dict):
        self._raw = data
        self._validate()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, path: str) -> "ProfileSchema":
        """Load a profile from a JSON file, merged with _default.json.

        Resolution order:
          1. Load _default.json from config/profiles/ (universal C defaults)
          2. Load the specified project profile
          3. Deep-merge: project profile overrides defaults
        """
        # Load defaults
        default_path = _PROFILE_DIR / "_default.json"
        if default_path.exists():
            with open(default_path, "r", encoding="utf-8") as f:
                defaults = json.load(f)
        else:
            defaults = dict(_DEFAULT_PROFILE)

        # Load project profile
        proj_path = Path(path)
        if not proj_path.exists():
            raise FileNotFoundError(f"Profile not found: {path}")
        with open(proj_path, "r", encoding="utf-8") as f:
            project = json.load(f)

        # Merge
        merged = _deep_merge(defaults, project)
        return cls(merged)

    @classmethod
    def from_dict(cls, data: dict, merge_defaults: bool = True) -> "ProfileSchema":
        """Create a profile from a dict, optionally merged with defaults."""
        if merge_defaults:
            merged = _deep_merge(dict(_DEFAULT_PROFILE), data)
        else:
            merged = dict(data)
        return cls(merged)

    @classmethod
    def defaults(cls) -> "ProfileSchema":
        """Return a profile with only universal defaults (no project overrides)."""
        return cls(dict(_DEFAULT_PROFILE))

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _validate(self):
        """Check required fields and types. Raise ValueError on problems."""
        d = self._raw

        if d.get("version") != _SUPPORTED_VERSION:
            # Try auto-migration before failing
            migrated = _migrate_profile(d)
            if migrated.get("version") == _SUPPORTED_VERSION:
                self._raw = migrated
                d = migrated
            else:
                raise ValueError(
                    f"Unsupported profile version {d.get('version')}; "
                    f"expected {_SUPPORTED_VERSION} and no migration path exists"
                )

        # Top-level sections must exist (after merge they always do)
        for section in ("skip_names", "api_detection", "callback_detection",
                        "endpoint_classification", "macro_heuristics",
                        "macro_dispatch", "struct_embeddings"):
            if section not in d:
                raise ValueError(f"Missing required section: {section}")

        # skip_names.external_lib_prefixes must be dict of {prefix: {category, visible}}
        elp = d["skip_names"].get("external_lib_prefixes", {})
        for prefix, info in elp.items():
            if not isinstance(info, dict):
                raise ValueError(
                    f"skip_names.external_lib_prefixes['{prefix}'] must be a dict "
                    f"with 'category' and 'visible' keys, got {type(info).__name__}"
                )
            if "category" not in info:
                raise ValueError(
                    f"skip_names.external_lib_prefixes['{prefix}'] missing 'category'"
                )

        # endpoint_classification.endpoint_rules must be list of dicts
        for i, rule in enumerate(d["endpoint_classification"].get("endpoint_rules", [])):
            if not isinstance(rule, dict):
                raise ValueError(f"endpoint_classification.endpoint_rules[{i}] must be a dict")
            if "pattern" not in rule:
                raise ValueError(f"endpoint_classification.endpoint_rules[{i}] missing 'pattern'")
            if "endpoint_type" not in rule:
                raise ValueError(f"endpoint_classification.endpoint_rules[{i}] missing 'endpoint_type'")
            import re
            try:
                re.compile(rule["pattern"])
            except re.error as e:
                raise ValueError(
                    f"endpoint_classification.endpoint_rules[{i}].pattern "
                    f"is not a valid regex: {e}"
                )

        # api_detection.export_macros must be list of strings
        em = d["api_detection"].get("export_macros", [])
        if not isinstance(em, list):
            raise ValueError(
                "api_detection.export_macros must be a list of strings, "
                f"got {type(em).__name__}"
            )
        for i, mname in enumerate(em):
            if not isinstance(mname, str):
                raise ValueError(
                    f"api_detection.export_macros[{i}] must be a string, "
                    f"got {type(mname).__name__}"
                )

        # api_detection.struct_op_types must be list of strings
        sot = d["api_detection"].get("struct_op_types", [])
        if not isinstance(sot, list):
            raise ValueError(
                "api_detection.struct_op_types must be a list of strings, "
                f"got {type(sot).__name__}"
            )
        for i, name in enumerate(sot):
            if not isinstance(name, str):
                raise ValueError(
                    f"api_detection.struct_op_types[{i}] must be a string, "
                    f"got {type(name).__name__}"
                )

        # api_detection.auto_detect must be bool
        ad = d["api_detection"].get("auto_detect", False)
        if not isinstance(ad, bool):
            raise ValueError(
                f"api_detection.auto_detect must be a bool, "
                f"got {type(ad).__name__}"
            )

        # callback_detection.static_patterns must be list of dicts
        for i, pat in enumerate(d["callback_detection"].get("static_patterns", [])):
            if not isinstance(pat, dict):
                raise ValueError(f"callback_detection.static_patterns[{i}] must be a dict")
            for key in ("register_func", "regex", "cb_arg_index", "concurrency_type"):
                if key not in pat:
                    raise ValueError(
                        f"callback_detection.static_patterns[{i}] missing '{key}'"
                    )

        # callback_detection.skip_call_prefixes must be list of strings
        scp = d["callback_detection"].get("skip_call_prefixes", [])
        if not isinstance(scp, list):
            raise ValueError(
                "callback_detection.skip_call_prefixes must be a list of strings, "
                f"got {type(scp).__name__}"
            )

        # callback_detection.skip_callees must be list of strings
        sc = d["callback_detection"].get("skip_callees", [])
        if not isinstance(sc, list):
            raise ValueError(
                "callback_detection.skip_callees must be a list of strings, "
                f"got {type(sc).__name__}"
            )

        # threading_models must be a dict of model_name → list of pattern dicts
        tm = d.get("threading_models", {})
        if not isinstance(tm, dict):
            raise ValueError(
                f"threading_models must be a dict, got {type(tm).__name__}"
            )
        for model_name, patterns in tm.items():
            if not isinstance(patterns, list):
                raise ValueError(
                    f"threading_models['{model_name}'] must be a list of pattern dicts"
                )

        # macro_dispatch.registration_macros must be list of dicts with required keys
        _REG_MACRO_REQUIRED = ("macro_name", "pattern", "struct_arg_index")
        _REG_MACRO_OPTIONAL = ("register_func", "global_list_var", "iterator_func",
                               "dispatch_field", "handler_arg_index",
                               "dispatch_caller", "generates",
                               "_confidence", "_needs_review")
        for i, entry in enumerate(d["macro_dispatch"].get("registration_macros", [])):
            if not isinstance(entry, dict):
                raise ValueError(
                    f"macro_dispatch.registration_macros[{i}] must be a dict"
                )
            for key in _REG_MACRO_REQUIRED:
                if key not in entry:
                    raise ValueError(
                        f"macro_dispatch.registration_macros[{i}] missing '{key}'"
                    )
            # struct_arg_index must be non-negative int
            sai = entry.get("struct_arg_index")
            if not isinstance(sai, int) or sai < 0:
                raise ValueError(
                    f"macro_dispatch.registration_macros[{i}].struct_arg_index "
                    f"must be a non-negative integer, got {sai!r}"
                )
            # Warn on unknown keys (typo guard)
            unknown = set(entry) - set(_REG_MACRO_REQUIRED) - set(_REG_MACRO_OPTIONAL)
            if unknown:
                raise ValueError(
                    f"macro_dispatch.registration_macros[{i}] has unknown keys: {unknown}"
                )

        # macro_dispatch.token_paste_macros must be list of dicts with required keys
        _TP_MACRO_REQUIRED = ("macro_name", "template", "param_names")
        _TP_MACRO_OPTIONAL = ("generates", "_confidence", "_needs_review")
        for i, entry in enumerate(d["macro_dispatch"].get("token_paste_macros", [])):
            if not isinstance(entry, dict):
                raise ValueError(
                    f"macro_dispatch.token_paste_macros[{i}] must be a dict"
                )
            for key in _TP_MACRO_REQUIRED:
                if key not in entry:
                    raise ValueError(
                        f"macro_dispatch.token_paste_macros[{i}] missing '{key}'"
                    )
            # param_names must be a list of strings
            pn = entry.get("param_names")
            if not isinstance(pn, list) or not all(isinstance(p, str) for p in pn):
                raise ValueError(
                    f"macro_dispatch.token_paste_macros[{i}].param_names "
                    f"must be a list of strings"
                )
            unknown = set(entry) - set(_TP_MACRO_REQUIRED) - set(_TP_MACRO_OPTIONAL)
            if unknown:
                raise ValueError(
                    f"macro_dispatch.token_paste_macros[{i}] has unknown keys: {unknown}"
                )

        # macro_dispatch.macro_aliases must be dict of {macro_name: expansion_target}
        ma = d["macro_dispatch"].get("macro_aliases", {})
        if not isinstance(ma, dict):
            raise ValueError(
                "macro_dispatch.macro_aliases must be a dict "
                f"of {{macro_name: expansion_target}}, got {type(ma).__name__}"
            )
        for mname, target in ma.items():
            if not isinstance(target, str):
                raise ValueError(
                    f"macro_dispatch.macro_aliases['{mname}'] must be a string "
                    f"(expansion target), got {type(target).__name__}"
                )

        # struct_embeddings.container_of_macros must be list of dicts
        _CO_MACRO_REQUIRED = ("macro_name",)
        _CO_MACRO_OPTIONAL = ("pattern",)
        for i, entry in enumerate(d["struct_embeddings"].get("container_of_macros", [])):
            if not isinstance(entry, dict):
                raise ValueError(
                    f"struct_embeddings.container_of_macros[{i}] must be a dict"
                )
            for key in _CO_MACRO_REQUIRED:
                if key not in entry:
                    raise ValueError(
                        f"struct_embeddings.container_of_macros[{i}] missing '{key}'"
                    )
            # Validate pattern is a valid regex if provided
            pat = entry.get("pattern", "")
            if pat:
                import re
                try:
                    re.compile(pat)
                except re.error as e:
                    raise ValueError(
                        f"struct_embeddings.container_of_macros[{i}].pattern "
                        f"is not a valid regex: {e}"
                    )
            unknown = set(entry) - set(_CO_MACRO_REQUIRED) - set(_CO_MACRO_OPTIONAL)
            if unknown:
                raise ValueError(
                    f"struct_embeddings.container_of_macros[{i}] has unknown keys: {unknown}"
                )

        # struct_embeddings.manual_entries must be list of dicts
        _MANUAL_ENTRY_REQUIRED = ("outer_type", "member", "inner_type")
        _MANUAL_ENTRY_OPTIONAL = ("domain_hint",)
        for i, entry in enumerate(d["struct_embeddings"].get("manual_entries", [])):
            if not isinstance(entry, dict):
                raise ValueError(
                    f"struct_embeddings.manual_entries[{i}] must be a dict"
                )
            for key in _MANUAL_ENTRY_REQUIRED:
                if key not in entry:
                    raise ValueError(
                        f"struct_embeddings.manual_entries[{i}] missing '{key}'"
                    )
            unknown = set(entry) - set(_MANUAL_ENTRY_REQUIRED) - set(_MANUAL_ENTRY_OPTIONAL)
            if unknown:
                raise ValueError(
                    f"struct_embeddings.manual_entries[{i}] has unknown keys: {unknown}"
                )

        # scan_hints.domain_rules must be list of dicts with pattern and at least
        # one of: domain_suffix, domain_tag, merge_to, label
        for i, rule in enumerate(d["scan_hints"].get("domain_rules", [])):
            if not isinstance(rule, dict):
                raise ValueError(f"scan_hints.domain_rules[{i}] must be a dict")
            if "pattern" not in rule:
                raise ValueError(f"scan_hints.domain_rules[{i}] missing 'pattern'")
            # At least one action key must be present
            _ACTION_KEYS = ("domain_suffix", "domain_tag", "merge_to", "label")
            if not any(k in rule for k in _ACTION_KEYS):
                raise ValueError(
                    f"scan_hints.domain_rules[{i}] must have at least one of: "
                    f"{', '.join(_ACTION_KEYS)}"
                )
            # Validate pattern is a valid regex
            import re
            try:
                re.compile(rule["pattern"])
            except re.error as e:
                raise ValueError(
                    f"scan_hints.domain_rules[{i}].pattern is not a valid regex: {e}"
                )

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    @property
    def raw(self) -> dict:
        """Return the full merged profile dict."""
        return self._raw

    @property
    def project_name(self) -> str:
        return self._raw.get("project", {}).get("name", "")

    @property
    def language(self) -> str:
        return self._raw.get("project", {}).get("language", "c")

    @property
    def detected_frameworks(self) -> list:
        return self._raw.get("project", {}).get("detected_frameworks", [])

    # ------------------------------------------------------------------
    # Scanner-facing config
    # ------------------------------------------------------------------

    def to_scanner_config(self) -> dict:
        """Produce a dict consumed by scan_c_file().

        Returns:
            {
                "skip_names_add": [...],           # extra names to skip
                "visible_external_prefixes": [...], # prefixes to remove from skip (create edges)
                "silent_skip_prefixes": [...],      # prefixes kept in skip (no edges)
                "callback_patterns": [...],         # static callback patterns
                "generic_cb_suffixes": [...],       # suffixes for generic cb detection
                "test_framework_prefixes": [...],   # test framework prefixes
                "macro_condition_prefixes": [...],  # macro #ifdef prefixes
                "api_prefixes": [...],              # public API prefixes
                "macro_dispatch_patterns": [...],   # registration macro patterns
                "token_paste_macros": [...],        # token-paste macro templates
                "container_of_macros": [...],       # container_of macro patterns
            }
        """
        d = self._raw
        sn = d["skip_names"]
        cd = d["callback_detection"]
        ah = d["macro_heuristics"]
        api = d["api_detection"]
        md = d["macro_dispatch"]
        se = d["struct_embeddings"]
        sh = d["scan_hints"]

        # Split external_lib_prefixes into visible (edges created) and silent (skip)
        visible = []
        silent = []
        for prefix, info in sn.get("external_lib_prefixes", {}).items():
            if info.get("visible", False):
                visible.append(prefix)
            else:
                silent.append(prefix)

        # Build callback_patterns from static_patterns
        callback_patterns = []
        for pat in cd.get("static_patterns", []):
            callback_patterns.append({
                "register_func": pat["register_func"],
                "regex": pat["regex"],
                "cb_arg_index": pat["cb_arg_index"],
                "concurrency_type": pat["concurrency_type"],
            })

        return {
            "skip_names_add": sn.get("add", []),
            "visible_external_prefixes": visible,
            "silent_skip_prefixes": silent,
            "callback_patterns": callback_patterns,
            "generic_cb_suffixes": cd.get("generic_cb_suffixes", []),
            "skip_call_prefixes": cd.get("skip_call_prefixes", []),
            "skip_callees": cd.get("skip_callees", []),
            "test_framework_prefixes": sn.get("test_framework_prefixes", []),
            "macro_condition_prefixes": ah.get("macro_condition_prefixes", []),
            "api_prefixes": api.get("public_prefixes", []),
            "export_macros": api.get("export_macros", []),
            "public_header_paths": api.get("public_header_paths", []),
            "non_api_paths": (d.get("project_boundaries", {}) or {}).get("non_api_paths", []),
            "struct_op_types": api.get("struct_op_types", []),
            "macro_dispatch_patterns": md.get("registration_macros", []),
            "token_paste_macros": md.get("token_paste_macros", []),
            "macro_aliases": md.get("macro_aliases", {}),
            "container_of_macros": se.get("container_of_macros", []),
            "domain_rules": sh.get("domain_rules", []),
            "skip_dirs": sh.get("skip_dirs", []),
            "dispatch_tuning": d.get("dispatch_tuning", {}),
        }

    # ------------------------------------------------------------------
    # Builder-facing config
    # ------------------------------------------------------------------

    def to_builder_config(self) -> dict:
        """Produce a dict consumed by builder modules.

        Returns:
            {
                "lib_prefix_map": {prefix: category, ...},
                "macro_condition_prefixes": [...],
                "public_prefixes": [...],
                "vtable_module_keys": [...],
                "macro_dispatch": {...},
                "struct_embeddings": {...},
                "skip_names_add": [...],
            }
        """
        d = self._raw
        ah = d["macro_heuristics"]
        api = d["api_detection"]
        ec = d["endpoint_classification"]
        sh = d["scan_hints"]
        sn = d["skip_names"]

        # Use endpoint_classification.lib_prefix_map directly — it's the
        # authoritative source for builder classification. It must contain
        # ALL prefixes (visible + silent) that map to endpoint categories.
        lib_prefix_map = dict(ec.get("lib_prefix_map", {}))

        return {
            "lib_prefix_map": lib_prefix_map,
            "endpoint_rules": ec.get("endpoint_rules", []),
            "macro_condition_prefixes": ah.get("macro_condition_prefixes", []),
            "public_prefixes": api.get("public_prefixes", []),
            "internal_patterns": api.get("internal_patterns", []),
            "public_header_paths": api.get("public_header_paths", []),
            "vtable_module_keys": sh.get("vtable_module_keys", []),
            "macro_dispatch": d["macro_dispatch"],
            "struct_embeddings": d["struct_embeddings"],
            "skip_names_add": sn.get("add", []),
            "scenario_noise_names": sn.get("scenario_noise_names", []),
            "struct_op_types": api.get("struct_op_types", []),
            "export_macros": api.get("export_macros", []),
            "api_auto_detect": api.get("auto_detect", False),
            "domain_rules": sh.get("domain_rules", []),
            "threading_models": d.get("threading_models", {}),
            "skip_dirs": sh.get("skip_dirs", []),
            "dispatch_tuning": d.get("dispatch_tuning", {}),
            "project_boundaries": d.get("project_boundaries", {}),
            "concurrency_patterns": d.get("concurrency_patterns", {}),
            "io_classification": d.get("io_classification", {}),
        }

    # ------------------------------------------------------------------
    # Effective skip set computation
    # ------------------------------------------------------------------

    def effective_skip_names(self, universal_skip: frozenset) -> frozenset:
        """Compute the effective skip set for the scanner.

        Formula:
            effective = (universal_skip | profile_add)
                        - {names matching visible_external_prefixes}

        This replaces the old project-specific skip name deduction logic.

        Args:
            universal_skip: The _UNIVERSAL_SKIP_NAMES frozenset from the scanner.

        Returns:
            The effective skip set to use for scanning.
        """
        d = self._raw
        sn = d["skip_names"]
        sc = self.to_scanner_config()

        # Start with universal + profile additions
        result = set(universal_skip) | set(sn.get("add", []))

        # Remove names matching visible external prefixes (they should create edges)
        for prefix in sc["visible_external_prefixes"]:
            result = {n for n in result if not n.startswith(prefix)}

        return frozenset(result)

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_json(self, indent: int = 2) -> str:
        """Serialize the merged profile to JSON."""
        return json.dumps(self._raw, indent=indent, ensure_ascii=False)

    def save(self, path: str):
        """Save the merged profile to a JSON file."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(self.to_json())

    # ------------------------------------------------------------------
    # Phase tracking
    # ------------------------------------------------------------------

    def is_phase_completed(self, phase: str) -> bool:
        """Check if a scan phase has been completed."""
        return self._raw.get("phases", {}).get(phase, False)

    def mark_phase_completed(self, phase: str):
        """Mark a scan phase as completed."""
        if "phases" not in self._raw:
            self._raw["phases"] = {}
        self._raw["phases"][phase] = True

    # ------------------------------------------------------------------
    # Representation
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        name = self.project_name or "(unnamed)"
        fw = ", ".join(self.detected_frameworks) or "none"
        return f"ProfileSchema(project={name!r}, frameworks=[{fw}])"
