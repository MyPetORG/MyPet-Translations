#!/usr/bin/env python3
"""Checks a pull request against the branch pipeline rules.

Every name this check uses is data from the pipeline rules (see pipeline_rules.py): the three
pipeline branches (integration, pre-release and release), the change kinds (a branch
prefix or pattern, the branch it goes into, whether it is player facing, whether it is merged
back, and which checks it runs), the repositories, the commit types, the changelog layout and the
PR body headings. The script hard-codes only the shape of the pipeline:

    <change>  --PR (squash)-->  <integration>  --PR-->  <pre-release>  --PR-->  <release>
                                                                 merge-back change --PR--^

Into the integration branch, a PR must come from a change into that branch (or be the
pre-release branch, merged back). It then runs only the checks its change names:
  * `commits`: only `type(scope): subject` commits, with the rules' commit types;
  * `title`: the PR title is the changelog line, because the squash commit takes it as its
    subject. A non-player-facing change's title ends with the rules' skip marker, which keeps it
    out of changelogs; a player-facing title must not contain it;
  * `siblings`: the PR body has the rules' siblings section naming every other repository with
    the rules' not-needed value or the same branch name; a named sibling must have a PR (open or
    merged) from that branch, looked up through the GitHub API with `SIBLINGS_TOKEN` (falling
    back to `GITHUB_TOKEN`);
  * `changelog`: a player-facing change adds its line to the next release's notes.

Into the pre-release branch only the integration branch (promotion) or the release branch
(merge-back) may come; into the release branch only the pre-release branch (promotion) or a change
into the release branch, which follows the change-PR rules above.

A PR into a stack-base change branch is a layer of a GitHub stacked PR. It follows the change-PR
rules above, and its named siblings must have a PR from the same branch into the layer's base.

Standard library only, Python 3.8 compatible. Tested by test_pr_rules.py.
"""
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, List, NamedTuple, Optional, Sequence, Tuple

from pipeline_rules import Change, Rules, RulesError, load_rules

# (repository, branch) -> a problem with that sibling PR, or None when it exists.
SiblingProblem = Callable[[str, str], Optional[str]]


class Commit(NamedTuple):
    sha: str
    subject: str
    parents: int


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

_FULL_CHECKS = frozenset({"title", "commits", "siblings", "changelog"})


def fragment_name(branch: str) -> str:
    """The fragment file a change branch writes: `feature/multi-pet/phase-1` becomes
    `feature-multi-pet-phase-1.txt`."""
    return branch.replace("/", "-") + ".txt"


def parse_fragment(text: str, sections: Sequence[str]) -> Tuple[str, str]:
    """(section, change) from a fragment's two non-blank lines; ValueError says what is wrong."""
    lines = [line.strip() for line in text.replace("\r", "").split("\n") if line.strip()]
    if len(lines) != 2:
        raise ValueError(f"a fragment is two lines, the section and then the change; this one has "
                         f"{len(lines)}")
    section, change = lines
    if section not in sections:
        raise ValueError(f"the first line must be one of {', '.join(sections)}, not `{section}`")
    return section, change


def _either(names: Sequence[str]) -> str:
    """`a`, `b` or `c`."""
    quoted = [f"`{n}`" for n in names]
    if len(quoted) <= 1:
        return "".join(quoted)
    return ", ".join(quoted[:-1]) + " or " + quoted[-1]


def _label(change: Change) -> str:
    return change.prefix if change.prefix is not None else (change.pattern or "")


def _change_prefixes(rules: Rules) -> List[str]:
    """The prefixes a person opens a change branch with: changes into the integration branch with a
    category and every check."""
    return [c.prefix for c in rules.changes
            if c.prefix is not None and c.into == rules.staging and c.category is not None
            and c.checks >= _FULL_CHECKS]


def _prefix_error(head: str, rules: Rules) -> List[str]:
    prefixes = _change_prefixes(rules)
    example = (prefixes[0] if prefixes else "") + "pet-drowning"
    return [f"The branch `{head}` must start with {_either(prefixes)} "
            f"(lowercase, e.g. `{example}`)."]


