#!/usr/bin/env python3
"""Checks a pull request against the MyPet branch pipeline rules.

The model (same in every MyPet repo):

    fix/* feature/* chore/*  --PR (squash)-->  staging  --PR-->  alpha  --PR-->  main
                                                                   hotfix/* --PR--^

Into `staging`, a change PR must:
  * come from a `fix/`, `feature/` or `chore/` branch (Dependabot's `dependabot/` and Seer's `seer/`
    branches are accepted too, and Crowdin's `i18n_`/`l10n_` service branches as they are;
    `alpha` is accepted as a hotfix merge-back);
  * contain only `type(scope): subject` commits — the scope is optional;
  * have a title that is the player-facing changelog line, because the squash commit takes the PR
    title as its subject. `chore/` titles are a plain sentence ending in `[skip ci]`, which keeps
    them out of changelogs and stops them triggering an alpha or release by themselves;
  * carry a "Siblings" section naming every other MyPet repo as `not needed` or the same branch name;
    a named sibling must have a PR (open or merged) from that branch into its own `staging`, looked
    up through the GitHub API with `SIBLINGS_TOKEN` (falling back to `GITHUB_TOKEN`, which cannot
    read the private repos).

Into `alpha` only `staging` (promotion) or `main` (merge-back) may come; into `main` only `alpha`
(promotion) or a `hotfix/` branch, which follows the change-PR rules above.

A PR into another `fix/`, `feature/` or `chore/` branch is a layer of a GitHub stacked PR. It
follows the change-PR rules above, and its named siblings must have a PR from the same branch into
the layer's base rather than into `staging`.

Run in CI by .github/workflows/pr-rules.yml; tested by test_pr_rules.py. The same file is copied
into every MyPet repo; keep the copies identical.
"""
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, List, NamedTuple, Optional, Tuple

COMMIT_TYPES = "feat|fix|chore|docs|test|refactor|perf|build|ci|style|revert"
CONVENTIONAL = re.compile(r"^(%s)(\([^)]+\))?!?: \S" % COMMIT_TYPES)
EXEMPT_COMMIT = re.compile(r'^(Revert "|fixup! |squash! |amend! )')
BRANCH_NAME = r"[a-z0-9][a-z0-9._/-]*"
SKIP_CI = "[skip ci]"
NON_PLAYER_FACING_SUBJECT = re.compile(r"^(chore|build|ci|test|docs)(\([^)]+\))?!?: ")
CHANGE_BRANCH = re.compile(r"(fix|feature|chore)/" + BRANCH_NAME)

# The display names used in the Siblings section, keyed by GitHub repository.
REPOS = {
    "MyPetORG/MyPet4": "Plugin",
    "MyPetORG/MyPet-Configurator": "Configurator",
    "MyPetORG/MyPet-Wiki": "Wiki",
    "MyPetORG/MyPet-Translations": "Translations",
    "MyPetORG/MyPet-Editor-Translations": "Editor-Translations",
}


# (repository, branch) -> a problem with that sibling PR, or None when it exists.
SiblingProblem = Callable[[str, str], Optional[str]]


class Commit(NamedTuple):
    sha: str
    subject: str
    parents: int


def is_player_facing(subject: str) -> bool:
    """Whether a commit subject on staging/alpha/main belongs in a public changelog.

    `[skip ci]` anywhere marks a chore (GitHub appends ` (#123)` to squash subjects, so "ends
    with" would never match on the branch). Dependabot's `chore(deps): ...` titles can't carry the
    marker — a `[skip ci]` in its commit would also skip the PR's own required checks — so a
    conventional non-player-facing type counts as a chore too.
    """
    return SKIP_CI not in subject and not NON_PLAYER_FACING_SUBJECT.match(subject)


def is_stack_layer(base: str) -> bool:
    """Whether a PR into `base` is an upper layer of a GitHub stacked PR.

    A stack's bottom PR targets staging; every layer above it targets the branch below. Each
    layer is later squash-merged into staging under its own title, so a layer is held to exactly
    the rules a PR into staging is — otherwise its title, commits and notes would land unchecked.
    """
    return bool(CHANGE_BRANCH.fullmatch(base))


