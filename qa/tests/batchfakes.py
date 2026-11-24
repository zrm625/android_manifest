"""Fakes for the batch runner's tests: GitHub, the phone, tools, podman.
Nothing here runs gh, git remotes, adb, fastboot or podman."""
import hashlib
import json
import subprocess
from types import SimpleNamespace

import _path  # noqa: F401
from riverbatch import ctx as C, github, ports

SHA = lambda s: hashlib.sha1(s.encode()).hexdigest()  # noqa: E731


class FakeGH(github.GitHub):
    """In-memory GitHub. ``prs[(repo, n)]`` dicts; ``tips[(repo, ref)]``;
    ``commits[(repo, sha)] = {"parents", "tree"}``. ``fail`` maps a method
    name to an exception (or a list of them, consumed in order)."""

    def __init__(self):
        super().__init__(runner=self._no_runner, sleep=lambda s: None)
        self.prs, self.tips, self.commits, self.files, self.changed = {}, {}, {}, {}, {}
        self.comments_by = {}
        self.fail = {}
        self.calls = []
        self.push = {}
        self.merge_effect = None       # callable(repo, n, sha) applied on merge_pr

    def _no_runner(self, cmd):
        raise AssertionError(f"unexpected raw call {cmd}")

    def _f(self, name, *a):
        self.calls.append((name, *a))
        f = self.fail.get(name)
        if isinstance(f, list):
            if f:
                e = f.pop(0)
                if e is not None:
                    raise e
        elif f is not None:
            raise f

    def rate_limit(self):
        self._f("rate_limit")
        return {}

    def can_push(self, repo):
        self._f("can_push", repo)
        return self.push.get(repo, True)

    def pr_view(self, repo, n):
        self._f("pr_view", repo, n)
        p = self.prs[(repo, n)]
        return dict(p)

    def pr_list(self, repo, head, state="all"):
        self._f("pr_list", repo, head, state)
        out = []
        for (r, n), p in sorted(self.prs.items()):
            if r == repo and p.get("headRefName") == head and (state == "all" or p["state"] == state.upper()):
                out.append(dict(p, number=n))
        return out

    def ls_remote(self, url, ref):
        self._f("ls_remote", url, ref)
        return self.tips.get((github.slug(url), ref))

    def commit(self, repo, sha):
        self._f("commit", repo, sha)
        return dict(self.commits[(repo, sha)])

    def merge_base(self, repo, base, head):
        self._f("merge_base", repo, base, head)
        return base

    def file_at(self, repo, path, ref):
        self._f("file_at", repo, path, ref)
        return self.files.get((repo, path, ref), (None, None))

    def changed_files(self, repo, n):
        self._f("changed_files", repo, n)
        return self.changed.get((repo, n), [])

    def comments(self, repo, n):
        return list(self.comments_by.get((repo, n), []))

    def comment(self, repo, n, body):
        self._f("comment", repo, n)
        self.comments_by.setdefault((repo, n), []).append(body)

    def close_pr(self, repo, n):
        self._f("close_pr", repo, n)
        self.prs[(repo, n)]["state"] = "CLOSED"

    def reopen_pr(self, repo, n):
        self._f("reopen_pr", repo, n)
        self.prs[(repo, n)]["state"] = "OPEN"

    def create_pr(self, repo, head, title, body):
        self._f("create_pr", repo, head)
        n = max([k[1] for k in self.prs if k[0] == repo] + [100]) + 1
        self.prs[(repo, n)] = {"state": "OPEN", "headRefName": head, "headRefOid": self.tips.get(
            (repo, f"refs/heads/{head}")), "baseRefName": "river-22.2", "isCrossRepository": False,
            "mergeable": "MERGEABLE", "mergeCommit": None}

    def merge_pr(self, repo, n, sha):
        self._f("merge_pr", repo, n, sha)
        if self.merge_effect:
            self.merge_effect(repo, n, sha)

    def call(self, cmd, repo=None):
        self._f("call", tuple(cmd))
        return self.raw.get(tuple(cmd), "") if hasattr(self, "raw") else ""


