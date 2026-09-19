#!/usr/bin/env python3
"""Tests for pr_rules.py. Run: python3 -m unittest discover -s .github/scripts -p 'test_*.py'"""
import unittest

from pr_rules import Commit, check, is_player_facing, sibling_pr_problem, token_from_env

REPO = "MyPetORG/MyPet4"
SIBLINGS_OK = """Some description.

## Siblings
- Configurator: not needed
- Wiki: not needed
- Translations: not needed
- Editor-Translations: not needed
"""
GOOD_COMMITS = [Commit("a1", "fix(pets): guard water damage for endermen", 1),
                Commit("b2", "test(pets): cover rain damage", 1)]


def errors(base="staging", head="fix/drowning", title="Fixed Enderman pets taking damage in rain",
           body=SIBLINGS_OK, commits=None, repo=REPO, sibling_problem=None):
    return check(base, head, title, body, GOOD_COMMITS if commits is None else commits, repo,
                 sibling_problem)


class BranchNames(unittest.TestCase):
    def test_fix_feature_chore_are_accepted_into_staging(self):
        self.assertEqual([], errors(head="fix/drowning"))
        self.assertEqual([], errors(head="feature/pet-import"))
        self.assertEqual([], errors(head="chore/harness", title="Updated the test harness [skip ci]"))

    def test_an_unprefixed_branch_into_staging_fails(self):
        self.assertTrue(any("branch" in e for e in errors(head="drowning-fix")))

    def test_the_old_agent_prefix_fails(self):
        self.assertTrue(any("branch" in e for e in errors(head="claude/agent-12")))

    def test_hotfix_is_refused_into_staging(self):
        self.assertTrue(any("hotfix/" in e for e in errors(head="hotfix/crash")))

    def test_only_staging_or_a_merge_back_from_main_may_enter_alpha(self):
        self.assertEqual([], errors(base="alpha", head="staging", title="anything", body=""))
        self.assertEqual([], errors(base="alpha", head="main", title="anything", body=""))
        self.assertTrue(errors(base="alpha", head="fix/drowning"))

    def test_only_alpha_or_a_hotfix_may_enter_main(self):
        self.assertEqual([], errors(base="main", head="alpha", title="anything", body=""))
        self.assertEqual([], errors(base="main", head="hotfix/crash"))
        self.assertTrue(errors(base="main", head="staging", title="anything", body=""))
        self.assertTrue(errors(base="main", head="fix/drowning"))

    def test_a_merge_back_from_alpha_into_staging_is_accepted(self):
        self.assertEqual([], errors(base="staging", head="alpha", title="anything", body=""))

    def test_other_base_branches_are_not_policed(self):
        self.assertEqual([], errors(base="feature/big", head="whatever", title="x", body=""))


class Commits(unittest.TestCase):
    def test_a_non_conventional_commit_fails_and_names_its_sha(self):
        errs = errors(commits=[Commit("deadbee", "fixed stuff", 1)])
        self.assertTrue(any("deadbee" in e for e in errs), errs)

    def test_scope_is_optional_and_breaking_marker_allowed(self):
        self.assertEqual([], errors(commits=[Commit("a", "fix: x", 1),
                                             Commit("b", "feat(api)!: y", 1)]))

    def test_merge_commits_reverts_and_fixups_are_exempt(self):
        self.assertEqual([], errors(commits=[
            Commit("a", "Merge branch 'staging' into fix/drowning", 2),
            Commit("b", 'Revert "fix(pets): guard water damage"', 1),
            Commit("c", "fixup! fix(pets): guard water damage", 1)]))

    def test_promotion_prs_do_not_police_commits(self):
        self.assertEqual([], errors(base="alpha", head="staging", title="x", body="",
                                    commits=[Commit("a", "Fixed pets drowning (#12)", 1)]))

    def test_an_unknown_type_fails(self):
        self.assertTrue(errors(commits=[Commit("a", "wip(pets): x", 1)]))


class Titles(unittest.TestCase):
    def test_a_conventional_title_on_a_fix_fails(self):
        self.assertTrue(any("player-facing" in e for e in errors(title="fix(pets): water damage")))

    def test_skip_ci_on_a_fix_fails(self):
        self.assertTrue(any("[skip ci]" in e for e in errors(title="Fixed pets [skip ci]")))

    def test_a_lowercase_title_fails(self):
        self.assertTrue(errors(title="fixed pets drowning"))

    def test_an_empty_title_fails(self):
        self.assertTrue(errors(title="  "))

    def test_a_chore_title_must_end_with_skip_ci(self):
        self.assertTrue(any("[skip ci]" in e for e in
                            errors(head="chore/harness", title="Updated the test harness")))

    def test_a_conventional_chore_title_fails(self):
        self.assertTrue(errors(head="chore/harness", title="chore: harness [skip ci]"))

    def test_a_hotfix_title_is_player_facing(self):
        self.assertTrue(errors(base="main", head="hotfix/crash", title="fix: crash"))