def check(base: str, head: str, title: str, body: str, commits: List[Commit],
          repo: str, rules: Rules, sibling_problem: Optional[SiblingProblem] = None,
          changelog: Changelog = None, labels: Optional[List[str]] = None) -> List[str]:
    """Every rule the PR breaks, as human-readable lines; empty when it passes.

    `sibling_problem` checks that a named sibling branch has its PR; None skips that lookup.
    `changelog` enables the changelog rule (repos with a version script); None skips it.
    `labels` are the PR's labels; None skips the risk-label rule, which applies only in the
    rules' primary repository.
    """
    stack_layer = base not in (rules.staging, rules.alpha, rules.release) and \
        rules.is_stack_base(base)
    change = rules.change_for(head)
    if change is not None and (base in (rules.staging, rules.release) or stack_layer) \
            and change.into not in (rules.staging, rules.alpha, rules.release):
        return [f"Pipeline rules: change `{_label(change)}` targets `{change.into}`, which is "
                f"not a pipeline branch."]
    if base == rules.alpha:
        return [] if head in (rules.staging, rules.release) else [
            f"Only `{rules.staging}` (promotion) or `{rules.release}` (merge-back) may be merged "
            f"into `{rules.alpha}`, not `{head}`."]
    if base == rules.release:
        if head == rules.alpha:
            return []
        if change is None or change.into != rules.release:
            merge_back = [_label(c) for c in rules.changes if c.merge_back]
            return [f"Only `{rules.alpha}` (promotion) or a {_either(merge_back)} branch may be "
                    f"merged into `{rules.release}`, not `{head}`."]
        return change_rules(change, head, title, body, commits, repo, rules, sibling_problem,
                            changelog)
    if base == rules.staging:
        if head == rules.alpha:
            return []
        if change is None:
            # A change into another branch keeps its specific advice even when the rest of
            # the name is malformed (an uppercase tail, say).
            change = next((c for c in rules.changes if c.prefix is not None
                           and c.into != rules.staging and head.startswith(c.prefix)), None)
            if change is None:
                return _prefix_error(head, rules)
        if change.into != rules.staging:
            return [f"`{_label(change)}` branches go into `{change.into}`, not `{rules.staging}`."]
        return change_rules(change, head, title, body, commits, repo, rules, sibling_problem,
                            changelog) + label_rules(base, labels, rules, repo)
    if stack_layer:
        if change is None or not rules.is_stack_base(head):
            return _prefix_error(head, rules)
        return change_rules(change, head, title, body, commits, repo, rules, sibling_problem,
                            changelog)
    return []


def _choices(names: Sequence[str]) -> str:
    """'a', 'a or b' or 'a, b or c'."""
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " or " + names[-1]


def label_rules(base: str, labels: Optional[List[str]], rules: Rules, repo: str = "") -> List[str]:
    """Exactly one risk label on a primary-repository PR into staging.

    Stack layers, an empty map and every other repository are exempt: only the primary repository
    carries the risk labels (its siblings' Dependabot and Crowdin PRs have none).
    """
    if labels is None or base != rules.staging or not rules.risk_labels:
        return []
    if not any(r.primary and r.repository == repo for r in rules.repos):
        return []
    configured = list(rules.risk_labels.values())
    found = [lb for lb in labels if lb in configured]
    if len(found) == 1:
        return []
    return [f"Add exactly one of the labels {_choices(configured)} "
            f"(found: {', '.join(found) if found else 'none'})."]


def change_rules(change: Change, head: str, title: str, body: str, commits: List[Commit],
                 repo: str, rules: Rules, sibling_problem: Optional[SiblingProblem],
                 changelog: Changelog = None) -> List[str]:
    """The checks the change names, in a fixed order."""
    errors = []  # type: List[str]
    if "commits" in change.checks:
        errors += commit_rules(commits, rules)
    if "title" in change.checks:
        errors += title_rules(change, title, rules)
    if "siblings" in change.checks:
        errors += sibling_rules(head, body, repo, rules, sibling_problem)
    if "changelog" in change.checks:
        errors += changelog_rules(change, changelog, rules, head, title)
    return errors


