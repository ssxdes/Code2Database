"""Detector implementation package.

Re-exports for backward compatibility.
"""
from _detector.build_detector import BuildDetector, evaluate_pp_condition, BuildInfo
from _detector.community_detector import detect_communities, CommunityResult
from _detector.framework_detector import (
    detect_framework, detect_frameworks_for_project,
    get_entry_multiplier, FrameworkHint,
)
