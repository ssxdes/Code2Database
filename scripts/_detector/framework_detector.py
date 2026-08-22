#!/usr/bin/env python3
"""Framework detection from file paths and source content.

Identifies known frameworks (Django, Flask, FastAPI, Spring,
Gin, Echo, Actix, Rocket, Tokio, Qt, etc.) from file path patterns
and directory structure.

Project-specific frameworks are detected via generic heuristics
(directory structure analysis) and profile-driven discovery, not
by hardcoded project name patterns.

Adapted for Code2Database's multi-language support.
"""

import os
import re
from dataclasses import dataclass


@dataclass
class FrameworkHint:
    """Detected framework information."""
    name: str          # e.g., "spdk", "django", "spring"
    category: str      # e.g., "web", "storage", "gui", "async"
    confidence: float  # 0.0–1.0
    entry_multiplier: float  # Multiplier for entry-point scoring


# Framework path patterns: (regex_pattern, FrameworkHint)
# Note: project-specific frameworks (SPDK, DPDK, etc.) are NOT hardcoded here.
# They should be detected via profile-driven discovery or generic heuristics.
_FRAMEWORK_PATTERNS = [
    # C/C++ systems libraries (universal)
    (r'/lib/event', FrameworkHint("libevent", "eventloop", 0.7, 1.2)),
    (r'/lib/ev', FrameworkHint("libev", "eventloop", 0.6, 1.2)),
    (r'/lib/uv', FrameworkHint("libuv", "eventloop", 0.7, 1.2)),
    (r'/qt/', FrameworkHint("qt", "gui", 0.6, 1.3)),
    (r'/QtCore/', FrameworkHint("qt", "gui", 0.7, 1.3)),
    (r'/gtk/', FrameworkHint("gtk", "gui", 0.6, 1.2)),

    # Python web frameworks
    (r'/django/', FrameworkHint("django", "web", 0.8, 1.5)),
    (r'/flask/', FrameworkHint("flask", "web", 0.7, 1.4)),
    (r'/fastapi/', FrameworkHint("fastapi", "web", 0.8, 1.5)),
    (r'/celery/', FrameworkHint("celery", "taskqueue", 0.7, 1.2)),
    (r'/tornado/', FrameworkHint("tornado", "web", 0.6, 1.3)),
    (r'/aiohttp/', FrameworkHint("aiohttp", "web", 0.6, 1.3)),
    (r'/bottle\.py', FrameworkHint("bottle", "web", 0.8, 1.3)),
    (r'/scrapy/', FrameworkHint("scrapy", "scraping", 0.7, 1.2)),

    # Java frameworks
    (r'/spring/', FrameworkHint("spring", "web", 0.7, 1.5)),
    (r'/springboot/', FrameworkHint("spring-boot", "web", 0.8, 1.5)),
    (r'/jaxrs/', FrameworkHint("jax-rs", "web", 0.7, 1.4)),
    (r'/jersey/', FrameworkHint("jersey", "web", 0.7, 1.4)),
    (r'/android/', FrameworkHint("android", "mobile", 0.8, 1.3)),

    # Go frameworks
    (r'/gin-gonic/', FrameworkHint("gin", "web", 0.8, 1.5)),
    (r'/echo/', FrameworkHint("echo", "web", 0.6, 1.4)),
    (r'/fiber/', FrameworkHint("fiber", "web", 0.6, 1.4)),
    (r'/kit/', FrameworkHint("go-kit", "microservice", 0.5, 1.3)),

    # Rust frameworks
    (r'/actix/', FrameworkHint("actix", "web", 0.8, 1.5)),
    (r'/rocket/', FrameworkHint("rocket", "web", 0.7, 1.5)),
    (r'/tokio/', FrameworkHint("tokio", "async", 0.8, 1.3)),
    (r'/warp/', FrameworkHint("warp", "web", 0.7, 1.4)),
    (r'/axum/', FrameworkHint("axum", "web", 0.7, 1.5)),

    # Node.js / TS frameworks
    (r'/express/', FrameworkHint("express", "web", 0.7, 1.5)),
    (r'/next/', FrameworkHint("nextjs", "web", 0.8, 1.5)),
    (r'/nuxt/', FrameworkHint("nuxt", "web", 0.7, 1.4)),
    (r'/nest/', FrameworkHint("nestjs", "web", 0.6, 1.4)),
    (r'/koa/', FrameworkHint("koa", "web", 0.6, 1.4)),
    (r'/fastify/', FrameworkHint("fastify", "web", 0.7, 1.5)),
]

