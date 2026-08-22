#!/usr/bin/env python3
"""Profile health scoring + auto-evolution.

The existing profile system has two gaps:
1. **No health check**: a profile may be missing critical fields
   (callback_patterns, skip_names, vtable_types) and the user has no
   way to know.
2. **No evolution**: as the project grows, new callback patterns emerge
   but aren't auto-added to the profile.

This module provides:

1. **Health scoring**: given a profile + the source tree it covers,
   compute a 0-100 score with breakdown by category. Categories:
   - callback_patterns (25 pts): are common register functions covered?
   - skip_names (15 pts): are build artifacts / test dirs skipped?
   - vtable_types (15 pts): are project's struct_op_types captured?
   - api_prefixes (10 pts): are public API prefixes set?
   - domain_keywords (15 pts): are domain keywords present?
   - macro_definitions (10 pts): are project-defining macros set?
   - profile_version (10 pts): is the profile versioned + bound to code?

2. **Auto-evolution**: after each build, scan for new callback patterns
   in the codebase that aren't in the profile, and propose additions.
   Also detect when existing patterns are no longer used (suggest removal).

3. **Version binding**: the profile should record the source_commit it
   was generated against, so a stale profile can be detected.

CLI:
    profile-health --graph <dir> --source <root>     # compute health
    profile-evolve --graph <dir> --source <root>     # suggest additions
    profile-bind-version --graph <dir> --source <root>  # bind to commit
"""

import json
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional, List, Dict, Any, Set, Tuple


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@dataclass
class HealthCategory:
    """One category of the health score."""
    name: str
    max_points: int
    points: int
    findings: List[str] = field(default_factory=list)
    suggestions: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class ProfileHealth:
    """Full health report for a profile."""
    profile_path: str
    total_score: int  # 0-100
    categories: List[HealthCategory]
    overall_status: str  # "excellent", "good", "fair", "poor"
    profile_version: str = ""
    source_commit: str = ""

    def to_dict(self) -> Dict:
        return {
            "profile_path": self.profile_path,
            "total_score": self.total_score,
            "categories": [c.to_dict() for c in self.categories],
            "overall_status": self.overall_status,
            "profile_version": self.profile_version,
            "source_commit": self.source_commit,
        }


def compute_profile_health(profile: Dict, source_root: str,
                            profile_path: str = "") -> ProfileHealth:
    """Compute a 0-100 health score for a profile.

    The score is broken into 7 categories (see module docstring).
    Each category's points are awarded based on whether the profile
    has the relevant fields populated AND whether those fields match
    what's actually in the source code.
    """
    categories: List[HealthCategory] = []

    # 1. callback_patterns (25 pts)
    cb_patterns = profile.get("callback_patterns", []) or []
    cb_findings, cb_suggestions = _check_callback_patterns(cb_patterns, source_root)
    cb_pts = min(25, len(cb_patterns) * 5)
    if cb_patterns and not cb_findings:
        cb_pts = 25
    categories.append(HealthCategory(
        name="callback_patterns", max_points=25, points=cb_pts,
        findings=cb_findings, suggestions=cb_suggestions))

    # 2. skip_names (15 pts)
    skip_names = profile.get("skip_names", []) or []
    skip_findings, skip_suggestions = _check_skip_names(skip_names)
    skip_pts = 15 if len(skip_names) >= 3 else len(skip_names) * 5
    categories.append(HealthCategory(
        name="skip_names", max_points=15, points=skip_pts,
        findings=skip_findings, suggestions=skip_suggestions))

    # 3. vtable_types / struct_op_types (15 pts)
    vtables = (profile.get("vtable_types", []) or
               profile.get("struct_op_types", []) or [])
    vt_findings, vt_suggestions = _check_vtable_types(vtables, source_root)
    vt_pts = min(15, len(vtables) * 3)
    if vtables and not vt_findings:
        vt_pts = 15
    categories.append(HealthCategory(
        name="vtable_types", max_points=15, points=vt_pts,
        findings=vt_findings, suggestions=vt_suggestions))

    # 4. api_prefixes (10 pts)
    api_prefixes = profile.get("api_prefixes", []) or []
    api_pts = min(10, len(api_prefixes) * 5)
    categories.append(HealthCategory(
        name="api_prefixes", max_points=10, points=api_pts,
        findings=[] if api_prefixes else ["no api_prefixes set"],
        suggestions=[] if api_prefixes else ["add api_prefixes for your public API"]))

    # 5. domain_keywords (15 pts)
    domain_kw = profile.get("domain_keywords", {}) or {}
    dk_pts = min(15, len(domain_kw) * 3)
    categories.append(HealthCategory(
        name="domain_keywords", max_points=15, points=dk_pts,
        findings=[] if domain_kw else ["no domain_keywords set"],
        suggestions=[] if domain_kw else ["add domain_keywords per domain"]))

    # 6. macro_definitions (10 pts)
    macros = profile.get("defined_macros", {}) or profile.get("macros", {}) or {}
    mac_pts = 10 if macros else 0
    categories.append(HealthCategory(
        name="macro_definitions", max_points=10, points=mac_pts,
        findings=[] if macros else ["no defined_macros set"],
        suggestions=[] if macros else ["add project-defining macros (e.g., -DCONFIG_X=1)"]))

    # 7. profile_version + source_commit binding (10 pts)
    pv = profile.get("profile_version", "")
    sc = profile.get("source_commit", "")
    ver_pts = 0
    ver_findings = []
    if pv:
        ver_pts += 5
    else:
        ver_findings.append("profile_version not set")
    if sc:
        ver_pts += 5
    else:
        ver_findings.append("source_commit not bound (profile may be stale)")
    categories.append(HealthCategory(
        name="profile_version", max_points=10, points=ver_pts,
        findings=ver_findings,
        suggestions=["add profile_version and source_commit"] if ver_pts < 10 else []))

    total = sum(c.points for c in categories)
    if total >= 85:
        status = "excellent"
    elif total >= 65:
        status = "good"
    elif total >= 40:
        status = "fair"
    else:
        status = "poor"

    return ProfileHealth(
        profile_path=profile_path, total_score=total,
        categories=categories, overall_status=status,
        profile_version=str(pv), source_commit=str(sc),
    )


