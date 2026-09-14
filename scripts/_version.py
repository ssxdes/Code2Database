"""Single source of truth for the Code2Database version.

Every shipped surface reads this constant so the numbers can never
drift apart:

- ``code2database_builder --version`` and ``code2database_scanner --version``
- the three skill manifests (skill.json / skill_analysis.json / skill_ops.json)
- the MCP registry manifest (server.json, including its package entries)
- the SARIF tool driver version and the LSP serverInfo version

tests/test_version_consistency.py pins all of them to this module.
"""

__version__ = "2.1.0"
