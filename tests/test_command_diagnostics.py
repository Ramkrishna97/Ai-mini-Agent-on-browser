"""Focused checks for actionable local command feedback."""

import unittest

from command_diagnostics import parse_command_result


class CommandDiagnosticsTests(unittest.TestCase):
    def test_extracts_failed_pytest_name_and_traceback(self):
        output = ("Traceback (most recent call last):\n"
                  "  File \"tests/test_math.py\", line 4, in test_sum\n"
                  "AssertionError: 2 != 3\n"
                  "FAILED tests/test_math.py::test_sum - AssertionError: 2 != 3\n")
        result = parse_command_result(1, output, "")
        self.assertEqual(result["status"], "failed")
        self.assertTrue(any(item.get("test") == "tests/test_math.py::test_sum" for item in result["errors"]))
        self.assertTrue(any(item["kind"] == "exception" for item in result["errors"]))

    def test_extracts_compiler_location_and_ignores_noise(self):
        output = "> Task :compileJava FAILED\nApp.java:12: error: cannot find symbol\nBUILD FAILED\n"
        result = parse_command_result(1, "", output)
        self.assertEqual(result["errors"], [{"kind": "compiler_error", "message": "cannot find symbol",
                                               "file": "App.java", "line": 12}])

    def test_fallback_and_success(self):
        self.assertEqual(parse_command_result(0, "done", "")["errors"], [])
        result = parse_command_result(2, "last useful line\n", "")
        self.assertEqual(result["errors"][0]["message"], "last useful line")

    def test_extracts_maven_gradle_and_typescript_errors(self):
        maven = parse_command_result(1, "", "[ERROR] Failed to execute goal test\n")
        self.assertEqual(maven["errors"][0]["message"], "Failed to execute goal test")
        gradle = parse_command_result(1, "* What went wrong:\n> Compilation failed\n", "")
        self.assertEqual(gradle["errors"][0]["message"], "> Compilation failed")
        typescript = parse_command_result(2, "src/app.ts(8,3): error TS2304: Cannot find name 'x'.", "")
        self.assertEqual(typescript["errors"][0]["file"], "src/app.ts")
        self.assertEqual(typescript["errors"][0]["line"], 8)


if __name__ == "__main__":
    unittest.main()