class FakeDev:
    """A phone for non-elevating reads. ``props`` answer the KB read;
    ``reads`` map a command substring to stdout. root() and guard() fail the
    test: KB never elevates (I1, I2)."""
    serial = "ZY225DPT7F"

    def __init__(self, props=None, state="device", fastboot=False, rc=0, reads=None):
        self.props = {"ro.product.device": "river", "ro.serialno": "ZY225DPT7F", "ro.boot.slot_suffix": "_a",
                      "sys.boot_completed": "1", "ro.river.build_id": "kg-1-abc", "ro.vendor.build.date.utc": "100",
                      **(props or {})}
        self._state, self._fastboot, self.rc = state, fastboot, rc
        self.reads = reads or {}
        self.cmds = []

    def state(self):
        return self._state

    def in_fastboot(self):
        return self._fastboot

    def root(self, *a, **k):
        raise AssertionError("KB must never root the phone")

    def guard(self):
        raise AssertionError("KB must never call dev.guard()")

    def sh_rc(self, cmd, timeout=30):
        self.cmds.append(cmd)
        if "getprop" in cmd and "echo" in cmd:
            from riverbatch import kb
            out = "".join(f"{k}={self.props.get(v, '')}\n" for k, v in kb.PROPS.items())
            return self.rc, out, ""
        for k, v in self.reads.items():
            if k in cmd:
                return 0, v, ""
        return 0, "", ""


class FakeTools(ports.Tools):
    """Scripted tools: ``script[name] = callable(args) -> (rc, stdout)`` or
    a list of (rc, stdout)."""

    def __init__(self, script=None):
        super().__init__()
        self.script = script or {}
        self.calls = []

    def run(self, argv, batch_id, log=None, env=None, timeout=None):
        name = argv[0].rsplit("/", 1)[-1]
        args = argv[1:]
        self.calls.append((name, tuple(args), batch_id))
        s = self.script.get(name)
        if callable(s):
            rc, out = s(args)
        elif isinstance(s, list) and s:
            rc, out = s.pop(0)
        else:
            rc, out = 0, ""
        if log:
            log.parent.mkdir(parents=True, exist_ok=True)
            with open(log, "a") as f:
                f.write(out)
        return ports.ToolResult(rc, out)


class FakePodman:
    def __init__(self, image="sha256:img", ps=""):
        self.image, self.ps, self.calls = image, ps, []

    def run(self, *args):
        self.calls.append(args)
        if args[:2] == ("image", "inspect"):
            return subprocess.CompletedProcess(args, 0 if self.image else 1, (self.image or "") + "\n", "")
        if args[0] == "ps":
            return subprocess.CompletedProcess(args, 0, self.ps, "")
        return subprocess.CompletedProcess(args, 0, "", "")


class NullGit:
    """Local git that succeeds and records (for paths that only prune)."""

    def __init__(self):
        self.calls = []

    def try_(self, path, *args, env=None):
        self.calls.append((path, args))
        return 0, ""

    def out(self, path, *args, env=None):
        self.calls.append((path, args))
        return ""

    def raw(self, path, *args):
        return b""


def mk_ctx(gh=None, dev=None, tools=None, podman=None, tc_ok=True, cfg=None):
    return C.Ctx(gh=gh or FakeGH(), git=NullGit(), tools=tools or FakeTools(), podman=podman or FakePodman(),
                 phone_factory=lambda: dev or FakeDev(), sleep=lambda s: None,
                 tc_check=lambda exp, *a, **k: SimpleNamespace(ok=tc_ok, blocking=[] if tc_ok else ["x: dirty"],
                                                              seconds=0.1, summary=lambda n=12: "x: dirty"),
                 cfg={"batch_size": 4, "batches_per_tick": 3, "free_space_floor_gb": 60,
                      "wifi_ssid_file": None, "wifi_psk_file": None, **(cfg or {})})


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
