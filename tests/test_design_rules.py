"""Keep persistence and private module names behind their public boundaries."""
import ast
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
MAX_FUNCTION_LINES = 120
# A string that is only an HTTP method, such as the Plane client's "DELETE", is not SQL.
HTTP_METHODS = {"GET", "POST", "PATCH", "PUT", "DELETE"}


class DesignRules(unittest.TestCase):
    def sources(self):
        return [ROOT / "main.py", *sorted((ROOT / "dispatch").rglob("*.py"))]

    def test_database_boundary(self):
        for path in self.sources():
            if path.parent == ROOT / "dispatch" / "runs":
                continue
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                        and node.value not in HTTP_METHODS:
                    self.assertIsNone(re.search(r"\b(SELECT|INSERT|UPDATE|DELETE|CREATE)\b", node.value),
                                      f"{path}:{node.lineno}: SQL outside the store")
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    self.assertFalse(node.func.attr in ("execute", "executemany", "executescript"),
                                     f"{path}:{node.lineno}: database call outside the store")

    def test_public_module_boundary(self):
        for path in self.sources():
            tree = ast.parse(path.read_text())
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    for alias in node.names:
                        self.assertFalse(alias.name.startswith("_"),
                                         f"{path}:{node.lineno}: private import")
                        imported.add(alias.asname or alias.name)
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
                    root = node.value
                    while isinstance(root, ast.Attribute):
                        root = root.value
                    if isinstance(root, ast.Name):
                        self.assertNotIn(root.id, imported,
                                         f"{path}:{node.lineno}: private module reference")

    def dispatch_imports(self):
        """Each dispatch module's dispatch imports, function-level ones included, as dotted names."""
        dispatch = ROOT / "dispatch"
        modules = {".".join(path.relative_to(ROOT).with_suffix("").parts).removesuffix(".__init__"): path
                   for path in sorted(dispatch.rglob("*.py"))}
        graph = {}
        for name, path in modules.items():
            package = name if path.name == "__init__.py" else name.rpartition(".")[0]
            found = set()
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Import):
                    found.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    base = package.split(".")
                    base = ".".join(base[:len(base) - node.level + 1]) if node.level else ""
                    base = ".".join(part for part in (base, node.module) if part)
                    # "from . import runs" names a module; "from .redact import log" names a value in one.
                    found.update(f"{base}.{alias.name}" if f"{base}.{alias.name}" in modules else base
                                 for alias in node.names)
            graph[name] = sorted(found & modules.keys() - {name})
        return graph

    def test_no_import_loops(self):
        # Modules depend on each other in one direction only, so a shared helper
        # lives in a module of its own rather than in one of its callers.
        graph = self.dispatch_imports()
        done, path = set(), []

        def visit(name):
            if name in path:
                return path[path.index(name):] + [name]
            if name in done:
                return None
            path.append(name)
            for imported in graph[name]:
                loop = visit(imported)
                if loop:
                    return loop
            path.pop()
            done.add(name)
            return None

        for name in graph:
            loop = visit(name)
            if loop:
                self.fail("import loop: " + " -> ".join(loop))

    def test_events_are_not_read_outside_the_store(self):
        # Events are best-effort history, so no decision may depend on them.
        runs_package = ROOT / "dispatch" / "runs"
        readers = set()
        for path in runs_package.glob("*.py"):
            for node in ast.parse(path.read_text()).body:
                if not isinstance(node, ast.FunctionDef) or node.name.startswith("_"):
                    continue
                sql = " ".join(n.value for n in ast.walk(node)
                               if isinstance(n, ast.Constant) and isinstance(n.value, str))
                if re.search(r"\bFROM events\b", sql) and not re.search(r"\b(INSERT INTO|UPDATE) events\b", sql):
                    readers.add(node.name)
        self.assertIn("events", readers)
        for path in (ROOT / "dispatch").rglob("*.py"):
            if path.parent == runs_package:
                continue
            tree = ast.parse(path.read_text())
            # Names bound to the runs package or one of its modules, however imported.
            store = {"runs"}
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and "runs" in (node.module or "").split("."):
                    store.update(alias.asname or alias.name for alias in node.names)
                    for alias in node.names:
                        self.assertNotIn(alias.name, readers, f"{path}:{node.lineno}: imports an event query")
            for node in ast.walk(tree):
                if (isinstance(node, ast.Attribute) and node.attr in readers
                        and isinstance(node.value, ast.Name) and node.value.id in store):
                    self.fail(f"{path}:{node.lineno}: reads events, which are history, not state")

    def test_functions_stay_short(self):
        too_long = []
        for path in self.sources():
            file = path.relative_to(ROOT).as_posix()
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    length = node.end_lineno - node.lineno + 1
                    if length <= MAX_FUNCTION_LINES:
                        continue
                    too_long.append(f"{file}:{node.lineno}: {node.name} is {length} lines")
        self.assertFalse(too_long, f"functions over {MAX_FUNCTION_LINES} lines:\n" + "\n".join(too_long))
