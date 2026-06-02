"""
Subprocess ``git`` wrapper backing the config-dir version history.

Synchronous on purpose — every method shells out to ``git`` and is
meant to be driven from an executor by
:class:`VersionHistoryController`, never from the event loop. The
wrapper is deliberately conservative about a *pre-existing* repo
(``/config/esphome`` is commonly already a git work tree, or sits
inside one such as ``/config``):

- It **adopts** an existing work tree rather than re-initialising,
  and never rewrites the user's ``.gitignore``.
- Commits are **pathspec-scoped** (``git commit -- <paths>``) so a
  partial commit never sweeps the user's unrelated staged edits into
  our history.
- It never writes ``user.*`` into the repo/global config — the commit
  identity is passed per-invocation via ``git -c user.name=...``.
- ``--no-verify`` + ``commit.gpgsign=false`` keep our automatic commits
  from tripping the user's hooks or blocking on a signing prompt.

If the ``git`` binary is absent the wrapper stays disabled and every
method is a no-op, so version history is a soft, optional feature.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

# Identity stamped on every automatic commit. Passed per-invocation
# with ``git -c`` so we never touch the user's global/repo config.
_COMMIT_NAME = "ESPHome Device Builder"
_COMMIT_EMAIL = "device-builder@esphome.io"

# Written only when *we* create the repo; a pre-existing repo keeps the
# user's own ignore rules untouched. Build artifacts under ``.esphome``
# and the machine-state sidecars are noise; ``secrets.yaml`` is ignored
# by default so credentials don't land in local history (or a remote the
# user later adds) — they can un-ignore it deliberately.
_DEFAULT_GITIGNORE = """\
# Managed by ESPHome Device Builder — created because this directory
# was not already a git repository. Edit freely; it won't be regenerated.
.esphome/
.device-builder*.json
secrets.yaml
"""


@dataclass(slots=True)
class CommitInfo:
    """One commit touching a file, as surfaced to the history UI."""

    sha: str
    short_sha: str
    author: str
    timestamp: int
    message: str


@dataclass(slots=True)
class GitRepo:
    """A git work tree wrapping the dashboard's config directory."""

    config_dir: Path
    git_bin: str | None = None
    toplevel: Path | None = None
    enabled: bool = field(default=False)

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def discover_or_init(self) -> None:
        """Locate an enclosing work tree, or initialise a fresh repo.

        Sets :attr:`enabled` / :attr:`toplevel`. A pre-existing work
        tree is adopted as-is; otherwise a new repo is created in
        :attr:`config_dir` with a default ``.gitignore``. Any failure
        leaves the feature disabled rather than raising.
        """
        self.git_bin = shutil.which("git")
        if self.git_bin is None:
            _LOGGER.info("git binary not found; version history disabled")
            return
        try:
            toplevel = self._discover_toplevel()
            if toplevel is not None:
                self.toplevel = toplevel
                self.enabled = True
                _LOGGER.debug("Adopted existing git work tree at %s", toplevel)
                return
            self._init_repo()
        except OSError as exc:
            _LOGGER.warning("Could not set up version-history git repo: %s", exc)

    def _discover_toplevel(self) -> Path | None:
        """Return the enclosing work tree's root, or ``None`` if there is none."""
        result = self._run(
            ["rev-parse", "--show-toplevel"],
            cwd=self.config_dir,
            check=False,
        )
        if result.returncode != 0:
            return None
        root = result.stdout.strip()
        return Path(root) if root else None

    def _init_repo(self) -> None:
        """Create a fresh repo in ``config_dir`` and seed the initial snapshot.

        Because the repo is brand new there's no user work-in-progress
        to protect, so the seed commit stages everything not ignored
        (the existing configs) — giving each device a first version in
        history immediately rather than only once it's first edited.
        """
        self._run(["init", str(self.config_dir)], cwd=self.config_dir, check=True)
        self.toplevel = self.config_dir
        self.enabled = True
        gitignore = self.config_dir / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text(_DEFAULT_GITIGNORE, encoding="utf-8")
        self._run(["add", "-A"], check=False)
        self._commit_index("Initialize version history")

    def _commit_index(self, message: str) -> None:
        """Commit whatever is currently staged (no pathspec); skip if empty.

        Only used for the fresh-init seed — every other commit is
        pathspec-scoped via :meth:`commit_paths` so it can't sweep the
        user's staged work.
        """
        if self._run(["diff", "--cached", "--quiet"], check=False).returncode == 0:
            return
        self._run(self._commit_argv(message, ()), check=False)

    @staticmethod
    def _commit_argv(message: str, pathspec: tuple[str, ...]) -> list[str]:
        """Build a ``git commit`` argv: our identity, no hooks, no signing.

        Identity is passed per-invocation with ``-c`` so we never write
        ``user.*`` into the user's config; ``--no-verify`` /
        ``commit.gpgsign=false`` keep automatic commits from tripping a
        hook or a signing prompt. A non-empty *pathspec* scopes the
        commit to exactly those paths.
        """
        argv = [
            "-c",
            f"user.name={_COMMIT_NAME}",
            "-c",
            f"user.email={_COMMIT_EMAIL}",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--no-verify",
            "-m",
            message,
        ]
        if pathspec:
            argv += ["--", *pathspec]
        return argv

    # ------------------------------------------------------------------
    # writes
    # ------------------------------------------------------------------

    def commit_paths(self, paths: list[Path], message: str) -> str | None:
        """Stage and commit exactly *paths*; return the new sha or ``None``.

        ``None`` means nothing changed for those paths (or the feature
        is disabled). The commit is pathspec-scoped, so the user's
        unrelated staged changes are never folded in. Picks up
        creations, edits, and deletions uniformly (``git add -A``).
        """
        if not self.enabled or not paths:
            return None
        spec = [str(p) for p in paths]
        try:
            self._run(["add", "-A", "--", *spec], check=True)
            staged = self._run(
                ["diff", "--cached", "--quiet", "--", *spec],
                check=False,
            )
            if staged.returncode == 0:
                return None  # nothing staged for these paths
            self._run(self._commit_argv(message, tuple(spec)), check=True)
        except subprocess.CalledProcessError as exc:
            _LOGGER.warning("git commit failed for %s: %s", message, exc.stderr or exc)
            return None
        head = self._run(["rev-parse", "HEAD"], check=False)
        return head.stdout.strip() if head.returncode == 0 else None

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------

    def log_file(self, path: Path, *, limit: int = 100) -> list[CommitInfo]:
        """Return the commit history touching *path*, newest first."""
        if not self.enabled:
            return []
        # ``%x1f`` (unit separator) between fields, ``%x1e`` (record
        # separator) between commits — neither appears in commit
        # metadata, so the parse can't be fooled by a message that
        # contains a newline or tab.
        fmt = "%H%x1f%h%x1f%an%x1f%at%x1f%s"
        result = self._run(
            ["log", f"--max-count={limit}", f"--format={fmt}%x1e", "--", str(path)],
            check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return []
        commits: list[CommitInfo] = []
        for raw in result.stdout.split("\x1e"):
            record = raw.strip("\n")
            if not record:
                continue
            sha, short, author, ts, subject = record.split("\x1f", 4)
            commits.append(
                CommitInfo(
                    sha=sha,
                    short_sha=short,
                    author=author,
                    timestamp=int(ts),
                    message=subject,
                )
            )
        return commits

    def latest_content(self, path: Path) -> tuple[str, str] | None:
        """Return ``(sha, content)`` of the newest commit where *path* existed.

        Used to restore a deleted file without the caller having to
        know which commit last held it (the deletion commit itself has
        no content). ``None`` if the path has no recoverable version.
        """
        for commit in self.log_file(path):
            content = self.file_at(path, commit.sha)
            if content is not None:
                return commit.sha, content
        return None

    def file_at(self, path: Path, sha: str) -> str | None:
        """Return *path*'s content at commit *sha*, or ``None`` if absent there."""
        if not self.enabled:
            return None
        rel = self._rel_to_toplevel(path)
        result = self._run(["show", f"{sha}:{rel}"], check=False)
        if result.returncode != 0:
            return None
        return result.stdout

    def diff_file(self, path: Path, sha: str) -> str:
        """Return a unified diff of *path* between *sha* and the working tree."""
        if not self.enabled:
            return ""
        result = self._run(["diff", sha, "--", str(path)], check=False)
        return result.stdout if result.returncode == 0 else ""

    def deleted_files(self, *, suffixes: tuple[str, ...] = (".yaml", ".yml")) -> list[str]:
        """Return repo-relative paths once committed but absent from the work tree.

        Drives the "restore a deleted device" view: a file that has
        history but no working-tree copy. Restricted to top-level
        configs with *suffixes* so archived copies and nested includes
        don't show up as deletable devices.
        """
        if not self.enabled:
            return []
        # Every path that ever existed in history.
        ever = self._run(
            ["log", "--pretty=format:", "--name-only", "--diff-filter=A"],
            check=False,
        )
        if ever.returncode != 0:
            return []
        assert self.toplevel is not None
        seen: set[str] = set()
        deleted: list[str] = []
        for raw in ever.stdout.splitlines():
            rel = raw.strip()
            if not rel or rel in seen:
                continue
            seen.add(rel)
            # Top-level configs only — no nested path segments.
            if "/" in rel or not rel.endswith(suffixes):
                continue
            if not (self.toplevel / rel).exists():
                deleted.append(rel)
        return deleted

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _rel_to_toplevel(self, path: Path) -> str:
        """Return *path* relative to the work-tree root (git's pathspec base)."""
        assert self.toplevel is not None
        try:
            return str(path.resolve().relative_to(self.toplevel.resolve()))
        except ValueError:
            return path.name

    def _run(
        self,
        args: list[str],
        *,
        check: bool,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run ``git`` with *args* in the work tree; capture text output."""
        assert self.git_bin is not None
        # git_bin is a resolved absolute path from shutil.which and the
        # args are a fixed argv (never shell-interpreted), so the only
        # external input is the pathspec — safe.
        return subprocess.run(  # noqa: S603
            [self.git_bin, *args],
            cwd=str(cwd or self.toplevel or self.config_dir),
            capture_output=True,
            text=True,
            check=check,
        )
