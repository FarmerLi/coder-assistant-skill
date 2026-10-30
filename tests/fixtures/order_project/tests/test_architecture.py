import ast
from pathlib import Path
import unittest


class OrderBoundaryTests(unittest.TestCase):
    def test_api_uses_public_service_boundary(self):
        source = Path(__file__).resolve().parents[1] / "order_app" / "api.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                self.assertNotIn("store", (node.module or "").split("."))
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn("store", alias.name.split("."))
            if isinstance(node, ast.Attribute):
                self.assertFalse(node.attr.startswith("_"), "入口不访问其他对象的私有属性")


if __name__ == "__main__":
    unittest.main()
