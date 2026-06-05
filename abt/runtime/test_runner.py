"""TestRunner — discovers and evaluates data assertions on node outputs."""

from pathlib import Path

import yaml

from ..models.prompt import TestDefinition

SAFE_BUILTINS = {
    "True": True,
    "False": False,
    "None": None,
    "int": int,
    "float": float,
    "str": str,
    "bool": bool,
    "list": list,
    "dict": dict,
    "tuple": tuple,
    "set": set,
    "len": len,
    "abs": abs,
    "min": min,
    "max": max,
    "sum": sum,
    "round": round,
    "isinstance": isinstance,
    "any": any,
    "all": all,
}


class TestResult:
    __slots__ = ("node_name", "test_name", "passed", "message", "assert_expr")

    def __init__(self, node_name: str, test_name: str, passed: bool,
                 message: str, assert_expr: str):
        self.node_name = node_name
        self.test_name = test_name
        self.passed = passed
        self.message = message
        self.assert_expr = assert_expr


class TestRunner:
    def __init__(self, prompt_root: Path):
        self.prompt_root = Path(prompt_root)
        self._tests: dict[str, list[TestDefinition]] = {}

    def discover(self) -> dict[str, list[TestDefinition]]:
        """Discover .test.yml files alongside .prompt files.

        Returns dict mapping qualified node name → list of TestDefinitions.
        """
        self._tests.clear()
        for test_file in sorted(self.prompt_root.rglob("*.test.yml")):
            with open(test_file, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}

            raw_tests = data.get("tests", [])
            if not isinstance(raw_tests, list):
                continue

            tests = [TestDefinition(**t) for t in raw_tests]

            # Map to qualified node name: path/to/node_name
            relative = test_file.relative_to(self.prompt_root)
            qualified = str(relative.with_suffix("").with_suffix("")).replace("\\", "/")

            if qualified in self._tests:
                self._tests[qualified].extend(tests)
            else:
                self._tests[qualified] = tests

        return self._tests

    def evaluate(self, node_name: str, output: dict | None) -> list[TestResult]:
        """Run all tests for a node against its output dict."""
        tests = self._tests.get(node_name, [])
        if not tests:
            return []

        results = []
        eval_env = {"__builtins__": SAFE_BUILTINS}
        context = output or {}

        for test in tests:
            expr = test.assert_
            try:
                passed = eval(expr, eval_env, context)
                if passed:
                    msg = "PASS"
                else:
                    msg = f"Assertion failed: {expr}"
            except Exception as e:
                passed = False
                msg = f"Error evaluating '{expr}': {e}"

            results.append(TestResult(
                node_name=node_name,
                test_name=test.name,
                passed=bool(passed),
                message=msg,
                assert_expr=expr,
            ))

        return results

    @property
    def test_count(self) -> int:
        return sum(len(t) for t in self._tests.values())

    def get_tested_nodes(self) -> set[str]:
        return set(self._tests.keys())