def changelog_rules(change: Change, changelog: Changelog, rules: Rules, head: str = "",
                    title: str = "") -> List[str]:
    """A player-facing change must add its line to the next release's notes.

    The release notes are written in the PRs that make the changes, so a release never ships notes
    written before its newest fix (and nothing has to write them afterwards). Chores are exempt:
    they never appear in a changelog. In the directory form each PR writes its own fragment, so two
    PRs for one version never touch the same file and never conflict.
    """
    if changelog is None or not change.player_facing or "changelog" not in change.checks:
        return []
    ctx = changelog if isinstance(changelog, ChangelogContext) else ChangelogContext(*changelog)
    if not ctx.directory:
        path = f"{rules.changelog_dir}/{ctx.version}{rules.changelog_extension}"
        if path in ctx.changed:
            return []
        return [f"This PR must add its change to `{path}` (the next release's changelog; create "
                f"it from the previous release's file if it doesn't exist yet). Chores are "
                f"exempt."]
    path = f"{rules.changelog_dir}/{ctx.version}/{fragment_name(head)}"
    if path not in ctx.changed:
        return [f"This PR must add `{path}`: two lines, the section ({', '.join(rules.sections)}) "
                f"and then this PR's title. Chores are exempt."]
    text = ctx.read(path)
    if text is None:
        return [f"`{path}` changed but could not be read at the PR's head; the PR must add it, "
                f"not delete it."]
    try:
        _, line = parse_fragment(text, rules.sections)
    except ValueError as problem:
        return [f"`{path}`: {problem}."]
    if line != title.strip():
        return [f"`{path}` says `{line}`, but the PR title is `{title.strip()}`. They must "
                f"match: the title becomes the squash commit and the fragment becomes the "
                f"release note."]
    return []


def commit_rules(commits: List[Commit], rules: Rules) -> List[str]:
    conventional = rules.conventional()
    exempt = rules.exempt_commit() if rules.exempt_subjects else None
    return [f"Commit {c.sha[:7]} `{c.subject}` is not `type(scope): subject` "
            f"(types: {', '.join(rules.commit_types)})."
            for c in commits
            if c.parents < 2 and not conventional.match(c.subject)
            and not (exempt is not None and exempt.match(c.subject))]


def title_rules(change: Change, title: str, rules: Rules) -> List[str]:
    title = title.strip()
    if not title:
        return ["The PR title is empty."]
    errors = []
    if rules.conventional().match(title):
        errors.append("The PR title must be a plain sentence — the player-facing change for "
                      "fixes and features (e.g. \"Fixed pets drowning in rain\") — not "
                      "`type(scope): ...`. The branch's commits carry that format; the title is "
                      "what the squash commit and the changelog show.")
    elif not title[0].isupper():
        errors.append("The PR title must start with a capital letter.")
    marker = rules.skip_marker
    if not change.player_facing:
        if not title.endswith(marker):
            errors.append(f"A `{_label(change)}` PR title must end with `{marker}` so it stays "
                          f"out of changelogs and never triggers `{rules.alpha}` or a release by "
                          f"itself.")
    elif marker and marker in title:
        quiet = [_label(c) for c in rules.changes if not c.player_facing]
        errors.append(f"Only {_either(quiet)} PRs may use `{marker}`; this title must be the "
                      f"player-facing changelog line.")
    return errors


def sibling_rules(head: str, body: str, repo: str, rules: Rules,
                  sibling_problem: Optional[SiblingProblem] = None) -> List[str]:
    heading = rules.siblings_heading
    others = [(r, n) for r, n in rules.repo_names().items() if r != repo]
    lines = (body or "").splitlines()
    pattern = r"[#*\s]*" + re.escape(heading) + r":?[*\s]*"
    start = next((i for i, line in enumerate(lines)
                  if re.fullmatch(pattern, line.strip(), re.IGNORECASE)), None)
    if start is None:
        example = others[0][1] if others else "Name"
        return [f"The PR description needs a **{heading}** section with one line per other "
                f"repository: `- {example}: {rules.not_needed}` or `- {example}: {head}` (see the "
                f"PR template)."]
    found = {}
    for line in lines[start + 1:]:
        m = re.match(r"\s*[-*]\s*([A-Za-z][A-Za-z0-9-]*)\s*:\s*(.+?)\s*$", line)
        if m:
            found[m.group(1).lower()] = m.group(2).strip("` ")
        elif line.strip().startswith("#"):
            break
    errors = []
    for sibling, name in others:
        value = found.get(name.lower())
        if value is None:
            errors.append(f"The {heading} section has no line for {name}.")
        elif value.lower() == rules.not_needed.lower():
            continue
        elif value != head:
            errors.append(f"{heading}: {name} is `{value}`; it must be `{rules.not_needed}` or "
                          f"the same branch name, `{head}`.")
        elif sibling_problem:
            problem = sibling_problem(sibling, head)
            if problem:
                errors.append(problem)
    return errors


