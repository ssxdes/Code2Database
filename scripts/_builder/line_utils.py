"""Line-number lookup helpers for source text.

Standalone module (no networkx / no third-party deps) so it can be
imported from `_builder.invariants`, `_builder.ffi_bridge`, scanners,
and other places that need O(log N) line lookups without pulling in
the heavy `_builder.utils` module graph.
"""
from __future__ import annotations

import bisect
from typing import List


def build_line_starts(text: str) -> List[int]:
    """Pre-compute the byte offset of the start of each line in `text`.

    Pair with `line_for_offset(line_starts, offset)` for O(log N) line
    lookups; this replaces the O(N) per-match `text.count("\\n", 0, m.start())`
    pattern that becomes O(N*M) when applied to M regex matches in a
    body of length N.

    Args:
      text: Source text (str).

    Returns:
      Sorted list of byte offsets where each line begins.
      ``line_starts[0] == 0`` (start of first line).
      ``line_starts[i] == offset`` of line ``i+1``.
      Length = (number of newlines in text) + 1.
    """
    starts = [0]
    idx = text.find("\n")
    while idx >= 0:
        starts.append(idx + 1)
        idx = text.find("\n", idx + 1)
    return starts


def line_for_offset(line_starts: List[int], offset: int) -> int:
    """Return the 1-based line number for the given byte offset.

    Args:
      line_starts: Pre-computed result of ``build_line_starts(text)``.
      offset: Byte offset into the source text.

    Returns:
      1-based line number (1 = first line).
    """
    if not line_starts:
        return 1
    return bisect.bisect_right(line_starts, offset)


def lines_for_matches(text: str, matches) -> List[int]:
    """Compute 1-based line numbers for a list of regex matches.

    Convenience wrapper that pre-computes ``line_starts`` once and then
    uses ``line_for_offset`` for each match.

    Args:
      text: The source text the matches were found in.
      matches: Iterable of ``re.Match`` objects (or any object with ``.start()``).

    Returns:
      List of 1-based line numbers, one per match.
    """
    line_starts = build_line_starts(text)
    return [line_for_offset(line_starts, m.start()) for m in matches]
