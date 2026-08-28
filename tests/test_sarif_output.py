"""Unit tests for sarif_output.py.

SARIF (Static Analysis Results Interchange Format) 2.1.0 is the
industry-standard format for static analysis findings, ingested by
GitHub Code Scanning, VS Code, Azure DevOps, etc.

This test suite covers:
- results_to_sarif(): basic SARIF structure, rule deduplication,
  severity-level mapping, location formatting
- races_to_sarif(): race-detection findings → SARIF
- taint_to_sarif(): taint-flow findings → SARIF (sanitized vs unsanitized)
- Schema compliance: every output has version, $schema, runs[], tool,
  results[] with required fields (ruleId, level, message, locations)
"""
import json
import unittest


def _is_valid_sarif_basic(sarif: dict) -> bool:
    """Check the minimum SARIF 2.1.0 structural requirements we rely on.

    Full schema validation would require the jsonschema library; this
    is a fast structural smoke check that catches missing required keys.
    """
    if not isinstance(sarif, dict):
        return False
    if sarif.get("version") != "2.1.0":
        return False
    if "$schema" not in sarif:
        return False
    runs = sarif.get("runs")
    if not isinstance(runs, list) or len(runs) == 0:
        return False
    run = runs[0]
    tool = run.get("tool", {})
    driver = tool.get("driver", {})
    if not driver.get("name"):
        return False
    if not isinstance(driver.get("rules"), list):
        return False
    if not isinstance(run.get("results"), list):
        return False
    for r in run["results"]:
        if "ruleId" not in r:
            return False
        if "level" not in r:
            return False
        if "message" not in r or "text" not in r["message"]:
            return False
        if not isinstance(r.get("locations", []), list):
            return False
    return True


class TestResultsToSarif(unittest.TestCase):
    """Tests for results_to_sarif (generic findings → SARIF)."""

    def test_empty_results_produces_valid_empty_sarif(self):
        from _builder.sarif_output import results_to_sarif
        sarif = results_to_sarif([])
        self.assertTrue(_is_valid_sarif_basic(sarif))
        self.assertEqual(sarif["runs"][0]["results"], [])
        self.assertEqual(sarif["runs"][0]["tool"]["driver"]["rules"], [])

    def test_basic_finding_produces_valid_sarif(self):
        from _builder.sarif_output import results_to_sarif
        findings = [{
            "rule_id": "test_rule",
            "message": "test issue",
            "level": "warning",
            "file": "/tmp/foo.c",
            "line": 42,
            "function": "my_func",
        }]
        sarif = results_to_sarif(findings)
        self.assertTrue(_is_valid_sarif_basic(sarif))
        run = sarif["runs"][0]
        self.assertEqual(len(run["results"]), 1)
        r = run["results"][0]
        self.assertEqual(r["ruleId"], "test_rule")
        self.assertEqual(r["level"], "warning")
        self.assertEqual(r["message"]["text"], "test issue")
        # Location should have file + startLine
        self.assertEqual(len(r["locations"]), 1)
        loc = r["locations"][0]
        self.assertEqual(loc["physicalLocation"]["artifactLocation"]["uri"], "/tmp/foo.c")
        self.assertEqual(loc["physicalLocation"]["region"]["startLine"], 42)
        # Function stored in properties
        self.assertEqual(r["properties"]["function"], "my_func")

    def test_rule_deduplication(self):
        """Multiple findings with the same rule_id → single rule entry."""
        from _builder.sarif_output import results_to_sarif
        findings = [
            {"rule_id": "dup_rule", "message": "first", "level": "warning"},
            {"rule_id": "dup_rule", "message": "second", "level": "warning"},
            {"rule_id": "other_rule", "message": "third", "level": "warning"},
        ]
        sarif = results_to_sarif(findings)
        rules = sarif["runs"][0]["tool"]["driver"]["rules"]
        self.assertEqual(len(rules), 2)  # deduplicated
        rule_ids = {r["id"] for r in rules}
        self.assertEqual(rule_ids, {"dup_rule", "other_rule"})
        # All 3 results preserved
        self.assertEqual(len(sarif["runs"][0]["results"]), 3)

    def test_severity_high_mapped_to_error(self):
        from _builder.sarif_output import results_to_sarif
        sarif = results_to_sarif([{"rule_id": "x", "message": "m", "level": "high"}])
        self.assertEqual(sarif["runs"][0]["results"][0]["level"], "error")

    def test_severity_low_mapped_to_note(self):
        from _builder.sarif_output import results_to_sarif
        sarif = results_to_sarif([{"rule_id": "x", "message": "m", "level": "low"}])
        self.assertEqual(sarif["runs"][0]["results"][0]["level"], "note")

    def test_default_level_is_warning_when_missing(self):
        from _builder.sarif_output import results_to_sarif
        sarif = results_to_sarif([{"rule_id": "x", "message": "m"}])
        self.assertEqual(sarif["runs"][0]["results"][0]["level"], "warning")

    def test_finding_without_file_has_empty_locations(self):
        from _builder.sarif_output import results_to_sarif
        sarif = results_to_sarif([{"rule_id": "x", "message": "m"}])
        self.assertEqual(sarif["runs"][0]["results"][0]["locations"], [])

    def test_finding_with_file_but_no_line_omits_region(self):
        from _builder.sarif_output import results_to_sarif
        sarif = results_to_sarif([{"rule_id": "x", "message": "m", "file": "/tmp/x.c"}])
        loc = sarif["runs"][0]["results"][0]["locations"][0]
        self.assertIn("artifactLocation", loc["physicalLocation"])
        self.assertNotIn("region", loc["physicalLocation"])

    def test_tool_name_and_version_propagated(self):
        from _builder.sarif_output import results_to_sarif
        sarif = results_to_sarif([], tool_name="CustomTool", tool_version="9.9.9")
        self.assertEqual(sarif["runs"][0]["tool"]["driver"]["name"], "CustomTool")
        self.assertEqual(sarif["runs"][0]["tool"]["driver"]["version"], "9.9.9")

    def test_rule_name_uppercase_with_underscores(self):
        """rule_id 'my-cool-rule' → rule name 'MY_COOL_RULE'."""
        from _builder.sarif_output import results_to_sarif
        sarif = results_to_sarif([{"rule_id": "my-cool-rule", "message": "m"}])
        rules = sarif["runs"][0]["tool"]["driver"]["rules"]
        self.assertEqual(rules[0]["name"], "MY_COOL_RULE")


