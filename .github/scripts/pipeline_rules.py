"""The pipeline rules shared by the CI scripts.

The rules (branch kinds, repositories, commit types, changelog layout, ...) come
from one of three sources, tried in this order by load_rules():

1. PIPELINE_RULES_FILE: a JSON file on disk (tests, local runs).
2. HUB_RULES_URL: the Hub's GET /api/v1/pipeline/rules, e.g.
   https://hub.mypet-plugin.com/api/v1/pipeline/rules
3. A caller-supplied fallback, normally DEFAULTS. DEFAULTS exists only for the
   offline scripts that must still work when the Hub is unreachable; it is a
   literal copy of the endpoint's output and is never read from a file.

A configured source that fails is an error; it never silently falls back.
Standard library only, Python 3.8 compatible.
"""
import http.client
import json
import os
import re
import urllib.error
import urllib.request
from typing import Any, Dict, FrozenSet, List, Mapping, NamedTuple, Optional, Pattern, Tuple

# A Hub rule, not a product fact: what GitHub and the Hub accept as a branch.
BRANCH_NAME = r"[a-z0-9][a-z0-9._/-]*"

DEFAULTS = {
    "repos": [
        {"name": "Plugin", "repository": "MyPetORG/MyPet4", "primary": True, "deploy": None},
        {"name": "Configurator", "repository": "MyPetORG/MyPet-Configurator", "primary": False,
         "deploy": {"workflow": "deploy-editor.yml", "ref": "alpha"}},
        {"name": "Wiki", "repository": "MyPetORG/MyPet-Wiki", "primary": False, "deploy": None},
        {"name": "Translations", "repository": "MyPetORG/MyPet-Translations", "primary": False, "deploy": None},
        {"name": "Editor-Translations", "repository": "MyPetORG/MyPet-Editor-Translations",
         "primary": False, "deploy": None},
    ],
    "branches": {"staging": "staging", "alpha": "alpha", "release": "main"},
    "changes": [
        {"prefix": "fix/", "pattern": None, "into": "staging", "category": "fix", "playerFacing": True,
         "mergeBack": False, "checks": ["title", "commits", "siblings", "changelog"]},
        {"prefix": "feature/", "pattern": None, "into": "staging", "category": "feature", "playerFacing": True,
         "mergeBack": False, "checks": ["title", "commits", "siblings", "changelog"]},
        {"prefix": "chore/", "pattern": None, "into": "staging", "category": "chore", "playerFacing": False,
         "mergeBack": False, "checks": ["title", "commits", "siblings", "changelog"]},
        {"prefix": "hotfix/", "pattern": None, "into": "main", "category": None, "playerFacing": True,
         "mergeBack": True, "checks": ["title", "commits", "siblings", "changelog"]},
        {"prefix": "dependabot/", "pattern": None, "into": "staging", "category": None, "playerFacing": True,
         "mergeBack": False, "checks": ["commits"]},
        {"prefix": "seer/", "pattern": None, "into": "staging", "category": None, "playerFacing": True,
         "mergeBack": False, "checks": ["title", "commits", "siblings", "changelog"]},
        {"prefix": None, "pattern": "(i18n|l10n)_.*", "into": "staging", "category": None, "playerFacing": True,
         "mergeBack": False, "checks": []},
    ],
    "commits": {
        "types": ["feat", "fix", "chore", "docs", "test", "refactor", "perf", "build", "ci", "style", "revert"],
        "exemptSubjects": ["^Revert \"", "^fixup! ", "^squash! ", "^amend! "],
        "nonPlayerFacingTypes": ["chore", "build", "ci", "test", "docs"],
    },
    "labels": {"risk": {"high": "risk:high", "medium": "risk:medium", "low": "risk:low"}},
    "changelog": {"directory": ".github/changelogs", "extension": ".bbcode", "preamble": "_preamble.bbcode",
                  "heading": "Full changelog:", "skipMarker": "[skip ci]",
                  "sections": ["Added", "Changed", "Fixed", "Removed"]},
    "prBody": {"siblingsHeading": "Siblings", "notNeeded": "not needed"},
    "alphaSuffix": "-alpha-",
    "statusContext": "hub/preflight",
}


class RulesError(Exception):
    """The rules could not be loaded or are malformed."""


_FULL_CHECKS = frozenset({"title", "commits", "siblings", "changelog"})


class Change(NamedTuple):
    prefix: Optional[str]
    pattern: Optional[str]
    into: str
    category: Optional[str]
    player_facing: bool
    merge_back: bool
    checks: FrozenSet[str]


class Repo(NamedTuple):
    name: str
    repository: str
    primary: bool