# Entry point name patterns per framework (function names that are likely entry points)
# Note: project-specific entry patterns should come from profile JSON, not hardcoded.
_ENTRY_PATTERNS = {
    "django": [r'^views\.', r'^urls\.', r'^admin\.', r'^models\.'],
    "flask": [r'^route_', r'^@app\.route'],
    "fastapi": [r'^@app\.', r'^@router\.'],
    "spring": [r'^@RequestMapping', r'^@GetMapping', r'^@PostMapping'],
    "gin": [r'^HandleFunc', r'^GET\(', r'^POST\('],
    "actix": [r'^handle\b', r'^HttpServer'],
    "rocket": [r'^@get\b', r'^@post\b', r'^launch\b'],
    "tokio": [r'^main\b.*async', r'^tokio::spawn'],
    "express": [r'^app\.(get|post|put|delete)', r'^router\.'],
    "nextjs": [r'^getServerSideProps', r'^getStaticProps', r'^handler\b'],
}


def detect_framework(filepath: str, language: str = "") -> FrameworkHint | None:
    """Detect framework from a file path.

    Args:
        filepath: Absolute or relative file path
        language: Language hint (c, cpp, python, java, go, rust)

    Returns:
        FrameworkHint if a framework is detected, None otherwise.
    """
    # Normalize path separators and ensure leading slash for pattern matching
    norm_path = '/' + filepath.replace('\\', '/').lower()

    best_hint = None
    best_confidence = 0.0

    for pattern, hint in _FRAMEWORK_PATTERNS:
        if re.search(pattern, norm_path):
            if hint.confidence > best_confidence:
                best_hint = hint
                best_confidence = hint.confidence

    return best_hint


def detect_frameworks_for_project(source_root: str) -> list[FrameworkHint]:
    """Scan source root for framework indicators.

    Returns list of unique detected frameworks. Walks the source tree
    but exits early once all known framework patterns have been found.
    """
    seen = set()
    results = []
    target_count = len(_FRAMEWORK_PATTERNS) if hasattr(_FRAMEWORK_PATTERNS, '__len__') else 30

    for dirpath, dirnames, filenames in os.walk(source_root):
        dirnames[:] = [d for d in dirnames
                       if not d.startswith('.') and d not in
                       ('__pycache__', 'node_modules', 'build', '_build')
                       and not d.startswith('cmake-build-')]
        for fname in filenames:
            fpath = os.path.join(dirpath, fname)
            hint = detect_framework(fpath)
            if hint and hint.name not in seen:
                seen.add(hint.name)
                results.append(hint)
                if len(seen) >= target_count:
                    return results
    return results


def get_entry_multiplier(func_name: str, frameworks: list[FrameworkHint]) -> float:
    """Get entry-point scoring multiplier based on function name and detected frameworks.

    Args:
        func_name: Function name to check
        frameworks: List of detected frameworks for the project

    Returns:
        Multiplier value (1.0 = no boost, higher = more likely entry point)
    """
    max_mult = 1.0
    for fw in frameworks:
        patterns = _ENTRY_PATTERNS.get(fw.name, [])
        for pat in patterns:
            if re.search(pat, func_name):
                max_mult = max(max_mult, fw.entry_multiplier)
    return max_mult


# ---------------------------------------------------------------------------
# CLI test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: framework_detector.py <source_root>")
        sys.exit(1)

    frameworks = detect_frameworks_for_project(sys.argv[1])
    if frameworks:
        print(f"Detected frameworks:")
        for fw in frameworks:
            print(f"  {fw.name} ({fw.category}, confidence={fw.confidence}, "
                  f"entry_mult={fw.entry_multiplier})")
    else:
        print("No frameworks detected")
