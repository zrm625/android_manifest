"""The real GitHub adapter (riverbatch/github.py) at its process boundary:
recorded `gh` output, no network. Pagination (Sol M11, Gemini GM1), the
synchronous merge (Sol B4) and the GraphQL refusal classes (Sol I21)."""
import json
import subprocess
import unittest

import _path  # noqa: F401
from riverbatch import github
from riverqa import recovery

R = "zrm625/android_device_motorola_river"


class Recorded:
    """A `gh` runner that answers from recorded output by command prefix and
    records every command."""

    def __init__(self, answers):
        self.answers, self.cmds = answers, []

    def __call__(self, cmd):
        self.cmds.append(cmd)
        for prefix, (rc, out, err) in self.answers:
            if cmd[:len(prefix)] == prefix:
                return subprocess.CompletedProcess(cmd, rc, out, err)
        raise AssertionError(f"unexpected command {cmd}")


def two_pages(a, b):
    # What `gh api --paginate --slurp` prints: one array holding each page.
    return json.dumps([a, b])


class Pagination(unittest.TestCase):
    def test_comments_on_the_second_page_are_seen(self):
        page1 = [{"body": f"comment {i}"} for i in range(30)]
        page2 = [{"body": "Merged through x.\n\n<!-- river-batch b0001 -->"}]
        run = Recorded([(["gh", "api", "--paginate", "--slurp"], (0, two_pages(page1, page2), ""))])
        gh = github.GitHub(runner=run)
        self.assertEqual(len(gh.comments(R, 6)), 31)
        gh.comment_once(R, 6, "<!-- river-batch b0001 -->", "again")      # the marker is on page 2: no post
        self.assertFalse(any(c[:3] == ["gh", "pr", "comment"] for c in run.cmds))
        self.assertTrue(all("--slurp" in c for c in run.cmds))

    def test_pr_files_on_two_pages(self):
        page1 = [{"filename": f"f{i}", "status": "modified"} for i in range(30)]
        page2 = [{"filename": "default.xml", "status": "modified"}]
        run = Recorded([(["gh", "api", "--paginate", "--slurp"], (0, two_pages(page1, page2), ""))])
        files = github.GitHub(runner=run).changed_files("zrm625/android_manifest", 12)
        self.assertEqual([f["filename"] for f in files][-1], "default.xml")
        self.assertEqual(len(files), 31)

    def test_unslurped_concatenated_pages_are_malformed(self):
        # Plain --paginate prints the pages back to back: not one JSON value.
        run = Recorded([(["gh", "api"], (0, json.dumps([{"body": "a"}]) + json.dumps([{"body": "b"}]), ""))])
        with self.assertRaises(github.Unknown):
            github.GitHub(runner=run).comments(R, 6)


class SynchronousMerge(unittest.TestCase):
    T, M = "a" * 40, "9" * 40

    def test_rest_merge_at_the_tested_head_never_queues(self):
        run = Recorded([(["gh", "api", "-X", "PUT"], (0, json.dumps({"sha": self.M, "merged": True,
                                                                      "message": "merged"}), ""))])
        got = github.GitHub(runner=run).merge_pr(R, 7, self.T)
        self.assertEqual(got, self.M)
        (cmd,) = run.cmds
        self.assertEqual(cmd[:5], ["gh", "api", "-X", "PUT", f"repos/{R}/pulls/7/merge"])
        self.assertIn(f"sha={self.T}", cmd)
        self.assertIn("merge_method=merge", cmd)
        self.assertFalse({"pr", "--auto", "merge"} & set(cmd[1:3]))           # never `gh pr merge`
        self.assertNotIn("--auto", cmd)

    def test_not_merged_answer_is_a_refusal(self):
        for body in ({"merged": False, "message": "Pull Request is not mergeable"}, {"merged": True}):
            run = Recorded([(["gh", "api"], (0, json.dumps(body), ""))])
            with self.assertRaises(github.Refused):
                github.GitHub(runner=run).merge_pr(R, 7, self.T)

    def test_http_classes(self):
        cases = [("gh: Pull Request is not mergeable (HTTP 405)", github.Refused),
                 ("gh: Head branch was modified. Review and try the merge again. (HTTP 409)", github.Refused),
                 ("gh: Server Error (HTTP 502)", github.Unknown),
                 ("gh: Bad credentials (HTTP 401)", github.AuthRefused)]
        for err, exc in cases:
            run = Recorded([(["gh", "api"], (1, "", err))])
            with self.assertRaises(exc, msg=err):
                github.GitHub(runner=run).merge_pr(R, 7, self.T)


class GraphQLRefusals(unittest.TestCase):
    def test_explicit_graphql_refusals_are_not_transport(self):
        for err in ("GraphQL: Pull Request is not mergeable (mergePullRequest)",
                    "GraphQL: Head branch was modified. Review and try the merge again. (mergePullRequest)",
                    "GraphQL: Pull request Pull request is in clean status (enablePullRequestAutoMerge)",
                    "GraphQL: Base branch was modified. Review and try the merge again. (mergePullRequest)"):
            self.assertEqual(recovery.classify(1, "", err)[0], recovery.REFUSED, err)
        for err in ("GraphQL: Something went wrong while executing your query.",
                    "GraphQL: API rate limit exceeded for user ID 1."):
            self.assertEqual(recovery.classify(1, "", err)[0], recovery.TRANSPORT, err)


if __name__ == "__main__":
    unittest.main()
