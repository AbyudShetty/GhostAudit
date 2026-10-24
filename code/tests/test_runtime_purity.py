"""The runtime package makes no model calls and no network calls; usage_report.md depends on that being true."""

import ast
import re
import unittest
from pathlib import Path

from tests.helpers import CODE_DIR

FORBIDDEN_IMPORTS = {"requests", "urllib", "urllib3", "http", "httpx", "aiohttp", "socket", "ssl", "ftplib", "smtplib", "xmlrpc", "websockets",
                     "openai", "anthropic", "google", "cohere", "mistralai", "litellm", "langchain", "subprocess"}
RUNTIME = [CODE_DIR / "ghostaudit", CODE_DIR / "main.py"]


def runtime_files():
    for r in RUNTIME:
        yield from ([r] if r.is_file() else sorted(r.rglob("*.py")))


class RuntimePurity(unittest.TestCase):
    def test_no_network_model_or_subprocess_imports_in_the_runtime_package(self):
        for f in runtime_files():
            for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
                mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""] if isinstance(node, ast.ImportFrom) and node.level == 0 else []
                for m in mods:
                    self.assertNotIn(m.split(".")[0], FORBIDDEN_IMPORTS, f"{f.name} imports {m}")

    def test_no_dynamic_imports_or_shell_calls(self):
        for f in runtime_files():
            text = f.read_text(encoding="utf-8")
            self.assertIsNone(re.search(r"__import__\(|importlib\.import_module|os\.system\(|os\.popen\(", text), f.name)

    def test_the_only_third_party_import_is_pdfplumber(self):
        import sys

        third = set()
        for f in runtime_files():
            for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    third |= {a.name.split(".")[0] for a in node.names}
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    third.add(node.module.split(".")[0])
        third -= set(sys.stdlib_module_names) | {"ghostaudit"}
        self.assertLessEqual(third, {"pdfplumber"})
        self.assertIn("pdfplumber==", (CODE_DIR / "requirements.txt").read_text(encoding="utf-8"))

    def test_credentials_are_never_read_by_the_runtime_package(self):
        for f in runtime_files():
            self.assertIsNone(re.search(r"os\.environ|getenv", f.read_text(encoding="utf-8")), f.name)


if __name__ == "__main__":
    unittest.main()