def _check_callback_patterns(patterns: List[Dict],
                              source_root: str) -> Tuple[List[str], List[str]]:
    """Check that callback_patterns match actual code in source_root."""
    findings = []
    suggestions = []
    if not patterns:
        findings.append("no callback_patterns configured")
        suggestions.append("run auto-profile to detect register_* patterns")
        return findings, suggestions
    # Check whether each pattern's register_func appears in the source
    if not os.path.isdir(source_root):
        return findings, suggestions
    found_in_code: Set[str] = set()
    for dirpath, dirnames, filenames in os.walk(source_root):
        dirnames[:] = [d for d in dirnames if not d.startswith('.')
                       and d not in ("__pycache__", "node_modules", "build", ".git")]
        for fname in filenames:
            ext = Path(fname).suffix.lower()
            if ext not in (".c", ".h", ".cpp", ".go", ".py", ".java", ".rs"):
                continue
            try:
                text = Path(os.path.join(dirpath, fname)).read_text(
                    encoding="utf-8", errors="replace")
            except OSError:
                continue
            for pat in patterns:
                rf = pat.get("register_func", "")
                if rf and rf in text:
                    found_in_code.add(rf)
    for pat in patterns:
        rf = pat.get("register_func", "")
        if rf and rf not in found_in_code:
            findings.append(f"register_func '{rf}' not found in source")
            suggestions.append(f"remove or update pattern for '{rf}'")
    return findings, suggestions


def _check_skip_names(skip_names: List[str]) -> Tuple[List[str], List[str]]:
    """Check that skip_names covers common build/test artifacts."""
    expected = {"test", "tests", "build", "vendor", "third_party",
                "node_modules", "__pycache__", ".git"}
    missing = expected - set(skip_names)
    findings = []
    suggestions = []
    if missing:
        findings.append(f"missing common skip_names: {sorted(missing)}")
        suggestions.append(f"add these to skip_names: {sorted(missing)}")
    return findings, suggestions


def _check_vtable_types(vtables: List[str],
                         source_root: str) -> Tuple[List[str], List[str]]:
    """Check whether vtable_types match actual struct names in source."""
    findings = []
    suggestions = []
    if not vtables:
        findings.append("no vtable_types / struct_op_types configured")
        suggestions.append("look for struct *_ops, *_operations, *_callbacks")
        return findings, suggestions
    if not os.path.isdir(source_root):
        return findings, suggestions
    # Look for at least one vtable type in the source
    text_sample = ""
    for dirpath, dirnames, filenames in os.walk(source_root):
        dirnames[:] = [d for d in dirnames if not d.startswith('.')
                       and d not in ("__pycache__", "node_modules", "build", ".git")]
        for fname in filenames:
            if Path(fname).suffix.lower() in (".c", ".h"):
                try:
                    text_sample += Path(os.path.join(dirpath, fname)).read_text(
                        encoding="utf-8", errors="replace")
                except OSError:
                    pass
        if len(text_sample) > 1_000_000:  # cap at 1MB
            break
    for vt in vtables:
        if vt not in text_sample:
            findings.append(f"vtable_type '{vt}' not found in source")
            suggestions.append(f"remove or update vtable_type '{vt}'")
    return findings, suggestions