CHANGELOG_DIR = ".github/changelogs"
SECTIONS = ("Added", "Changed", "Fixed", "Removed")


class ChangelogContext(NamedTuple):
    """The next release version, the files the PR changes, whether that version's notes are a
    directory of fragments, and a reader for a file at the PR's head (None when absent)."""
    version: str
    changed: List[str]
    directory: bool = False
    read: Callable[[str], Optional[str]] = lambda path: None


# None where the repo computes no versions. A plain (version, changed) tuple is the single-file
# form and is still accepted.
Changelog = Optional[object]


def fragment_name(branch: str) -> str:
    """The fragment file a change branch writes: `feature/multi-pet/phase-1` becomes
    `feature-multi-pet-phase-1.txt`."""
    return branch.replace("/", "-") + ".txt"


def parse_fragment(text: str) -> Tuple[str, str]:
    """(section, change) from a fragment's two non-blank lines; ValueError says what is wrong."""
    lines = [line.strip() for line in text.replace("\r", "").split("\n") if line.strip()]
    if len(lines) != 2:
        raise ValueError(f"a fragment is two lines, the section and then the change; this one has "
                         f"{len(lines)}")
    section, change = lines
    if section not in SECTIONS:
        raise ValueError(f"the first line must be one of {', '.join(SECTIONS)}, not `{section}`")
    return section, change


def check(base: str, head: str, title: str, body: str, commits: List[Commit],
          repo: str, sibling_problem: Optional[SiblingProblem] = None,
          changelog: Changelog = None) -> List[str]:
    """Every rule the PR breaks, as human-readable lines; empty when it passes.

    `sibling_problem` checks that a named sibling branch has its PR; None skips that lookup.
    `changelog` enables the changelog rule (repos with .github/scripts/version.py); None skips it.
    """
    if base == "alpha":
        return [] if head in ("staging", "main") else [
            f"Only `staging` (promotion) or `main` (merge-back) may be merged into `alpha`, "
            f"not `{head}`."]
    if base == "main":
        if head == "alpha":
            return []
        if not re.fullmatch(r"hotfix/" + BRANCH_NAME, head):
            return [f"Only `alpha` (promotion) or a `hotfix/` branch may be merged into `main`, "
                    f"not `{head}`."]
        return change_rules("hotfix", head, title, body, commits, repo, sibling_problem,
                            changelog)
    if base == "staging":
        if head == "alpha":
            return []
        if head.startswith("dependabot/"):
            return commit_rules(commits)
        if re.match(r"(i18n|l10n)_", head):
            return []
        if head.startswith("hotfix/"):
            return ["`hotfix/` branches go into `main`, not `staging`."]
        match = re.fullmatch(r"(fix|feature|chore|seer)/" + BRANCH_NAME, head)
        if not match:
            return [f"The branch `{head}` must start with `fix/`, `feature/` or `chore/` "
                    f"(lowercase, e.g. `fix/pet-drowning`)."]
        return change_rules(match.group(1), head, title, body, commits, repo, sibling_problem,
                            changelog)
    if is_stack_layer(base):
        match = CHANGE_BRANCH.fullmatch(head)
        if not match:
            return [f"The branch `{head}` must start with `fix/`, `feature/` or `chore/` "
                    f"(lowercase, e.g. `fix/pet-drowning`)."]
        return change_rules(match.group(1), head, title, body, commits, repo, sibling_problem,
                            changelog)
    return []


def change_rules(kind: str, head: str, title: str, body: str, commits: List[Commit],
                 repo: str, sibling_problem: Optional[SiblingProblem],
                 changelog: Changelog = None) -> List[str]:
    return (commit_rules(commits) + title_rules(kind, title)
            + sibling_rules(head, body, repo, sibling_problem)
            + changelog_rules(kind, changelog, head, title))


