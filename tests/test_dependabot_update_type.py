"""Coverage for script/dependabot_update_type.py: diff-based Dependabot update classification."""

from __future__ import annotations

import io
import runpy
from pathlib import Path

import pytest

from script.dependabot_update_type import UNKNOWN, main, update_type

_ACTIONS_DIFF = """\
diff --git a/.github/workflows/actions.lock b/.github/workflows/actions.lock
index 08d893fee..e6c9f29e1 100644
--- a/.github/workflows/actions.lock
+++ b/.github/workflows/actions.lock
@@ -79,7 +79,7 @@ workflows:
         - 'codecov/codecov-action@v7.1.1'
-        - 'codspeedhq/action@v5.6.3'
+        - 'codspeedhq/action@v5.8.0'
     '.github/workflows/verify-lockfile.yml':
@@ -194,9 +194,9 @@ dependencies:
-    'codspeedhq/action@v5.6.3':
-        ref: 'v5.6.3'
-        commit: 'sha1-af75f77f17a054769abefaa42fe8dc2975469b5f'
+    'codspeedhq/action@v5.8.0':
+        ref: 'v5.8.0'
+        commit: 'sha1-b1f28b5eccf7cb5bfe6a2a89cb3053eb67f1150c'
         owner_id: 116658140
diff --git a/.github/workflows/test.yml b/.github/workflows/test.yml
index 01c4b36ee..31a6afa14 100644
--- a/.github/workflows/test.yml
+++ b/.github/workflows/test.yml
@@ -707,7 +707,7 @@ jobs:
-        uses: CodSpeedHQ/action@v5.6.3
+        uses: CodSpeedHQ/action@v5.8.0
         with:
"""

_NPM_LOCK_DIFF = """\
diff --git a/flasher/package-lock.json b/flasher/package-lock.json
index 78f986c37..4f3448ac0 100644
--- a/flasher/package-lock.json
+++ b/flasher/package-lock.json
@@ -1198,9 +1198,9 @@
     "node_modules/js-yaml": {
-      "version": "4.2.0",
-      "resolved": "https://registry.npmjs.org/js-yaml/-/js-yaml-4.2.0.tgz",
-      "integrity": "sha512-ePWsvanv0DWuDRsW8dnt+R4jQ31SCRCQ7hhNcPXZPsoBZiemuZNYGf7==",
+      "version": "4.3.2",
+      "resolved": "https://registry.npmjs.org/js-yaml/-/js-yaml-4.3.2.tgz",
+      "integrity": "sha512-SFNOvSJ+Dgf/9An904Yx+CgSlIPCkIpao4qo51lpee25TIRejdH3==",
       "dev": true,
"""


def _pyproject_diff(old: str, new: str) -> str:
    return f"""\
diff --git a/pyproject.toml b/pyproject.toml
index c2131428b..3e83b84c5 100644
--- a/pyproject.toml
+++ b/pyproject.toml
@@ -82,7 +82,7 @@ test = [
   "pytest-xdist==3.8.0",
-  "ruff=={old}",
+  "ruff=={new}",
   "types-PyYAML",
\\ No newline at end of file
"""


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        ("0.17.4", "0.17.5", "patch"),
        ("0.17.4", "0.18.0", "minor"),
        ("1.9.0", "2.0.0", "major"),
        ("1.2", "1.2.1", "patch"),
        ("2026.11.0", "2026.11.1", "patch"),
    ],
)
def test_pyproject_pin_jump(old: str, new: str, expected: str) -> None:
    assert update_type(_pyproject_diff(old, new)) == expected


def test_actions_bump_with_lockfile_is_minor() -> None:
    assert update_type(_ACTIONS_DIFF) == "minor"


def test_npm_lockfile_bump_is_minor() -> None:
    assert update_type(_NPM_LOCK_DIFF) == "minor"


def test_largest_jump_wins() -> None:
    diff = _pyproject_diff("0.17.4", "0.17.5") + _pyproject_diff("1.0.0", "2.0.0")
    assert update_type(diff) == "major"


def test_header_lines_are_not_changes() -> None:
    diff = """\
diff --git a/v1.0.0 b/v2.0.0
--- a/v1.0.0
+++ b/v2.0.0
@@ -1 +1 @@
-x==1.0.0
+x==1.0.1
"""
    assert update_type(diff) == "patch"


def test_unpaired_block_is_unknown() -> None:
    diff = """\
@@ -1,2 +1 @@
-a==1.0.0
-b==1.0.0
+a==1.0.1
"""
    assert update_type(diff) == UNKNOWN


def test_version_count_mismatch_is_unknown() -> None:
    diff = """\
@@ -1 +1 @@
-a==1.0.0
+a==1.0.1 b==1.0.0
"""
    assert update_type(diff) == UNKNOWN


@pytest.mark.parametrize(
    "diff",
    [
        pytest.param("", id="empty"),
        pytest.param("@@ -1 +1 @@\n-sha: abc\n+sha: def\n", id="no-versions"),
        pytest.param("@@ -1 +1 @@\n-x==1.0.0\n+x==1.0.0  # pinned\n", id="unchanged-version"),
    ],
)
def test_no_version_change_is_unknown(diff: str) -> None:
    assert update_type(diff) == UNKNOWN


def test_main_prints_update_type(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(_pyproject_diff("0.17.4", "0.17.5")))
    main()
    assert capsys.readouterr().out == "patch\n"


def test_runs_as_script(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    script = Path(__file__).resolve().parent.parent / "script" / "dependabot_update_type.py"
    monkeypatch.setattr("sys.stdin", io.StringIO(_pyproject_diff("1.0.0", "2.0.0")))
    runpy.run_path(str(script), run_name="__main__")
    assert capsys.readouterr().out == "major\n"