# ---------------------------------------------------------------------------
# Auto-evolution: detect new callback patterns not in profile
# ---------------------------------------------------------------------------

# Common callback registration patterns
_CB_REGISTER_RES = [
    re.compile(r'(\w+_register_\w+)\s*\('),
    re.compile(r'register_(\w+_callback)\s*\('),
    re.compile(r'(\w+_register_callback)\s*\('),
    re.compile(r'(\w+_cb_register)\s*\('),
    re.compile(r'\b(\w+_init_callbacks)\s*\('),
]


@dataclass
class EvolutionSuggestion:
    """One suggested change to the profile."""
    kind: str  # 'add_pattern', 'remove_pattern', 'add_skip_name', 'bind_version'
    description: str
    payload: Dict = field(default_factory=dict)
    confidence: str = "INFERRED"  # EXTRACTED (high) / INFERRED (medium) / AMBIGUOUS


def detect_evolution_suggestions(profile: Dict, source_root: str) -> List[EvolutionSuggestion]:
    """Detect new callback patterns in the source not covered by the profile.

    Returns a list of suggestions (additions / removals / version binding).
    """
    suggestions: List[EvolutionSuggestion] = []

    # 1. Detect new callback register functions in the source
    existing_register_funcs = {p.get("register_func", "")
                                for p in profile.get("callback_patterns", []) or []
                                if p.get("register_func")}
    new_register_funcs: Dict[str, int] = defaultdict(int)  # name → occurrence count
    if os.path.isdir(source_root):
        for dirpath, dirnames, filenames in os.walk(source_root):
            dirnames[:] = [d for d in dirnames if not d.startswith('.')
                           and d not in ("__pycache__", "node_modules", "build", ".git")]
            for fname in filenames:
                ext = Path(fname).suffix.lower()
                if ext not in (".c", ".h", ".cpp", ".go", ".py", ".java", ".rs"):
                    continue
                try:
                    text = Path(os.path.join(dirpath, fname)).read_text(
                        encoding="utf-8", errors="replace")
                except OSError:
                    continue
                for pat in _CB_REGISTER_RES:
                    for m in pat.finditer(text):
                        rf = m.group(1)
                        if rf and rf not in existing_register_funcs:
                            new_register_funcs[rf] += 1
    # Suggest the most-common new patterns
    for rf, count in sorted(new_register_funcs.items(),
                            key=lambda x: -x[1])[:10]:
        if count >= 2:  # at least 2 occurrences to be confident
            suggestions.append(EvolutionSuggestion(
                kind="add_pattern",
                description=f"new callback register function '{rf}' found "
                            f"({count} occurrences) — not in profile",
                payload={"register_func": rf, "occurrences": count},
                confidence="EXTRACTED" if count >= 5 else "INFERRED",
            ))

    # 2. Detect existing patterns that are no longer used
    for pat in profile.get("callback_patterns", []) or []:
        rf = pat.get("register_func", "")
        if rf and rf not in new_register_funcs and rf not in existing_register_funcs:
            # We checked existence in _check_callback_patterns; if not found
            # there either, suggest removal.
            suggestions.append(EvolutionSuggestion(
                kind="remove_pattern",
                description=f"register_func '{rf}' not found in source — "
                            f"consider removing from profile",
                payload={"register_func": rf},
                confidence="INFERRED",
            ))

    # 3. Suggest version binding
    if not profile.get("profile_version"):
        suggestions.append(EvolutionSuggestion(
            kind="bind_version",
            description="profile has no profile_version — add one to track evolution",
            payload={},
            confidence="EXTRACTED",
        ))
    if not profile.get("source_commit"):
        suggestions.append(EvolutionSuggestion(
            kind="bind_version",
            description="profile has no source_commit — bind to current git HEAD",
            payload={},
            confidence="EXTRACTED",
        ))

    return suggestions


def apply_evolution_suggestions(profile: Dict,
                                 suggestions: List[EvolutionSuggestion]) -> Dict:
    """Apply EXTRACTED-confidence suggestions to a profile (returns new dict).

    INFERRED suggestions are NOT auto-applied — they require user review.
    """
    new_profile = dict(profile)
    cb_patterns = list(new_profile.get("callback_patterns", []) or [])
    for s in suggestions:
        if s.confidence != "EXTRACTED":
            continue
        if s.kind == "add_pattern":
            rf = s.payload.get("register_func", "")
            if rf and not any(p.get("register_func") == rf for p in cb_patterns):
                cb_patterns.append({
                    "register_func": rf,
                    "callback_field": "callback",
                    "evolved_from": "auto_evolution",
                })
        elif s.kind == "remove_pattern":
            rf = s.payload.get("register_func", "")
            cb_patterns = [p for p in cb_patterns if p.get("register_func") != rf]
        elif s.kind == "bind_version":
            if not new_profile.get("profile_version"):
                new_profile["profile_version"] = "auto-evolved-1"
            if not new_profile.get("source_commit"):
                # Try to detect git HEAD
                try:
                    import subprocess
                    result = subprocess.run(
                        ["git", "-C", new_profile.get("_source_root", "."), "rev-parse", "HEAD"],
                        capture_output=True, text=True, timeout=5
                    )
                    if result.returncode == 0:
                        new_profile["source_commit"] = result.stdout.strip()
                except Exception:
                    pass
    new_profile["callback_patterns"] = cb_patterns
    new_profile["_last_evolved_at"] = str(int(__import__("time").time()))
    return new_profile