def changelog_rules(kind: str, changelog: Changelog, head: str = "",
                    title: str = "") -> List[str]:
    """A player-facing change must add its line to the next release's notes.

    The release notes are written in the PRs that make the changes, so a release never ships notes
    written before its newest fix (and nothing has to write them afterwards). Chores are exempt:
    they never appear in a changelog. In the directory form each PR writes its own fragment, so two
    PRs for one version never touch the same file and never conflict.
    """
    if changelog is None or kind == "chore":
        return []
    ctx = changelog if isinstance(changelog, ChangelogContext) else ChangelogContext(*changelog)
    if not ctx.directory:
        path = f"{CHANGELOG_DIR}/{ctx.version}.bbcode"
        if path in ctx.changed:
            return []
        return [f"This PR must add its change to `{path}` (the next release's changelog; create "
                f"it from the previous release's file if it doesn't exist yet). Chores are "
                f"exempt."]
    path = f"{CHANGELOG_DIR}/{ctx.version}/{fragment_name(head)}"
    if path not in ctx.changed:
        return [f"This PR must add `{path}`: two lines, the section ({', '.join(SECTIONS)}) and "
                f"then this PR's title. Chores are exempt."]
    text = ctx.read(path)
    if text is None:
        return [f"`{path}` changed but could not be read at the PR's head; the PR must add it, "
                f"not delete it."]
    try:
        _, change = parse_fragment(text)
    except ValueError as problem:
        return [f"`{path}`: {problem}."]
    if change != title.strip():
        return [f"`{path}` says `{change}`, but the PR title is `{title.strip()}`. They must "
                f"match: the title becomes the squash commit and the fragment becomes the "
                f"release note."]
    return []


def commit_rules(commits: List[Commit]) -> List[str]:
    return [f"Commit {c.sha[:7]} `{c.subject}` is not `type(scope): subject` "
            f"(types: {COMMIT_TYPES.replace('|', ', ')})."
            for c in commits
            if c.parents < 2 and not CONVENTIONAL.match(c.subject)
            and not EXEMPT_COMMIT.match(c.subject)]


def title_rules(kind: str, title: str) -> List[str]:
    title = title.strip()
    if not title:
        return ["The PR title is empty."]
    errors = []
    if CONVENTIONAL.match(title):
        errors.append("The PR title must be a plain sentence — the player-facing change for "
                      "fixes and features (e.g. \"Fixed pets drowning in rain\") — not "
                      "`type(scope): ...`. The branch's commits carry that format; the title is "
                      "what the squash commit and the changelog show.")
    elif not title[0].isupper():
        errors.append("The PR title must start with a capital letter.")
    if kind == "chore":
        if not title.endswith(SKIP_CI):
            errors.append("A `chore/` PR title must end with `[skip ci]` so it stays out of "
                          "changelogs and never triggers an alpha or release by itself.")
    elif SKIP_CI in title:
        errors.append("Only `chore/` PRs may use `[skip ci]`; this title must be the "
                      "player-facing changelog line.")
    return errors


def sibling_rules(head: str, body: str, repo: str,
                  sibling_problem: Optional[SiblingProblem] = None) -> List[str]:
    lines = (body or "").splitlines()
    start = next((i for i, line in enumerate(lines)
                  if re.fullmatch(r"[#*\s]*siblings:?[*\s]*", line.strip(), re.IGNORECASE)), None)
    if start is None:
        return ["The PR description needs a **Siblings** section with one line per other MyPet "
                "repo: `- Wiki: not needed` or `- Wiki: " + head + "` (see the PR template)."]
    found = {}
    for line in lines[start + 1:]:
        m = re.match(r"\s*[-*]\s*([A-Za-z-]+)\s*:\s*(.+?)\s*$", line)
        if m:
            found[m.group(1).lower()] = m.group(2).strip("` ")
        elif line.strip().startswith("#"):
            break
    errors = []
    for sibling, name in [(r, n) for r, n in REPOS.items() if r != repo]:
        value = found.get(name.lower())
        if value is None:
            errors.append(f"The Siblings section has no line for {name}.")
        elif value.lower() == "not needed":
            continue
        elif value != head:
            errors.append(f"Siblings: {name} is `{value}`; it must be `not needed` or the same "
                          f"branch name, `{head}`.")
        elif sibling_problem:
            problem = sibling_problem(sibling, head)
            if problem:
                errors.append(problem)
    return errors


