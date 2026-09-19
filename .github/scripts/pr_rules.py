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


def check(base: str, head: str, title: str, body: str, commits: List[Commit],
          repo: str, sibling_problem: Optional[SiblingProblem] = None) -> List[str]:
    """Every rule the PR breaks, as human-readable lines; empty when it passes.

    `sibling_problem` checks that a named sibling branch has its PR; None skips that lookup.
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
        return change_rules("hotfix", head, title, body, commits, repo, sibling_problem)
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
        return change_rules(match.group(1), head, title, body, commits, repo, sibling_problem)
    return []


def change_rules(kind: str, head: str, title: str, body: str, commits: List[Commit],
                 repo: str, sibling_problem: Optional[SiblingProblem]) -> List[str]:
    return (commit_rules(commits) + title_rules(kind, title)
            + sibling_rules(head, body, repo, sibling_problem))


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
                       get: Callable[[str], Tuple[int, object]]) -> Optional[str]:
    """Why `sibling` has no PR from `branch` into `staging` (open or merged), or None if it has."""
    owner = sibling.split("/")[0]
    url = (f"https://api.github.com/repos/{sibling}/pulls?"
           + urllib.parse.urlencode({"head": f"{owner}:{branch}", "base": "staging",
                                     "state": "all"}))
    status, pulls = get(url)
    if status in (401, 403, 404):
        return (f"Could not look up PRs in {sibling} (HTTP {status}). The `SIBLINGS_TOKEN` "
                f"secret must hold a token that can read that repository.")
    if status != 200:
        return f"Could not look up PRs in {sibling} (HTTP {status or 'error'}); re-run the check."
    if not pulls:
        return (f"Siblings: {sibling} has no PR from `{branch}` into `staging`. Open it, or "
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


def main() -> int:
    env = os.environ
    get = github_get(token_from_env(env))
    errors = check(env["PR_BASE"], env["PR_HEAD"], env.get("PR_TITLE", ""),
                   env.get("PR_BODY", ""),
                   commits_between(env["PR_BASE_SHA"], env["PR_HEAD_SHA"]),
                   env.get("GITHUB_REPOSITORY", ""),
                   lambda sibling, branch: sibling_pr_problem(sibling, branch, get))
    for error in errors:
        print(f"::error::{error}")
    if not errors:
        print("PR follows the branch pipeline rules.")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