class TestRacesToSarif(unittest.TestCase):
    """Tests for races_to_sarif (detect-races → SARIF)."""

    def test_empty_races_produces_valid_sarif(self):
        from _builder.sarif_output import races_to_sarif
        sarif = races_to_sarif([])
        self.assertTrue(_is_valid_sarif_basic(sarif))

    def test_race_with_high_severity_is_error(self):
        from _builder.sarif_output import races_to_sarif
        races = [{
            "type": "data_race",
            "severity": "high",
            "description": "race on counter",
            "shared_resource": {"name": "counter"},
            "reader": {"source_file": "/tmp/r.c", "line": 10, "function": "reader"},
            "writer": {"source_file": "/tmp/w.c", "line": 20, "function": "writer"},
        }]
        sarif = races_to_sarif(races)
        self.assertTrue(_is_valid_sarif_basic(sarif))
        r = sarif["runs"][0]["results"][0]
        self.assertEqual(r["level"], "error")
        self.assertEqual(r["ruleId"], "race_data_race")
        self.assertEqual(r["locations"][0]["physicalLocation"]["artifactLocation"]["uri"], "/tmp/r.c")
        self.assertEqual(r["properties"]["function"], "reader")

    def test_race_with_low_severity_is_warning(self):
        from _builder.sarif_output import races_to_sarif
        races = [{
            "type": "toctou",
            "severity": "low",
            "description": "low-sev race",
            "reader": {"source_file": "/x.c", "line": 1, "function": "f"},
            "writer": {},
        }]
        sarif = races_to_sarif(races)
        self.assertEqual(sarif["runs"][0]["results"][0]["level"], "warning")

    def test_tool_name_is_code2database_races(self):
        from _builder.sarif_output import races_to_sarif
        sarif = races_to_sarif([])
        self.assertEqual(sarif["runs"][0]["tool"]["driver"]["name"], "Code2Database-races")


class TestTaintToSarif(unittest.TestCase):
    """Tests for taint_to_sarif (taint flows → SARIF)."""

    def test_empty_flows_produces_valid_sarif(self):
        from _builder.sarif_output import taint_to_sarif
        sarif = taint_to_sarif([])
        self.assertTrue(_is_valid_sarif_basic(sarif))

    def test_unsanitized_flow_is_error(self):
        from _builder.sarif_output import taint_to_sarif
        flows = [{
            "source": "user_input",
            "sink": "exec",
            "sanitized": False,
            "path": [{"function": "f1"}, {"function": "f2"}],
        }]
        sarif = taint_to_sarif(flows)
        self.assertTrue(_is_valid_sarif_basic(sarif))
        r = sarif["runs"][0]["results"][0]
        self.assertEqual(r["level"], "error")
        self.assertEqual(r["ruleId"], "taint_flow")
        self.assertIn("UNSANITIZED", r["message"]["text"])
        self.assertIn("2 hops", r["message"]["text"])  # path length mentioned

    def test_sanitized_flow_is_note(self):
        from _builder.sarif_output import taint_to_sarif
        flows = [{
            "source": "user_input",
            "sink": "exec",
            "sanitized": True,
            "path": [{"function": "sanitize"}],
        }]
        sarif = taint_to_sarif(flows)
        self.assertEqual(sarif["runs"][0]["results"][0]["level"], "note")
        self.assertIn("sanitized", sarif["runs"][0]["results"][0]["message"]["text"])

    def test_tool_name_is_code2database_taint(self):
        from _builder.sarif_output import taint_to_sarif
        sarif = taint_to_sarif([])
        self.assertEqual(sarif["runs"][0]["tool"]["driver"]["name"], "Code2Database-taint")


if __name__ == "__main__":
    unittest.main()