def sibling_pr_problem(sibling: str, branch: str,
                       get: Callable[[str], Tuple[int, object]],
                       base: str, heading: Optional[str] = None,
                       not_needed: Optional[str] = None) -> Optional[str]:
    """Why `sibling` has no PR from `branch` into `base` (open or merged), or None if it has.

    `base` is the integration branch for an ordinary change PR, and the layer's own base for a stack
    layer: a sibling of a stacked change is stacked too, on the sibling branch of the same name.
    `heading` and `not_needed` are the rules' siblings heading and not-needed value, used in the
    advice; without them the advice is neutral.
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
        where = f"{heading}: " if heading else ""
        fix = (f"change that line to `{not_needed}`" if not_needed
               else "mark it as not needed in the linked-repositories section")
        return (f"{where}{sibling} has no PR from `{branch}` into `{base}`. Open it, or "
                f"{fix}.")
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


def changelog_context(base_sha: str, head_sha: str, rules: Rules) -> Changelog:
    """The next version (computed on the checked-out PR merge, tags included), the PR's files,
    whether that version is a directory at the PR's head, and a reader for files at the head.

    None in repos without a version script: only a repository that versions its releases this way
    carries one.
    """
    if not os.path.exists(VERSION_SCRIPT):
        return None
    result = subprocess.run([sys.executable, VERSION_SCRIPT, "next"], capture_output=True, text=True)
    if result.returncode != 0 or not result.stdout.strip():
        stderr = result.stderr.strip() or "version.py printed nothing"
        if result.returncode == 3:
            advice = ("one version has both a changelog file and a changelog directory — "
                      "remove one of them.")
        else:
            advice = "the checkout needs full history with tags (fetch-depth: 0)."
        raise RuntimeError("Could not compute the next version for the changelog rule: "
                           + stderr + " — " + advice)
    version = result.stdout.strip()
    changed = subprocess.run(["git", "diff", "--name-only", f"{base_sha}...{head_sha}"],
                             check=True, capture_output=True, text=True).stdout.split()
    # Check the directory-vs-file form at HEAD (the checked-out PR merge commit), the same
    # ref the version script just read — not head_sha (the PR branch tip). A branch opened before
    # the fragments migration landed on its base has no directory at its own tip, but merging
    # it lands on a HEAD that does; checking head_sha would wrongly let it resurrect the
    # single-file form instead of being told to write a fragment.
    entry = subprocess.run(["git", "ls-tree", "HEAD", "--", f"{rules.changelog_dir}/{version}"],
                           check=True, capture_output=True, text=True).stdout
    directory = " tree " in entry

    def read(path: str) -> Optional[str]:
        shown = subprocess.run(["git", "show", f"{head_sha}:{path}"], capture_output=True,
                               text=True)
        return shown.stdout if shown.returncode == 0 else None

    return ChangelogContext(version, changed, directory, read)


def main() -> int:
    env = os.environ
    try:
        rules = load_rules(env)
    except RulesError as e:
        print(f"::error::{e}")
        return 1
    get = github_get(token_from_env(env))
    try:
        changelog = changelog_context(env["PR_BASE_SHA"], env["PR_HEAD_SHA"], rules)
    except RuntimeError as problem:
        print(f"::error::{problem}")
        return 1
    base = env["PR_BASE"]
    sibling_base = base if rules.is_stack_base(base) else rules.staging
    errors = check(base, env["PR_HEAD"], env.get("PR_TITLE", ""),
                   env.get("PR_BODY", ""),
                   commits_between(env["PR_BASE_SHA"], env["PR_HEAD_SHA"]),
                   env.get("GITHUB_REPOSITORY", ""),
                   rules,
                   lambda sibling, branch: sibling_pr_problem(
                       sibling, branch, get, base=sibling_base,
                       heading=rules.siblings_heading, not_needed=rules.not_needed),
                   changelog,
                   json.loads(env.get("PR_LABELS") or "[]"))
    for error in errors:
        print(f"::error::{error}")
    if not errors:
        print("PR follows the branch pipeline rules.")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
