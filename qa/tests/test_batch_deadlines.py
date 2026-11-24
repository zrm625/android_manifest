"""Tool deadlines and signals (Sol M8, M9; Opus minors): the real holder,
a real child process group, no phone."""
import io
import json
import os
import signal
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import _path  # noqa: F401
from batchfakes import FakeTools, mk_ctx, write_json
from benchhome import bench_home
from riverbatch import cli, paths, ports, tick
from riverbatch import ctx as C
from riverbatch import state as S
from riverqa import children
from test_batch_flow import claim, steps


def wait_for(path, seconds=20):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if path.exists() and path.read_text().strip():
            return int(path.read_text().strip())
        time.sleep(0.05)
    raise AssertionError(f"{path} never appeared")


def group_dead(pgid):
    try:
        os.killpg(pgid, 0)
        return False
    except ProcessLookupError:
        return True


class Deadline(unittest.TestCase):
    def test_a_tool_past_its_deadline_is_stopped_with_its_group(self):
        with bench_home() as h:
            pidf = h / "pid"
            t0 = time.monotonic()
            r = ports.Tools().run(["sh", "-c", f"echo $$ > {pidf}; sleep 60 & wait"], None, h / "tool.log",
                                  timeout=1)
            self.assertTrue(r.timed_out)
            self.assertEqual(r.rc, ports.TIMEOUT_RC)
            self.assertLess(time.monotonic() - t0, 30)
            self.assertTrue(group_dead(wait_for(pidf)))                         # the tool and its sleep
            self.assertEqual(children.live_children(paths.home()), [])          # the record is gone
            self.assertIn("deadline", (h / "tool.log").read_text())

    def test_ctx_tool_passes_each_tools_deadline(self):
        with bench_home():
            tools = FakeTools()
            ctx = mk_ctx(tools=tools, cfg={"tool_timeouts": {"phone-qa": 5}})
            ctx.tool("phone-setup", [], None)
            ctx.tool("phone-qa", [], None)
            self.assertEqual(tools.timeouts, [("phone-setup", C.TOOL_TIMEOUTS["phone-setup"]), ("phone-qa", 5.0)])

    def test_a_hung_setup_voids_the_batch_and_rolls_back(self):
        with bench_home() as h:
            run = claim(["a"])
            write_json(h / "slot-state.json", {"pair": {"batch": "b0001", "attempts": ["fa-1"]}})
            write_json(h / "wifi-ssid", "x")
            tools = FakeTools({"phone-setup": [(ports.TIMEOUT_RC, "")]})
            ctx = mk_ctx(tools=tools, cfg={"wifi_ssid_file": str(h / "wifi-ssid"), "wifi_psk_file": str(h / "w")})
            ps = [p for p in steps() if getattr(p, "attribute", None) != "setup_candidate"]
            for p in ps:
                p.start()
            try:
                tick.run_batch(ctx, run, {"latest": {"cameras": [0, 1, 2]}})
            finally:
                mock.patch.stopall()
            st = S.read()
            self.assertIn(("flash", ("--rollback",), "b0001"), tools.calls)      # the reservation is recovered
            self.assertEqual(st["entries"][0]["status"], "queued")              # void, not the change's fault
            self.assertIn("deadline", st["entries"][0]["reason"])
            self.assertEqual(st["failure_history"], {})


class Interrupt(unittest.TestCase):
    def test_sigint_stops_and_reaps_the_holder_before_exit(self):
        with bench_home() as h:
            pidf = h / "pid"
            ctx = mk_ctx()
            ctx.tools = ports.Tools()

            def fake_tick(ctx_, n):
                ctx_.tools.run(["sh", "-c", f"echo $$ > {pidf}; sleep 60 & wait"], None, None)
                return 0, {}

            def interrupt():
                wait_for(pidf)
                os.kill(os.getpid(), signal.SIGINT)
            th = threading.Thread(target=interrupt)
            before = signal.getsignal(signal.SIGINT)
            with mock.patch.object(tick, "tick", fake_tick), redirect_stdout(io.StringIO()) as out, \
                    redirect_stderr(io.StringIO()):
                th.start()
                code = cli.main(["tick"], ctx)
            th.join()
            self.assertEqual(code, C.EXIT_RESUMABLE)
            self.assertIn("interrupted", json.loads(out.getvalue().strip().splitlines()[-1]))
            self.assertTrue(group_dead(int(pidf.read_text())))
            self.assertEqual(children.live_children(paths.home()), [])
            self.assertIs(signal.getsignal(signal.SIGINT), before)                # handlers restored


class LastError(unittest.TestCase):
    def test_only_a_clean_tick_clears_last_error(self):
        # Gemini, Opus minor: exit 3 or 75 says nothing about the error.
        with bench_home():
            with S.transaction() as st:
                st["last_error"] = {"where": "tick:b0001:building", "error": "x", "count": 1}
            for code in (C.EXIT_RESUMABLE, C.EXIT_HALT):
                with mock.patch.object(tick, "tick", return_value=(code, {})), redirect_stdout(io.StringIO()):
                    cli.main(["tick"], mk_ctx())
                self.assertIsNotNone(S.read()["last_error"])
            with mock.patch.object(tick, "tick", return_value=(C.EXIT_OK, {})), redirect_stdout(io.StringIO()):
                cli.main(["tick"], mk_ctx())
            self.assertIsNone(S.read()["last_error"])


if __name__ == "__main__":
    unittest.main()
