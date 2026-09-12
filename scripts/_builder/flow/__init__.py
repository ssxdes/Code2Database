"""Goal-oriented lifecycle layer for the Code2Database CLI.

This package hosts the `c2d` umbrella command: a small set of lifecycle
verbs (ask/recipes/verbs here; setup/session/capture and freshen/report
arrive with their own modules) that wrap the 250+ individual
subcommands behind goal-oriented entry points, so neither humans nor
agents have to memorize the full command surface.

- recipes.py — the recipe registry (executable routing tables ported
  from the SKILL.md routing tables) plus question classification.
- entry.py — the recipe execution engine and the `c2d` CLI handler.
"""
