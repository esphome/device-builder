#!/usr/bin/env python3
"""
Render the device-builder release notes for a single release.

Mirrors release-drafter's label-driven categorisation, then *expands*
each frontend version pin in place: the GitHub releases between the
previous tag's pinned version (exclusive) and the current pinned version
(inclusive) are fetched from the frontend repo, and their bullets are
dropped into the matching backend category. The "Bump frontend to ..."
PRs themselves never appear in the rendered notes — readers care about
what changed, not which sequence of bumps landed it.

Bullets in the frontend's Dependencies / CI section are skipped: the
frontend's dependabot churn is irrelevant to a device-builder release.
Bullets under any unrecognised section heading are also dropped — the
frontend repo enforces a labelled PR per merge, so an unknown heading
only happens when a release body is hand-edited, in which case the
maintainer can drop the change into the relevant section by hand.
"""

from __future__ import annotations

import os
import re
import sys
import tomllib
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from github.Repository import Repository


# Path to the release-drafter config we share with the rolling-draft tool.
_CONFIG_PATH = ".github/release-drafter.yml"

# Frontend release-notes patterns. The frontend repo's release-drafter
# emits one bullet per PR with the same "$TITLE (by @$AUTHOR in #$NUMBER)"
# template, so we can pull title/author/number out cleanly. Backticks
# around the handle are a copy that GitHub adds when one PR's body is
# included into another via the dependency-update template, so be
# permissive about them.
_FRONTEND_BULLET_RE = re.compile(
    r"^[-*•]\s+"
    r"(?P<title>.+?)"
    r"\s+\(by\s+`?@(?P<author>[\w-]+)`?\s+in\s+#(?P<number>\d+)\)"
    r"\s*$"
)
_SECTION_HEADER_RE = re.compile(r"^##\s+(.+?)\s*$")
_FRONTEND_BUMP_TITLE_RE = re.compile(r"^Bump frontend to ")

# Versions are X.Y.Z; tuple-compare is enough for ordering. Anything
# fancier (pre-releases, build metadata) is unlikely on the frontend
# and would need a real packaging.version comparison.
_VERSION_RE = re.compile(r"^\d+(?:\.\d+)+$")

# Category labels whose section we never inline frontend bullets into.
# A frontend dependency bump is just dependabot churn from the user's
# perspective.
_FRONTEND_SKIP_LABELS = frozenset({"dependencies", "ci"})


def load_config(path: str = _CONFIG_PATH) -> dict[str, Any]:
    """Load the release-drafter config that defines categories and templates."""
    p = Path(path)
    if not p.exists():
        sys.exit(f"Error: {path} not found")
    with p.open() as f:
        return yaml.safe_load(f) or {}


def parse_pyproject_frontend_version(text: str | None, dep_name: str) -> str | None:
    """
    Extract the frontend pin from a pyproject.toml string.

    Returns ``None`` when the dep is missing or pinned with anything
    other than ``==``. We deliberately don't try to resolve ``>=``
    ranges — a release pin is always exact in this project.
    """
    if not text:
        return None
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None
    deps = (data.get("project") or {}).get("dependencies") or []
    prefix = f"{dep_name}=="
    for spec in deps:
        if isinstance(spec, str) and spec.startswith(prefix):
            return spec[len(prefix) :].strip().strip('"').strip("'")
    return None


def get_pyproject_at(repo: Repository, ref: str | None) -> str | None:
    """Fetch ``pyproject.toml`` at ``ref`` (tag, branch, or SHA)."""
    from github import GithubException

    if not ref:
        return None
    try:
        contents = repo.get_contents("pyproject.toml", ref=ref)
    except GithubException as e:  # pragma: no cover - network failure
        print(f"Warning: cannot read pyproject.toml at {ref}: {e}", file=sys.stderr)
        return None
    if isinstance(contents, list):  # directory; shouldn't happen
        return None
    return contents.decoded_content.decode("utf-8")


def _version_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in v.split("."))


def get_frontend_releases_in_range(
    frontend_repo: Repository,
    *,
    after: str | None,
    up_to: str,
) -> list[Any]:
    """
    Yield frontend releases with ``after < tag <= up_to``, oldest-first.

    ``after`` is exclusive — its body shipped with the previous backend
    release — and ``up_to`` is inclusive. Returns an empty list if either
    bound isn't a recognised X.Y.Z version (e.g. an experimental tag),
    rather than guessing how to compare.
    """
    if not _VERSION_RE.match(up_to):
        return []
    up_to_t = _version_tuple(up_to)
    after_t = _version_tuple(after) if after and _VERSION_RE.match(after) else None

    selected: list[Any] = []
    for rel in frontend_repo.get_releases():
        tag = rel.tag_name or ""
        if not _VERSION_RE.match(tag):
            continue
        t = _version_tuple(tag)
        if after_t is not None and t <= after_t:
            continue
        if t > up_to_t:
            continue
        selected.append(rel)
    selected.sort(key=lambda r: _version_tuple(r.tag_name))
    return selected