class Rules:
    """The pipeline rules, parsed from the Hub's JSON shape."""

    def __init__(self) -> None:
        self.repos = []  # type: List[Repo]
        self.staging = ""
        self.alpha = ""
        self.release = ""
        self.changes = []  # type: List[Change]
        self._patterns = {}  # type: Dict[str, Pattern]
        self.commit_types = []  # type: List[str]
        self.exempt_subjects = []  # type: List[str]
        self.non_player_facing_types = []  # type: List[str]
        self.risk_labels = {}  # type: Dict[str, str]
        self.changelog_dir = ""
        self.changelog_extension = ""
        self.preamble = ""
        self.heading = ""
        self.skip_marker = ""
        self.sections = ()  # type: Tuple[str, ...]
        self.siblings_heading = ""
        self.not_needed = ""
        self.alpha_suffix = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Rules":
        """Build Rules from the endpoint's JSON object; malformed input raises RulesError."""
        try:
            r = cls()
            r.repos = [Repo(x["name"], x["repository"], bool(x["primary"])) for x in data["repos"]]
            branches = data["branches"]
            r.staging, r.alpha, r.release = branches["staging"], branches["alpha"], branches["release"]
            r.changes = [
                Change(c.get("prefix"), c.get("pattern"), c["into"], c.get("category"),
                       bool(c["playerFacing"]), bool(c["mergeBack"]), frozenset(c["checks"]))
                for c in data["changes"]
            ]
            for c in r.changes:
                if c.pattern is not None and c.pattern not in r._patterns:
                    try:
                        r._patterns[c.pattern] = re.compile(c.pattern)
                    except re.error as e:
                        raise RulesError(
                            "pipeline rules: change pattern %s is not a valid regex (%s)" % (c.pattern, e))
            commits = data["commits"]
            r.commit_types = list(commits["types"])
            r.exempt_subjects = list(commits["exemptSubjects"])
            for subject in r.exempt_subjects:
                try:
                    re.compile(subject)
                except re.error as e:
                    raise RulesError(
                        "pipeline rules: exempt subject %s is not a valid regex (%s)" % (subject, e))
            r.non_player_facing_types = list(commits["nonPlayerFacingTypes"])
            r.risk_labels = dict(data["labels"]["risk"])
            cl = data["changelog"]
            r.changelog_dir = cl["directory"]
            r.changelog_extension = cl["extension"]
            r.preamble = cl["preamble"]
            r.heading = cl["heading"]
            r.skip_marker = cl["skipMarker"]
            r.sections = tuple(cl["sections"])
            r.siblings_heading = data["prBody"]["siblingsHeading"]
            r.not_needed = data["prBody"]["notNeeded"]
            r.alpha_suffix = data["alphaSuffix"]
        except (KeyError, TypeError, AttributeError) as e:
            raise RulesError("The pipeline rules are malformed (%s: %s)" % (type(e).__name__, e))
        return r

    def change_for(self, branch: str) -> Optional[Change]:
        """The first change whose prefix or pattern matches the branch, or None.

        A prefix change with the `title` check is a human-named branch, so the rest of its name
        must also be a BRANCH_NAME; a bot prefix without it (Dependabot keeps artifact casing)
        matches by the prefix alone.
        """
        for change in self.changes:
            if change.prefix is not None:
                if branch.startswith(change.prefix) and (
                        "title" not in change.checks
                        or re.fullmatch(BRANCH_NAME, branch[len(change.prefix):])):
                    return change
            elif change.pattern is not None and self._patterns[change.pattern].fullmatch(branch):
                return change
        return None

    def is_stack_base(self, branch: str) -> bool:
        """True for a branch whose change carries all four checks and is not merged back."""
        change = self.change_for(branch)
        return change is not None and not change.merge_back and change.checks >= _FULL_CHECKS

    def player_facing_subject(self, subject: str) -> bool:
        """False for a skip-marked subject or a `type(scope)!: ` one whose type is not player
        facing; the same test as the Hub's Conventions.playerFacing. An empty scope or no space
        after the colon is not that form, so `chore(): x` and `chore:x` are player facing."""
        if self.skip_marker and self.skip_marker in subject:
            return False
        if not self.non_player_facing_types:
            return True
        types = "|".join(re.escape(t) for t in self.non_player_facing_types)
        return not re.match(r"^(%s)(\([^)]+\))?!?: " % types, subject)

    def conventional(self) -> Pattern:
        """Pattern for a conventional commit subject: type(scope)!: text."""
        types = "|".join(re.escape(t) for t in self.commit_types)
        return re.compile(r"^(%s)(\([^)]+\))?!?: \S" % types)

    def exempt_commit(self) -> Pattern:
        """Pattern for commit subjects exempt from the conventional-commit check."""
        return re.compile("|".join("(?:%s)" % s for s in self.exempt_subjects))

    def primary(self) -> Repo:
        """The primary repository."""
        for repo in self.repos:
            if repo.primary:
                return repo
        raise RulesError("The pipeline rules name no primary repository")

    def repo_names(self) -> Dict[str, str]:
        """Repository slug to display name."""
        return {repo.repository: repo.name for repo in self.repos}


def _parse(text: str) -> Dict[str, Any]:
    data = json.loads(text)
    if not isinstance(data, dict) or "changes" not in data:
        raise ValueError("not a rules object")
    return data


def fetch_rules_json(url: str) -> Dict[str, Any]:
    """GET the rules object from the Hub once; any failure raises RulesError."""
    try:
        # The Hub's proxy answers 403 to Python's default User-Agent.
        agent = "pipeline-rules/1.0"
        repository = os.environ.get("GITHUB_REPOSITORY")
        if repository:
            agent += " (+https://github.com/%s)" % repository
        request = urllib.request.Request(
            url, headers={"Accept": "application/json", "User-Agent": agent}
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            if response.status != 200:
                raise ValueError("HTTP %s" % response.status)
            body = response.read().decode("utf-8")
        return _parse(body)
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as e:
        raise RulesError("Could not fetch the pipeline rules from %s (%s)" % (url, e))


def load_rules(env: Mapping[str, str], fallback: Optional[dict] = None) -> Rules:
    """Load the rules from PIPELINE_RULES_FILE, else HUB_RULES_URL, else the fallback."""
    path = env.get("PIPELINE_RULES_FILE")
    if path:
        try:
            with open(path, encoding="utf-8") as f:
                return Rules.from_dict(_parse(f.read()))
        except (OSError, ValueError) as e:
            raise RulesError("Could not read the pipeline rules file %s (%s)" % (path, e))
    url = env.get("HUB_RULES_URL")
    if url:
        return Rules.from_dict(fetch_rules_json(url))
    if fallback is not None:
        return Rules.from_dict(fallback)
    raise RulesError("HUB_RULES_URL is not set")
