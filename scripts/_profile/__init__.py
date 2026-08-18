"""Project profile system for Code2Database.

Externalizes per-project knowledge (skip names, callback patterns, endpoint
classification, etc.) that was previously hardcoded in the scanner/builder.

Usage:
    from _profile import ProfileSchema
    profile = ProfileSchema.load("scripts/config/profiles/spdk.json")
    scanner_config = profile.to_scanner_config()
    builder_config = profile.to_builder_config()
"""

from _profile.schema import ProfileSchema

__all__ = ["ProfileSchema"]