def parse_frontend_release_body(
    body: str,
    *,
    category_titles: list[str],
    skip_titles: set[str],
    frontend_repo_full_name: str,
    server_url: str = "https://github.com",
) -> dict[str, list[dict[str, Any]]]:
    """
    Bucket a frontend release body into category -> list of changes.

    Walks the body line-by-line, tracking the most recent ``## TITLE``
    heading. Bullets emitted while the current heading matches a known
    backend category title (and isn't a skip-list bucket like
    Dependencies / CI) are captured into a dict the renderer can reuse.

    The frontend repo writes ``#NNN`` for its own PR refs; we synthesise
    the absolute URL here so the rendered link in the backend's release
    notes lands on the actual frontend PR rather than auto-linking to
    the same number in the backend repo.
    """
    by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    title_set = set(category_titles)
    current_title: str | None = None

    for raw in body.splitlines():
        line = raw.rstrip()
        header_match = _SECTION_HEADER_RE.match(line)
        if header_match:
            heading = header_match.group(1).strip()
            current_title = heading if heading in title_set else None
            continue

        if current_title is None or current_title in skip_titles:
            continue

        bullet = _FRONTEND_BULLET_RE.match(line.strip())
        if not bullet:
            continue

        number = int(bullet.group("number"))
        by_category[current_title].append(
            {
                "title": bullet.group("title").strip(),
                "author": bullet.group("author"),
                "number": number,
                "url": f"{server_url}/{frontend_repo_full_name}/pull/{number}",
                "is_frontend": True,
            }
        )

    return by_category


def render_change_line(template: str, change: dict[str, Any]) -> str:
    """
    Render a single bullet from a captured change dict.

    For backend PRs we emit ``#NNN`` literally — GitHub auto-links to
    the surrounding repo, which is the right destination. For frontend
    bullets we pre-substitute ``#$NUMBER`` with an explicit markdown
    link to the frontend PR, prefixed with ``frontend`` so readers can
    tell at a glance which repo the number belongs to; otherwise the
    same auto-link logic would silently re-resolve the number against
    the backend repo and either point at the wrong PR or render as a
    dead link.
    """
    line = template
    if change.get("is_frontend"):
        line = line.replace("#$NUMBER", f"[frontend#{change['number']}]({change['url']})")
    line = line.replace("$TITLE", str(change["title"]))
    line = line.replace("$AUTHOR", str(change["author"]))
    line = line.replace("$NUMBER", str(change["number"]))
    return line.replace("$URL", str(change["url"]))


def build_release_notes(
    *,
    config: dict[str, Any],
    by_category: dict[str, list[dict[str, Any]]],
    previous_tag: str | None,
    repo_url: str,
) -> str:
    """Render the final body using the release-drafter ``template`` field."""
    change_template = config.get("change-template", "- $TITLE (@$AUTHOR) #$NUMBER")
    category_configs = config.get("categories", [])

    section_lines: list[str] = []
    if previous_tag:
        section_lines.append(
            f"_Changes since [{previous_tag}]({repo_url}/releases/tag/{previous_tag})_"
        )
        section_lines.append("")

    for cfg in category_configs:
        title = cfg["title"]
        changes = by_category.get(title, [])
        if not changes:
            continue
        section_lines.append(f"### {title}")
        section_lines.append("")

        collapse_after = cfg.get("collapse-after")
        collapsed = collapse_after is not None and len(changes) > collapse_after
        if collapsed:
            section_lines.append("<details>")
            section_lines.append(f"<summary>{len(changes)} changes</summary>")
            section_lines.append("")

        section_lines.extend(render_change_line(change_template, c) for c in changes)

        if collapsed:
            section_lines.append("")
            section_lines.append("</details>")
        section_lines.append("")

    body = "\n".join(section_lines).rstrip()
    template = config.get("template") or "## What's changed\n\n$CHANGES\n"
    return template.replace("$CHANGES", body).rstrip() + "\n"