class Siblings(unittest.TestCase):
    def test_a_missing_section_fails(self):
        self.assertTrue(any("Siblings" in e for e in errors(body="no section")))

    def test_a_missing_repo_line_fails_and_names_it(self):
        body = SIBLINGS_OK.replace("- Wiki: not needed\n", "")
        self.assertTrue(any("Wiki" in e for e in errors(body=body)))

    def test_a_sibling_must_use_the_same_branch_name(self):
        ok = SIBLINGS_OK.replace("- Wiki: not needed", "- Wiki: fix/drowning")
        bad = SIBLINGS_OK.replace("- Wiki: not needed", "- Wiki: fix/other")
        self.assertEqual([], errors(body=ok))
        self.assertTrue(any("fix/drowning" in e for e in errors(body=bad)))

    def test_the_own_repo_line_is_not_required(self):
        body = SIBLINGS_OK.replace("- Configurator: not needed\n", "") + "- Plugin: not needed\n"
        self.assertEqual([], errors(body=body, repo="MyPetORG/MyPet-Configurator"))

    def test_hotfixes_need_the_section_too(self):
        self.assertTrue(errors(base="main", head="hotfix/crash", body="none"))

    def test_the_section_accepts_bold_and_case_variations(self):
        body = SIBLINGS_OK.replace("## Siblings", "**siblings:**").replace("not needed", "Not needed")
        self.assertEqual([], errors(body=body))


class Dependabot(unittest.TestCase):
    def test_dependabot_is_accepted_without_title_or_siblings_rules(self):
        self.assertEqual([], errors(head="dependabot/gradle/com.zaxxer-HikariCP-7.1.0",
                                    title="chore(deps): Bump HikariCP from 7.0 to 7.1", body="",
                                    commits=[Commit("a", "chore(deps): bump HikariCP", 1)]))


class Crowdin(unittest.TestCase):
    def test_crowdin_service_branches_are_accepted_into_staging_as_is(self):
        for head in ("i18n_staging", "l10n_staging"):
            self.assertEqual([], errors(head=head, title="New Crowdin updates", body="",
                                        commits=[Commit("a", "New translations x (German)", 1)]))

    def test_crowdin_branches_are_refused_into_main(self):
        self.assertTrue(errors(base="main", head="i18n_staging", title="x", body=""))


class PlayerFacing(unittest.TestCase):
    def test_skip_ci_anywhere_marks_a_commit_non_player_facing(self):
        self.assertFalse(is_player_facing("Updated the test harness [skip ci] (#45)"))

    def test_dependabot_style_subjects_are_non_player_facing(self):
        self.assertFalse(is_player_facing("chore(deps): Bump HikariCP from 7.0 to 7.1 (#46)"))

    def test_a_player_facing_sentence_is_player_facing(self):
        self.assertTrue(is_player_facing("Fixed pets drowning (#47)"))


class SiblingPullRequests(unittest.TestCase):
    """A Siblings line naming a branch must point at a real PR into that repo's staging."""

    def stub(self, status, data):
        calls = []

        def get(url):
            calls.append(url)
            return status, data
        return get, calls

    def test_the_lookup_asks_for_prs_from_the_branch_into_staging_in_any_state(self):
        get, calls = self.stub(200, [{"number": 7}])
        self.assertIsNone(sibling_pr_problem("MyPetORG/MyPet-Wiki", "fix/drowning", get))
        self.assertEqual(["https://api.github.com/repos/MyPetORG/MyPet-Wiki/pulls"
                          "?head=MyPetORG%3Afix%2Fdrowning&base=staging&state=all"], calls)

    def test_no_pr_is_an_error_naming_the_repo_and_branch(self):
        get, _ = self.stub(200, [])
        problem = sibling_pr_problem("MyPetORG/MyPet-Wiki", "fix/drowning", get)
        self.assertIn("MyPetORG/MyPet-Wiki", problem)
        self.assertIn("fix/drowning", problem)

    def test_an_unreadable_repo_asks_for_the_siblings_token(self):
        for status in (401, 403, 404):
            get, _ = self.stub(status, None)
            problem = sibling_pr_problem("MyPetORG/MyPet4", "fix/drowning", get)
            self.assertIn("SIBLINGS_TOKEN", problem)
            self.assertIn("MyPetORG/MyPet4", problem)

    def test_any_other_failure_is_an_error_too(self):
        get, _ = self.stub(0, None)
        self.assertIn("MyPetORG/MyPet-Wiki",
                      sibling_pr_problem("MyPetORG/MyPet-Wiki", "fix/drowning", get))

    def test_check_looks_up_each_named_sibling_in_its_repo(self):
        asked = []

        def problem(repo, branch):
            asked.append((repo, branch))
            return "no PR in " + repo if repo == "MyPetORG/MyPet-Wiki" else None
        body = (SIBLINGS_OK.replace("- Wiki: not needed", "- Wiki: fix/drowning")
                .replace("- Translations: not needed", "- Translations: `fix/drowning`"))
        self.assertEqual(["no PR in MyPetORG/MyPet-Wiki"], errors(body=body, sibling_problem=problem))
        self.assertEqual([("MyPetORG/MyPet-Wiki", "fix/drowning"),
                          ("MyPetORG/MyPet-Translations", "fix/drowning")], asked)

    def test_not_needed_and_mismatched_lines_are_never_looked_up(self):
        asked = []
        body = SIBLINGS_OK.replace("- Wiki: not needed", "- Wiki: fix/other")

        def problem(repo, branch):
            asked.append(repo)
        errors(body=body, sibling_problem=problem)
        self.assertEqual([], asked)

    def test_the_token_prefers_siblings_token_and_falls_back_to_github_token(self):
        self.assertEqual("s", token_from_env({"SIBLINGS_TOKEN": "s", "GITHUB_TOKEN": "g"}))
        self.assertEqual("g", token_from_env({"SIBLINGS_TOKEN": "", "GITHUB_TOKEN": "g"}))
        self.assertEqual("", token_from_env({}))


if __name__ == "__main__":
    unittest.main()
