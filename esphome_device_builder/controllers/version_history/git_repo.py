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
import time
from dataclasses import dataclass, field
from itertools import batched
from pathlib import Path

import esphome_device_builder
from esphome_device_builder.constants import is_secrets_file

_LOGGER = logging.getLogger(__name__)

# The installed Device Builder package dir. Only ever sits inside a source
# checkout's git work tree, never under a user's /config — so it identifies the
# one repo we must never adopt as a history store: a config dir kept inside the
# clone (``--dev configs``) would otherwise commit user YAML into the project.
# Resolved from the package itself so it survives this module being moved.
_OWN_SOURCE_ROOT = Path(esphome_device_builder.__file__).resolve().parent

# Errors a git invocation raises for genuine git / environment reasons
# (a failed ``git`` invocation, the binary vanishing) as opposed to a
# programming bug. Callers swallow these as best-effort and let anything
# else propagate so real bugs surface instead of being mislabelled.
GIT_COMMIT_ERRORS: tuple[type[Exception], ...] = (OSError, subprocess.CalledProcessError)

# Identity stamped on every automatic commit. Passed per-invocation
# with ``git -c`` so we never touch the user's global/repo config.
_COMMIT_NAME = "ESPHome Device Builder"
_COMMIT_EMAIL = "device-builder@esphome.io"

# Files Device Builder must never let into git history: its own
# machine state (sidecars, locks), identity key, and remote-build peer
# credentials, plus ESPHome build artifacts and OS noise. None of these
# are user config. They're forced into the repo's *local* exclude
# (``.git/info/exclude``) on both init and adopt — see
# ``_ensure_local_excludes`` — so a key can't be committed even when the
# user's own ``.gitignore`` (or a stock ESPHome one) doesn't cover it,
# and without ever modifying that tracked ``.gitignore``.
_MANAGED_EXCLUDES = (
    ".esphome/",
    ".device-builder*",
    ".receiver_peers.json",
    ".offloader_pairings.json",
    ".DS_Store",
)

# Markers delimiting our block in ``.git/info/exclude`` so the write is
# idempotent across restarts.
_EXCLUDE_MARKER = "# >>> ESPHome Device Builder (managed) >>>"
_EXCLUDE_END = "# <<< ESPHome Device Builder (managed) <<<"

# The seed must never capture the user's secrets file (credentials don't
# belong in a repo that may be pushed to a remote); ``is_secrets_file``
# filters it out. The CORE sentinel (``controllers/config/settings.py``) is
# a virtual ``CORE.config_path`` value, never written to disk, so it can't
# be globbed and needs no filter.

# Fields ``git check-ignore -v -z`` emits per input path: source, linenum,
# pattern, pathname. A non-empty pattern marks that path ignored.
_CHECK_IGNORE_FIELDS = 4

# Glob patterns for the YAML configs this feature versions.
_YAML_GLOBS = ("*.yaml", "*.yml")

# Min age before a leftover ``index.lock`` counts as stale and is cleared;
# younger than this a live git may still hold it, so we leave it alone.
_STALE_LOCK_SECONDS = 30.0

# Repo-local git-config verdict (``true``/``false``) for whether we own a
# repo. Written at init and cached on the first adopt so ownership survives a
# restart and the seed-root backfill scan runs at most once per repo. Unset
# means "not yet resolved"; an adopted user repo ends up cached ``false``.
_MANAGED_CONFIG_KEY = "device-builder.managed"

# Written only when *we* create the repo and no ``.gitignore`` exists; a
# pre-existing one is left untouched (the local exclude is what actually
# protects the secrets above). ``secrets.yaml`` is ignored here — not in
# the forced local exclude — because whether to version it is the user's
# call, but the safe default keeps credentials out of a repo that may
# later be pushed to a remote.
_DEFAULT_GITIGNORE = "".join(
    [
        "# Managed by ESPHome Device Builder — created because this directory\n",
        "# was not already a git repository. Edit freely; it won't be regenerated.\n",
        *(f"{pattern}\n" for pattern in _MANAGED_EXCLUDES),
        "secrets.yaml\n",
    ]
)