# ---------------------------------------------------------------------------
# Version binding
# ---------------------------------------------------------------------------

def bind_profile_to_commit(profile: Dict, source_root: str) -> Dict:
    """Bind a profile to the current git/svn commit of source_root."""
    new_profile = dict(profile)
    try:
        import subprocess
        result = subprocess.run(
            ["git", "-C", source_root, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            new_profile["source_commit"] = result.stdout.strip()
            # Also get short form
            result2 = subprocess.run(
                ["git", "-C", source_root, "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True, timeout=5
            )
            if result2.returncode == 0:
                new_profile["source_commit_short"] = result2.stdout.strip()
            return new_profile
    except Exception:
        pass
    # Try svn
    try:
        import subprocess
        result = subprocess.run(
            ["svn", "info", source_root],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            for line in result.stdout.split("\n"):
                if line.startswith("Revision:"):
                    new_profile["source_commit"] = f"r{line.split(':')[1].strip()}"
                    break
    except Exception:
        pass
    return new_profile


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_profile_health(args):
    """Compute profile health score.

    Usage: profile-health --graph <dir> --source <root> [--profile <path>]
    """
    graph_dir = args.graph
    source_root = args.source
    profile_path = getattr(args, "profile", "") or os.path.join(
        graph_dir, ".code2database_profile.json")
    if not os.path.exists(profile_path):
        # Try source root
        profile_path = os.path.join(source_root, ".code2database_profile.json")
    if not os.path.exists(profile_path):
        print(f"Profile not found", file=sys.stderr)
        sys.exit(1)

    profile = json.loads(Path(profile_path).read_text(encoding="utf-8"))
    health = compute_profile_health(profile, source_root, profile_path)
    print(json.dumps(health.to_dict(), ensure_ascii=False, indent=2, default=str))


def cmd_profile_evolve(args):
    """Suggest profile evolution.

    Usage: profile-evolve --graph <dir> --source <root> [--apply]
    """
    graph_dir = args.graph
    source_root = args.source
    profile_path = getattr(args, "profile", "") or os.path.join(
        graph_dir, ".code2database_profile.json")
    if not os.path.exists(profile_path):
        profile_path = os.path.join(source_root, ".code2database_profile.json")
    if not os.path.exists(profile_path):
        print(f"Profile not found", file=sys.stderr)
        sys.exit(1)

    profile = json.loads(Path(profile_path).read_text(encoding="utf-8"))
    suggestions = detect_evolution_suggestions(profile, source_root)

    if getattr(args, "apply", False):
        new_profile = apply_evolution_suggestions(profile, suggestions)
        Path(profile_path).write_text(
            json.dumps(new_profile, ensure_ascii=False, indent=2),
            encoding="utf-8")
        print(f"Applied {sum(1 for s in suggestions if s.confidence == 'EXTRACTED')} "
              f"suggestions to {profile_path}", file=sys.stderr)

    print(json.dumps({
        "suggestion_count": len(suggestions),
        "suggestions": [asdict(s) for s in suggestions],
    }, ensure_ascii=False, indent=2, default=str))


def cmd_profile_bind_version(args):
    """Bind profile to current source commit.

    Usage: profile-bind-version --graph <dir> --source <root>
    """
    graph_dir = args.graph
    source_root = args.source
    profile_path = getattr(args, "profile", "") or os.path.join(
        graph_dir, ".code2database_profile.json")
    if not os.path.exists(profile_path):
        profile_path = os.path.join(source_root, ".code2database_profile.json")
    if not os.path.exists(profile_path):
        print(f"Profile not found", file=sys.stderr)
        sys.exit(1)

    profile = json.loads(Path(profile_path).read_text(encoding="utf-8"))
    new_profile = bind_profile_to_commit(profile, source_root)
    Path(profile_path).write_text(
        json.dumps(new_profile, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(json.dumps({
        "profile_path": profile_path,
        "source_commit": new_profile.get("source_commit", ""),
        "source_commit_short": new_profile.get("source_commit_short", ""),
        "profile_version": new_profile.get("profile_version", ""),
    }, ensure_ascii=False, indent=2))