def categorise_backend_pr(
    pr_labels: set[str], category_configs: list[dict[str, Any]]
) -> str | None:
    """Return the first category whose labels overlap ``pr_labels``."""
    for cfg in category_configs:
        labels = cfg.get("labels") or []
        if isinstance(labels, str):
            labels = [labels]
        if pr_labels & set(labels):
            return cfg["title"]
    return None


def _extract_pr_numbers_from_commits(commits: list[Any]) -> set[int]:
    """Pull every ``#NNN`` reference out of a list of commit messages."""
    pr_re = re.compile(r"#(\d+)")
    merge_re = re.compile(r"Merge pull request #(\d+)")
    numbers: set[int] = set()
    for commit in commits:
        msg = commit.commit.message
        merge_match = merge_re.search(msg)
        if merge_match:
            numbers.add(int(merge_match.group(1)))
            continue
        numbers.update(int(m.group(1)) for m in pr_re.finditer(msg))
    return numbers


def _get_tag_date(repo: Repository, tag: str) -> Any | None:
    """Return the commit date that ``tag`` points to (for filtering)."""
    from github import GithubException

    try:
        ref = repo.get_git_ref(f"tags/{tag}")
        if ref.object.type == "tag":
            return repo.get_git_tag(ref.object.sha).tagger.date
        return repo.get_commit(ref.object.sha).commit.committer.date
    except GithubException:
        return None


def fetch_backend_prs(
    repo: Repository,
    *,
    previous_tag: str | None,
    commitish: str,
) -> list[Any]:
    """
    Fetch backend PRs merged since ``previous_tag``.

    Falls back to the last 100 commits on ``commitish`` when no previous
    tag exists (first-ever release). Mirrors release-drafter's strategy
    of pulling PR numbers out of commit messages instead of querying the
    PR list directly, since rebase-merged commits may not appear under
    the tag's compare ancestry exactly the way ``gh pr list`` orders them.
    """
    from github import GithubException

    if previous_tag:
        try:
            commits = list(repo.compare(previous_tag, commitish).commits)
        except GithubException as e:
            sys.exit(f"Error: cannot compare {previous_tag}..{commitish}: {e}")
    else:
        commits = list(repo.get_commits(sha=commitish)[:100])

    pr_numbers = _extract_pr_numbers_from_commits(commits)
    tag_date = _get_tag_date(repo, previous_tag) if previous_tag else None

    prs: list[Any] = []
    for num in sorted(pr_numbers):
        try:
            pr = repo.get_pull(num)
        except GithubException as e:
            print(f"Warning: cannot fetch PR #{num}: {e}", file=sys.stderr)
            continue
        if not pr.merged:
            continue
        # Squash-merged or referenced from a later commit but actually
        # shipped in the previous tag — drop it to avoid duplicating
        # the line across two consecutive releases.
        if tag_date and pr.merged_at and pr.merged_at <= tag_date:
            continue
        prs.append(pr)
    return prs


def _compute_skip_titles(category_configs: list[dict[str, Any]]) -> set[str]:
    """Categories whose labels mark them as 'machine churn' (deps / CI)."""
    skip: set[str] = set()
    for cfg in category_configs:
        labels = cfg.get("labels") or []
        if isinstance(labels, str):
            labels = [labels]
        if set(labels) & _FRONTEND_SKIP_LABELS:
            skip.add(cfg["title"])
    return skip


def _categorise_backend_prs(
    prs: list[Any],
    category_configs: list[dict[str, Any]],
) -> tuple[dict[str, list[dict[str, Any]]], list[int]]:
    """Bucket backend PRs by category. Bump-PR numbers are returned separately."""
    by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    skipped_bumps: list[int] = []

    for pr in prs:
        if _FRONTEND_BUMP_TITLE_RE.match(pr.title):
            skipped_bumps.append(pr.number)
            continue
        labels = {label.name for label in pr.labels}
        title = categorise_backend_pr(labels, category_configs)
        if title is None:
            continue
        by_category[title].append(
            {
                "title": pr.title,
                "author": pr.user.login,
                "number": pr.number,
                "url": pr.html_url,
                "is_frontend": False,
            }
        )

    return by_category, skipped_bumps