class GitCommandError(subprocess.CalledProcessError):
    """``CalledProcessError`` whose ``str`` carries git's stderr.

    Plain ``CalledProcessError`` renders only the exit status, so a
    logged ``exc_info`` drops the ``fatal:`` line that says *why* — the
    one fact needed to triage a failed commit (stale ``index.lock``,
    dubious ownership, disk full).
    """

    def __str__(self) -> str:
        """Exit-status line followed by git's trimmed stderr, when present."""
        detail = (self.stderr or "").strip()
        base = super().__str__()
        return f"{base}: {detail}" if detail else base


class GitIndexLockBusyError(GitCommandError):
    """A live writer holds the ``index.lock``; the async caller should retry."""


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
    # True for a repo *we* initialised, set from the persisted
    # _MANAGED_CONFIG_KEY so it survives a restart's adopt path. Gates
    # stale-index.lock recovery; an adopted user repo is never ours.
    managed: bool = field(default=False)

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
            if (
                toplevel is not None
                and not _encloses_own_source(toplevel)
                and not self._enclosing_repo_ignores_config_dir()
            ):
                self.toplevel = toplevel
                self.enabled = True
                self.managed = self._adopt_ownership()
                self._ensure_local_excludes()
                _LOGGER.debug("Adopted existing git work tree at %s", toplevel)
                return
            if toplevel is not None:
                reason = (
                    "is inside the Device Builder source checkout"
                    if _encloses_own_source(toplevel)
                    else "is ignored by the enclosing git repo"
                )
                _LOGGER.info(
                    "Config dir %s %s (%s); creating a config-local history repo "
                    "instead of committing into it",
                    self.config_dir,
                    reason,
                    toplevel,
                )
            elif (git_entry := self.config_dir / ".git").is_symlink() or git_entry.exists():
                # rev-parse found no work tree yet ``.git`` is physically
                # present: an unusable git dir (a submodule / worktree pointer
                # file or symlink whose target isn't mounted, or a corrupt
                # repo). ``is_symlink()`` catches a broken symlink that
                # ``exists()`` misses; a valid symlink to a usable repo never
                # reaches here (rev-parse would have found its work tree).
                # Re-initialising over it fails, so disable rather than crash
                # on init.
                _LOGGER.info(
                    "Config dir %s has a .git git can't use here (likely a submodule or "
                    "worktree whose git dir isn't mounted); version history disabled",
                    self.config_dir,
                )
                self._disable()
                return
            self._init_repo()
        except GIT_COMMIT_ERRORS as exc:
            # Never leave the repo half-enabled: a failure after the adopt
            # branch set ``enabled`` would otherwise strand it.
            self._disable()
            _LOGGER.warning("Could not set up version-history git repo: %s", exc)

    def _disable(self) -> None:
        """Reset to the fully-disabled state (no work tree adopted or created)."""
        self.enabled = False
        self.toplevel = None
        self.managed = False

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

    def _enclosing_repo_ignores_config_dir(self) -> bool:
        """Whether the enclosing work tree ignores ``config_dir`` itself."""
        result = self._run(
            # ``--`` so a config_dir starting with ``-`` isn't read as an option.
            ["check-ignore", "-q", "--", str(self.config_dir)],
            cwd=self.config_dir,
            check=False,
        )
        return result.returncode == 0

    def _init_repo(self) -> None:
        """Create a fresh repo in ``config_dir`` and seed the existing configs.

        Seeds only the top-level YAML configs (plus our ``.gitignore``) —
        the same unit the dashboard versions — so each device starts with
        a first version in history immediately. Deliberately **not**
        ``git add -A``: the config dir may also hold large non-config
        files (logs, databases, media) that have no business in git
        history, and git history is forever.
        """
        self._run(["init", str(self.config_dir)], cwd=self.config_dir, check=True)
        self.toplevel = self.config_dir
        self.enabled = True
        self.managed = True
        self._mark_managed(managed=True)
        self._ensure_local_excludes()
        gitignore = self.config_dir / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text(_DEFAULT_GITIGNORE, encoding="utf-8")
        # ``.gitignore`` always exists here (just written or pre-existing),
        # so the seed is never empty.
        self._run(["add", "--", *self._seed_paths()], check=False)
        self._commit_index("Initialize version history")

    def _seed_paths(self) -> list[str]:
        """Top-level YAML configs (+ our ``.gitignore``) to stage on first init.

        Top-level only and YAML-only by design: matches the dashboard's
        unit of versioning and keeps logs / databases / images out of
        history. ``secrets.yaml`` is left out — credentials don't belong
        in a repo that may later be pushed to a remote.
        """
        names = [".gitignore"]
        for pattern in _YAML_GLOBS:
            names += [
                path.name
                for path in sorted(self.config_dir.glob(pattern))
                if not is_secrets_file(path)
            ]
        return [name for name in names if (self.config_dir / name).exists()]

    def _ensure_local_excludes(self) -> None:
        """Append our managed patterns to ``.git/info/exclude`` (idempotent).

        ``info/exclude`` is git's repo-local ignore: never committed,
        invisible to the user's tracked ``.gitignore``, and applied on
        top of it. Writing here guarantees our secrets / state are never
        staged, regardless of how the user configured their repo, without
        mutating anything they track.
        """
        result = self._run(["rev-parse", "--git-path", "info/exclude"], check=False)
        if result.returncode != 0 or not result.stdout.strip():
            return
        exclude_path = Path(result.stdout.strip())
        if not exclude_path.is_absolute():
            exclude_path = (self.toplevel or self.config_dir) / exclude_path
        try:
            existing = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
            if _EXCLUDE_MARKER in existing:
                return
            exclude_path.parent.mkdir(parents=True, exist_ok=True)
            block = "\n".join(["", _EXCLUDE_MARKER, *_MANAGED_EXCLUDES, _EXCLUDE_END, ""])
            with exclude_path.open("a", encoding="utf-8") as handle:
                handle.write(block)
        except OSError as exc:
            _LOGGER.debug("Could not write git info/exclude: %s", exc)

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
        is disabled). A genuine git error raises ``CalledProcessError``
        rather than being swallowed, so the controller can tell a no-op
        from a failure. The commit is pathspec-scoped, so the user's
        unrelated staged changes are never folded in, and picks up
        creations, edits, and deletions uniformly (``git add -A``).
        """
        if not self.enabled or not paths:
            return None
        # A path that's gone from disk and untracked has nothing to commit:
        # the dashboard delete already recorded the removal, or an atomic-save
        # editor briefly removed it. Drop those. A gone-but-tracked path stays
        # so its deletion is staged (git add -A picks up the removal).
        present: list[Path] = []
        gone: list[Path] = []
        for p in paths:
            (present if p.exists() else gone).append(p)
        # Drop present paths git would refuse as ignored (e.g. secrets.yaml,
        # deliberately kept out of history); explicitly listing one makes
        # ``git add`` fail, and a commit of only such paths is a clean no-op.
        # Costs one ``check-ignore`` per save, ahead of the add/diff/commit.
        ignored = set(self._ignored_subset(present))
        spec: list[str] = [str(p) for p in present if p not in ignored]
        if gone:
            spec += [str(p) for p in self._tracked_subset(gone)]
        if not spec:
            return None
        self._run_write(["add", "-A", "--", *spec])
        staged = self._run(["diff", "--cached", "--quiet", "--", *spec], check=False)
        if staged.returncode == 0:
            return None  # nothing staged for these paths
        self._run_write(self._commit_argv(message, tuple(spec)))
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

    def _run_write(self, args: list[str]) -> None:
        """
        Run a checked git write, handling index.lock contention.

        A stale lock is cleared and the write retried in place (once); a
        fresh lock raises :class:`GitIndexLockBusyError` for the async
        caller to back off and retry. Any other failure propagates.
        """
        cleared_stale = False
        while True:
            try:
                self._run(args, check=True)
            except subprocess.CalledProcessError as exc:
                if not self._is_index_lock_error(exc):
                    raise
                # Clear a stale lock once and retry in place; a re-collision
                # after that (a fresh writer grabbed it) drops to the
                # freshness check below and becomes a retryable busy.
                if not cleared_stale and self._clear_stale_index_lock(exc):
                    cleared_stale = True
                    continue
                if not self._index_lock_is_fresh():
                    # Stale but unclearable (an adopted repo we won't touch, or
                    # a lock we couldn't unlink): it won't free itself, so
                    # surface the original failure instead of spinning on it.
                    raise
                # A fresh lock is a live concurrent writer; tell the async
                # caller to back off and retry.
                raise GitIndexLockBusyError(
                    exc.returncode, exc.cmd, output=exc.output, stderr=exc.stderr
                ) from exc
            else:
                return

    @staticmethod
    def _is_index_lock_error(exc: subprocess.CalledProcessError) -> bool:
        """Whether *exc*'s stderr blames a contended ``index.lock``."""
        return "index.lock" in (exc.stderr or "")

    def _index_lock_is_fresh(self) -> bool:
        """Whether the index.lock is young enough that a live writer may hold it.

        A vanished or unreadable lock counts as fresh (the retry resolves
        it); one aged past :data:`_STALE_LOCK_SECONDS` won't free itself.
        """
        lock = self._index_lock_path()
        if lock is None:
            return True
        try:
            age = time.time() - lock.stat().st_mtime
        except OSError:
            return True
        return age < _STALE_LOCK_SECONDS

    def _clear_stale_index_lock(self, exc: subprocess.CalledProcessError) -> bool:
        """
        Remove the index.lock blamed by *exc* iff it's stale; return whether removed.

        Gated on ownership (only a repo we manage, never an adopted
        ``/config``) and age (:data:`_STALE_LOCK_SECONDS`), so a lock a
        live git is actively holding is never deleted out from under it.
        """
        if not self.managed or not self._is_index_lock_error(exc):
            return False
        lock = self._index_lock_path()
        if lock is None or not lock.exists():
            return False
        try:
            age = time.time() - lock.stat().st_mtime
        except OSError:
            return False
        if age < _STALE_LOCK_SECONDS:
            return False  # fresh — a live git may hold it; don't clobber
        try:
            lock.unlink()
        except OSError as unlink_exc:
            _LOGGER.warning("Could not remove stale git index.lock at %s: %s", lock, unlink_exc)
            return False
        _LOGGER.warning("Removed stale git index.lock at %s (age %.0fs)", lock, age)
        return True

    def _adopt_ownership(self) -> bool:
        """
        Resolve whether an adopted repo is one we own, caching the verdict once.

        A prior run's cached :data:`_MANAGED_CONFIG_KEY` short-circuits.
        Otherwise the seed-root backfill identifies repos we created before
        the marker existed; the verdict (ours or not) is stamped so the scan
        runs at most once. A scan that couldn't run is left uncached.
        """
        cached = self._read_managed_flag()
        if cached is not None:
            return cached
        owned = self._looks_self_initialised()
        if owned is None:
            return False  # couldn't determine; re-resolve next start
        self._mark_managed(managed=owned)
        return owned

    def _mark_managed(self, *, managed: bool) -> None:
        """Persist the ownership verdict so the next start skips re-deriving it."""
        value = "true" if managed else "false"
        result = self._run(["config", "--local", _MANAGED_CONFIG_KEY, value], check=False)
        if result.returncode != 0:
            _LOGGER.warning(
                "Could not stamp managed flag on %s: %s", self.toplevel, result.stderr.strip()
            )

    def _read_managed_flag(self) -> bool | None:
        """Return the cached ownership verdict from a prior run, or ``None`` if unresolved."""
        result = self._run(["config", "--local", "--get", _MANAGED_CONFIG_KEY], check=False)
        if result.returncode != 0:
            return None
        return result.stdout.strip() == "true"

    def _looks_self_initialised(self) -> bool | None:
        """
        Whether a root commit was authored by our seed — backfill for pre-marker repos.

        The ``Initialize version history`` seed is authored by our identity,
        which an adopted user repo's root commit never is; git filters on
        the author so we only ask whether such a root exists. ``None`` when
        the query couldn't run (e.g. a repo with no commits yet).
        """
        result = self._run(
            ["log", "--max-parents=0", f"--author={_COMMIT_EMAIL}", "--format=%H"],
            check=False,
        )
        if result.returncode != 0:
            return None
        return bool(result.stdout.strip())

    def _index_lock_path(self) -> Path | None:
        """Resolve the work tree's ``index.lock`` path (handles split git dirs)."""
        result = self._run(["rev-parse", "--git-path", "index.lock"], check=False)
        if result.returncode != 0 or not result.stdout.strip():
            return None
        lock = Path(result.stdout.strip())
        if not lock.is_absolute():
            lock = (self.toplevel or self.config_dir) / lock
        return lock

    def _tracked_subset(self, paths: list[Path]) -> list[Path]:
        """Return the subset of *paths* git tracks in the index (one ls-files call).

        Runs with ``check=True``: ``ls-files`` exits 0 (empty output) for an
        untracked pathspec, so a non-zero exit is a genuine git failure and
        must propagate rather than masquerade as "nothing tracked".
        """
        result = self._run(
            ["ls-files", "-z", "--full-name", "--", *(str(p) for p in paths)], check=True
        )
        tracked = {rel for rel in result.stdout.split("\0") if rel}
        return [p for p in paths if self._rel_to_toplevel(p) in tracked]

    def _ignored_subset(self, paths: list[Path]) -> list[Path]:
        """
        Return the subset of *paths* git would refuse to ``add`` as ignored.

        ``check-ignore`` honours the index, so a tracked path is never
        reported even if it matches a rule; the result is exactly the set
        ``git add`` rejects with "paths are ignored". A non-0/1 exit (a
        genuine failure) is treated as "nothing ignored" so a checkable
        path is never silently dropped.
        """
        if not paths:
            return []
        # ``--stdin -v --non-matching -z`` emits one record per input path, in
        # order, as four NUL-separated fields (source, linenum, pattern,
        # pathname). We correlate by position and read the pattern field
        # rather than comparing path strings: git echoes forward slashes while
        # ``str(p)`` uses backslashes on Windows, so a string match is
        # unreliable there. Exit 0/1 are both normal (some/none ignored);
        # anything else is a genuine failure we treat as "nothing ignored".
        result = self._run(
            ["check-ignore", "-z", "-v", "--non-matching", "--stdin"],
            check=False,
            input_text="\0".join(str(p) for p in paths),
        )
        if result.returncode not in (0, 1):
            return []
        # One record per input path, in order; correlate by position. The
        # trailing element from the final NUL is a short record zip() drops.
        records = batched(result.stdout.split("\0"), _CHECK_IGNORE_FIELDS)
        ignored: list[Path] = []
        # strict=False: the split's trailing element is a short final record
        # zip() stops before, since there's exactly one record per path.
        for path, record in zip(paths, records, strict=False):
            if len(record) != _CHECK_IGNORE_FIELDS:
                continue
            _source, _linenum, pattern, _pathname = record
            if pattern:
                ignored.append(path)
        return ignored

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
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run ``git`` with *args* in the work tree; capture text output.

        Checks the exit status here (rather than via ``check=True``) so a
        failure raises :class:`GitCommandError`, whose ``str`` carries the
        ``fatal:`` stderr line a bare ``CalledProcessError`` would drop.
        *input_text*, when set, is fed to the child's stdin (e.g. for
        ``--stdin`` pathspecs).
        """
        assert self.git_bin is not None
        # git_bin is a resolved absolute path from shutil.which and the
        # args are a fixed argv (never shell-interpreted), so the only
        # external input is the pathspec — safe.
        # close_fds=False mirrors helpers.subprocess: the default
        # close_fds=True makes the child iterate the fd table before
        # exec, which is pure overhead on memory-pressured systems; our
        # spawns don't rely on inherited fds being closed at the boundary.
        # --no-optional-locks stops reads from grabbing index.lock for an
        # optional refresh, so an unlocked read can't contend with a commit;
        # the required lock add/commit take is unaffected.
        result = subprocess.run(  # noqa: S603
            [self.git_bin, "--no-optional-locks", *args],
            cwd=str(cwd or self.toplevel or self.config_dir),
            input=input_text,
            capture_output=True,
            text=True,
            check=False,
            close_fds=False,
        )
        if check and result.returncode != 0:
            raise GitCommandError(
                result.returncode, result.args, output=result.stdout, stderr=result.stderr
            )
        return result


def _encloses_own_source(toplevel: Path) -> bool:
    """Whether *toplevel* is the Device Builder's own source checkout."""
    try:
        _OWN_SOURCE_ROOT.relative_to(toplevel.resolve())
    except ValueError:
        return False
    return True
