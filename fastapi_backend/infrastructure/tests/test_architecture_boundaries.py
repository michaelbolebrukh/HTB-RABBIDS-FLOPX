"""
Layering guard: `platform` must never import `product`.

The startup-factory contract (`.claude/rules/startup-factory.md`) is that a new
instance clones this repo and replaces `product/` wholesale. That only works if
nothing reusable depends on it: `product -> platform/infrastructure` is allowed,
`platform -> product` never is, and `infrastructure` may call stable product
exports ONLY at explicit composition points.

The frontend enforces its half with ESLint. Nothing enforced the Python half, so
it was held by convention alone - which is exactly the kind of invariant that is
true until the week nobody is looking. This test is the enforcement: it parses
every module under `platform/` and `infrastructure/` and fails on any import of
the product package, whether written as `import product...`, `from product...`,
or resolved dynamically through `importlib.import_module("product...")` /
`__import__("product...")`.

The three legitimate `infrastructure -> product` seams are listed below with the
reason each exists. The list is checked in both directions: a new seam fails the
test, and a seam that no longer imports product fails it too, so the list stays
a description of the composition rather than a place violations go to retire.
"""

import ast
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]

PRODUCT_PACKAGE = "product"

# Every file allowed to reach into `product`, and why. A clone rewires exactly
# these; there are deliberately no others.
ALLOWED_COMPOSITION_POINTS: dict[str, str] = {
    "main.py": "mounts product.routers alongside platform.routers on the app",
    "infrastructure/startup.py": "runs product.startup.STARTUP_TASKS at boot",
    "infrastructure/database.py": (
        "imports product.models so product tables register on Base.metadata"
    ),
    "infrastructure/seeder.py": (
        "seeds product.organisations.SEEDED_ORGANISATIONS (orgs + partnership "
        "rows) at boot; the catalogue stays product-owned, the seeder only runs it"
    ),
}

# Test packages are excluded from the infrastructure sweep: several infrastructure
# tests import `product.models` purely to register tables for SQLite create_all.
# That is test-fixture coupling, tracked separately - not a runtime composition
# point, and not something this guard should legitimise by listing it above.
_TEST_DIR_NAMES = {"tests"}


def _python_files(root: Path):
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path


def _is_product_module(name: str | None) -> bool:
    return bool(name) and (
        name == PRODUCT_PACKAGE or name.startswith(f"{PRODUCT_PACKAGE}.")
    )


def _product_imports(path: Path) -> list[str]:
    """Every reference to the product package in one module, as reportable lines.

    Relative imports cannot reach `product` from inside `platform`/`infrastructure`
    (they are sibling top-level packages, so a relative escape would have to go
    above the root), which is why only absolute forms are matched.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_product_module(alias.name):
                    hits.append(f"line {node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and _is_product_module(node.module):
                names = ", ".join(alias.name for alias in node.names)
                hits.append(f"line {node.lineno}: from {node.module} import {names}")
        elif isinstance(node, ast.Call):
            func = node.func
            callee = (
                func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            )
            if callee in {"import_module", "__import__"} and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and _is_product_module(first.value):
                    hits.append(f"line {node.lineno}: {callee}({first.value!r})")

    return hits


def _violations(root: Path, *, skip_tests: bool) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in _python_files(root):
        rel = path.relative_to(BACKEND_ROOT).as_posix()
        if skip_tests and _TEST_DIR_NAMES.intersection(path.parts):
            continue
        hits = _product_imports(path)
        if hits:
            found[rel] = hits
    return found


def test_platform_never_imports_product():
    """platform/ is what survives a clone, so it cannot name the product at all:"""
    found = _violations(BACKEND_ROOT / "platform", skip_tests=False)
    assert not found, (
        "platform must not import product (a clone deletes product/ wholesale). "
        "Move the dependency behind a platform-owned interface the product "
        "implements, or hand the value in at an infrastructure composition "
        f"point. Violations: {found}"
    )


def test_infrastructure_product_imports_are_declared_composition_points():
    """infrastructure/ may call product only at the seams listed in this file:"""
    found = _violations(BACKEND_ROOT / "infrastructure", skip_tests=True)
    found.update(
        {
            "main.py": hits
            for hits in [_product_imports(BACKEND_ROOT / "main.py")]
            if hits
        }
    )

    undeclared = {
        rel: hits for rel, hits in found.items() if rel not in ALLOWED_COMPOSITION_POINTS
    }
    assert not undeclared, (
        "new infrastructure -> product seam(s). A composition point is a "
        "deliberate, documented exception: add it to ALLOWED_COMPOSITION_POINTS "
        "in this file with the reason it must exist, or remove the import. "
        f"Undeclared: {undeclared}"
    )


def test_declared_composition_points_are_all_real():
    """A declared seam that no longer imports product is stale documentation:"""
    stale = []
    for rel, reason in ALLOWED_COMPOSITION_POINTS.items():
        path = BACKEND_ROOT / rel
        assert path.exists(), f"{rel} is declared a composition point but does not exist"
        if not _product_imports(path):
            stale.append(f"{rel} ({reason})")

    assert not stale, (
        "these files are declared infrastructure -> product composition points "
        "but no longer import product; drop them from "
        f"ALLOWED_COMPOSITION_POINTS: {stale}"
    )