def sibling_pr_problem(sibling: str, branch: str,
                       get: Callable[[str], Tuple[int, object]],
                       base: str = "staging") -> Optional[str]:
    """Why `sibling` has no PR from `branch` into `base` (open or merged), or None if it has.

    `base` is staging for an ordinary change PR, and the layer's own base for a stack layer: a
    sibling of a stacked change is stacked too, on the sibling branch of the same name.
    """
    owner = sibling.split("/")[0]
    url = (f"https://api.github.com/repos/{sibling}/pulls?"
           + urllib.parse.urlencode({"head": f"{owner}:{branch}", "base": base,
                                     "state": "all"}))
    status, pulls = get(url)
    if status in (401, 403, 404):
        return (f"Could not look up PRs in {sibling} (HTTP {status}). The `SIBLINGS_TOKEN` "
                f"secret must hold a token that can read that repository.")
    if status != 200:
        return f"Could not look up PRs in {sibling} (HTTP {status or 'error'}); re-run the check."
    if not pulls:
        return (f"Siblings: {sibling} has no PR from `{branch}` into `{base}`. Open it, or "
                f"change that line to `not needed`.")
    return None


def token_from_env(env) -> str:
    return env.get("SIBLINGS_TOKEN") or env.get("GITHUB_TOKEN") or ""


def github_get(token: str) -> Callable[[str], Tuple[int, object]]:
    """A GET against the GitHub API returning (status, parsed JSON); status 0 on network failure."""
    def get(url: str) -> Tuple[int, object]:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers),
                                        timeout=30) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as e:
            return e.code, None
        except (urllib.error.URLError, OSError, ValueError):
            return 0, None
    return get


def commits_between(base_sha: str, head_sha: str) -> List[Commit]:
    out = subprocess.run(["git", "log", "--format=%H%x09%P%x09%s", f"{base_sha}..{head_sha}"],
                         check=True, capture_output=True, text=True).stdout
    commits = []
    for line in out.splitlines():
        sha, parents, subject = line.split("\t", 2)
        commits.append(Commit(sha, subject, len(parents.split())))
    return commits


VERSION_SCRIPT = ".github/scripts/version.py"


def changelog_context(base_sha: str, head_sha: str) -> Changelog:
    """The next version (computed on the checked-out PR merge, tags included), the PR's files,
    whether that version is a directory at the PR's head, and a reader for files at the head.

    None in repos without a version script — only MyPet4 versions its releases this way.
    """
    if not os.path.exists(VERSION_SCRIPT):
        return None
    result = subprocess.run([sys.executable, VERSION_SCRIPT, "next"], capture_output=True, text=True)
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError("Could not compute the next version for the changelog rule: "
                           + (result.stderr.strip() or "version.py printed nothing") + " — the "
                           "checkout needs full history with tags (fetch-depth: 0).")
    version = result.stdout.strip()
    changed = subprocess.run(["git", "diff", "--name-only", f"{base_sha}...{head_sha}"],
                             check=True, capture_output=True, text=True).stdout.split()
    entry = subprocess.run(["git", "ls-tree", head_sha, "--", f"{CHANGELOG_DIR}/{version}"],
                           check=True, capture_output=True, text=True).stdout
    directory = " tree " in entry

    def read(path: str) -> Optional[str]:
        shown = subprocess.run(["git", "show", f"{head_sha}:{path}"], capture_output=True,
                               text=True)
        return shown.stdout if shown.returncode == 0 else None

    return ChangelogContext(version, changed, directory, read)


def main() -> int:
    env = os.environ
    get = github_get(token_from_env(env))
    try:
        changelog = changelog_context(env["PR_BASE_SHA"], env["PR_HEAD_SHA"])
    except RuntimeError as problem:
        print(f"::error::{problem}")
        return 1
    errors = check(env["PR_BASE"], env["PR_HEAD"], env.get("PR_TITLE", ""),
                   env.get("PR_BODY", ""),
                   commits_between(env["PR_BASE_SHA"], env["PR_HEAD_SHA"]),
                   env.get("GITHUB_REPOSITORY", ""),
                   lambda sibling, branch: sibling_pr_problem(
                       sibling, branch, get,
                       base=env["PR_BASE"] if is_stack_layer(env["PR_BASE"]) else "staging"),
                   changelog)
    for error in errors:
        print(f"::error::{error}")
    if not errors:
        print("PR follows the branch pipeline rules.")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