def _collect_frontend_changes(
    *,
    repo: Repository,
    commitish: str,
    previous_tag: str | None,
    frontend_repo_name: str,
    frontend_dep_name: str,
    category_titles: list[str],
    skip_titles: set[str],
    server_url: str,
) -> dict[str, list[dict[str, Any]]]:
    """Resolve the frontend version range and inline its release-notes bullets.

    The version range comes straight from ``pyproject.toml`` at the
    previous tag and the release commitish, so a backend bump that skips
    a frontend version (e.g. 0.1.20 -> 0.1.22 in one PR) still surfaces
    the intermediate 0.1.21 release notes.
    """
    from github import Github

    current_fv = parse_pyproject_frontend_version(
        get_pyproject_at(repo, commitish), frontend_dep_name
    )
    previous_fv = (
        parse_pyproject_frontend_version(get_pyproject_at(repo, previous_tag), frontend_dep_name)
        if previous_tag
        else None
    )

    by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if not current_fv or current_fv == previous_fv:
        return by_category

    print(
        f"Inlining frontend changes from {previous_fv or '(first release)'} -> {current_fv}",
        file=sys.stderr,
    )
    # The release.yml token is a GitHub App token scoped to the backend
    # repo; it can't read the frontend repo. Frontend releases are
    # public, so an anonymous client works — at the cost of the
    # 60/hour rate limit, which is plenty for a one-shot release run.
    try:
        frontend_repo = Github().get_repo(frontend_repo_name)
        releases = get_frontend_releases_in_range(
            frontend_repo, after=previous_fv, up_to=current_fv
        )
    except Exception as e:
        print(
            f"Warning: cannot fetch frontend releases ({e}); "
            "release notes will not include frontend changes.",
            file=sys.stderr,
        )
        return by_category

    for release in releases:
        if not release.body:
            continue
        cats = parse_frontend_release_body(
            release.body,
            category_titles=category_titles,
            skip_titles=skip_titles,
            frontend_repo_full_name=frontend_repo_name,
            server_url=server_url,
        )
        for title, items in cats.items():
            by_category[title].extend(items)
    return by_category


def _emit_outputs(notes: str) -> None:
    """Write the notes to a file and to ``GITHUB_OUTPUT`` (when present)."""
    runner_temp = os.environ.get("RUNNER_TEMP", "/tmp")
    notes_file = Path(runner_temp) / "release-notes.md"
    notes_file.write_text(notes, encoding="utf-8")

    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        sys.stdout.write(notes)
        return
    with open(output_path, "a", encoding="utf-8") as f:
        f.write(f"release-notes-file={notes_file}\n")
        # Multi-line outputs need the heredoc form. The delimiter has to
        # be a string we know won't appear in the rendered notes.
        f.write("release-notes<<RELEASENOTES_EOF\n")
        f.write(notes)
        if not notes.endswith("\n"):
            f.write("\n")
        f.write("RELEASENOTES_EOF\n")


def main() -> None:
    """Entry point for the GitHub Action."""
    from github import Github

    token = os.environ["GITHUB_TOKEN"]
    repo_name = os.environ["GITHUB_REPOSITORY"]
    server_url = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    previous_tag = os.environ.get("PREVIOUS_TAG", "").strip() or None
    commitish = os.environ["COMMITISH"]
    frontend_repo_name = os.environ["FRONTEND_REPO"]
    frontend_dep_name = os.environ["FRONTEND_DEP_NAME"]

    config = load_config()
    category_configs = config.get("categories", [])
    category_titles = [c["title"] for c in category_configs]
    skip_titles = _compute_skip_titles(category_configs)

    repo = Github(token).get_repo(repo_name)

    backend_prs = fetch_backend_prs(repo, previous_tag=previous_tag, commitish=commitish)
    print(f"Backend PRs in range: {len(backend_prs)}", file=sys.stderr)

    by_category, skipped_bumps = _categorise_backend_prs(backend_prs, category_configs)
    if skipped_bumps:
        print(
            f"Skipped {len(skipped_bumps)} frontend bump PR(s): "
            f"{', '.join(f'#{n}' for n in skipped_bumps)}",
            file=sys.stderr,
        )

    frontend_buckets = _collect_frontend_changes(
        repo=repo,
        commitish=commitish,
        previous_tag=previous_tag,
        frontend_repo_name=frontend_repo_name,
        frontend_dep_name=frontend_dep_name,
        category_titles=category_titles,
        skip_titles=skip_titles,
        server_url=server_url,
    )
    for title, items in frontend_buckets.items():
        by_category[title].extend(items)

    notes = build_release_notes(
        config=config,
        by_category=by_category,
        previous_tag=previous_tag,
        repo_url=f"{server_url}/{repo_name}",
    )
    _emit_outputs(notes)


if __name__ == "__main__":
    main()
