"""`scripts/batch`: the batch runner's command line (R2.3, R3.1).

Exit codes: 0 done, idle, paused, blocked, not qualified or waiting on Zak;
1 a runner error, a refused command or a failed `batch qualify`; 3 HALT, now or still; 75 resumable
(a lock or the build container is busy, a child of an earlier run is alive,
or GitHub or the network gave no answer). The last stdout line is a JSON
summary.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import traceback
from pathlib import Path

from . import commands, ctx as C, paths, records, report, state as S

USAGE = """\
batch: the river batch runner (qa/RUNNER.md).

  batch tick [--max-batches N]                 Tester thread: recover, run up to N batches (default 3).
  batch status [--json]                        The queue, the running batch and the waiting-on-Zak list.
  batch enqueue --record FILE                  Queue a council-approved change (the agent that ran the council).
  batch withdraw NAME                          Withdraw an entry that is not in a batch.
  batch requeue NAME --reason TEXT [--zak]     A failed entry back to the queue (the desk caused it).
  batch tried NAME --reason TEXT               A failed entry was the change's fault.
  batch settle ID --zak                        Zak: end a forward path that cannot finish (R13.4).
  batch cleanup drop ID --zak                  Zak: drop a cleanup item resolved by hand.
  batch resume --zak                           Zak: clear a HALT or a pause.
  batch stage --kind calibration|baseline --id ID [--change NAME...]
                                               Build and stage a calibration or baseline candidate.
  batch qualify [--run-only] [--change NAME...]
                                               Qualify the runner (part 1 A->B->A builds, part 2 one run).
  batch migrate --record FILE [--record FILE...]
                                               Once: queue/approved.jsonl -> the state file.
  batch report [ID]                            Rewrite a report (default: every pending one).

Agents never pass --zak. Exit: 0 ok/idle/paused/blocked, 1 error or refused, 3 HALT, 75 retry later.
"""


def zak_only(a) -> None:
    if not getattr(a, "zak", False):
        raise S.Refused("Zak-only command: needs --zak (agents never pass it)")


def main(argv: list[str], ctx: C.Ctx | None = None) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0 if argv else 1
    ap = argparse.ArgumentParser(prog="batch", add_help=False)
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("tick")
    p.add_argument("--max-batches", type=int, default=None)
    p = sub.add_parser("status")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("enqueue")
    p.add_argument("--record", type=Path, required=True)
    p = sub.add_parser("withdraw")
    p.add_argument("name")
    p = sub.add_parser("requeue")
    p.add_argument("name")
    p.add_argument("--reason", required=True)
    p.add_argument("--zak", action="store_true")
    p = sub.add_parser("tried")
    p.add_argument("name")
    p.add_argument("--reason", required=True)
    p = sub.add_parser("settle")
    p.add_argument("id")
    p.add_argument("--zak", action="store_true")
    p = sub.add_parser("cleanup")
    p.add_argument("action", choices=["drop"])
    p.add_argument("id")
    p.add_argument("--zak", action="store_true")
    p = sub.add_parser("resume")
    p.add_argument("--zak", action="store_true")
    p = sub.add_parser("stage")
    p.add_argument("--kind", required=True)
    p.add_argument("--id", required=True)
    p.add_argument("--change", action="append", default=[])
    p = sub.add_parser("qualify")
    p.add_argument("--run-only", action="store_true")
    p.add_argument("--change", action="append", default=[])
    p = sub.add_parser("migrate")
    p.add_argument("--record", type=Path, action="append", default=[])
    p = sub.add_parser("report")
    p.add_argument("id", nargs="?")
    try:
        a = ap.parse_args(argv)
    except SystemExit:
        print(USAGE)
        return 1
    ctx = ctx or C.Ctx()

    def on_signal(signum, _frame):
        # Sol M8, Opus minor: SIGINT too. Stop and reap the holder (it ends
        # the tool's group) before the locks are released on the way out;
        # running and the lease stay for the next tick's recovery.
        ctx.tools.stop()
        raise KeyboardInterrupt(f"signal {signum}")
    previous = {s: signal.signal(s, on_signal) for s in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)}
    try:
        return _run(a, ctx)
    finally:
        for s, h in previous.items():
            signal.signal(s, h)


def _run(a, ctx: C.Ctx) -> int:
    summary: dict = {"command": a.cmd}
    code = C.EXIT_OK
    try:
        if a.cmd == "tick":
            from . import tick
            code, summary = tick.tick(ctx, a.max_batches or ctx.cfg["batches_per_tick"])
            summary["command"] = "tick"
        elif a.cmd == "status":
            print(commands.status(a.json))
        elif a.cmd == "enqueue":
            summary["status"] = commands.enqueue(ctx, a.record)
        elif a.cmd == "withdraw":
            commands.withdraw(a.name)
        elif a.cmd == "requeue":
            summary["status"] = commands.requeue(ctx, a.name, a.reason, a.zak)
        elif a.cmd == "tried":
            commands.tried(a.name, a.reason)
        elif a.cmd == "settle":
            zak_only(a)
            from . import settle
            summary["settle"] = settle.settle(ctx, a.id)
            report.flush(ctx)
        elif a.cmd == "cleanup":
            zak_only(a)
            commands.cleanup_drop(ctx, a.id)
        elif a.cmd == "resume":
            zak_only(a)
            summary["cleared"] = commands.resume()
        elif a.cmd == "stage":
            from . import qualify
            summary.update(qualify.stage_cmd(ctx, a.kind, a.id, a.change))
        elif a.cmd == "qualify":
            from . import qualify
            summary.update(qualify.qualify_cmd(ctx, a.run_only, a.change))
            report.flush(ctx)
            if summary.get("qualified") is not True:
                code = C.EXIT_ERROR                   # Gemini GM2-1: not qualified is a failed command
        elif a.cmd == "migrate":
            summary.update(commands.migrate(ctx, a.record))
        elif a.cmd == "report":
            if a.id:
                report.write(a.id)
            summary["written"] = report.flush(ctx) + ([a.id] if a.id else [])
    except C.ReExec:
        raise
    except (S.Refused, records.RecordError) as e:            # a bad record is a refusal, never a runner error
        code, summary["refused"] = C.EXIT_ERROR, str(e)
        print(f"error: {e}", file=sys.stderr)
    except C.Busy as e:
        code, summary["busy"] = C.EXIT_RESUMABLE, e.message
    except C.AuthRefused as e:                     # Gemini minor: an auth refusal is not resumable by retrying
        code, summary["auth"] = C.EXIT_ERROR, str(e)
        print(f"error: GitHub auth: {e}", file=sys.stderr)
    except C.Unknown as e:
        code, summary["unknown"] = C.EXIT_RESUMABLE, str(e)
    except C.Halt as e:
        from .tick import commit_stop
        commit_stop(e)
        report.flush(ctx)
        code, summary["halt"] = C.EXIT_HALT, e.message
    except C.Stop as e:
        from .tick import commit_stop
        if isinstance(e, (C.Pause,)):
            commit_stop(e)
        if a.cmd in ("qualify", "stage"):
            code = C.EXIT_ERROR                       # I20: a live blocking condition refuses
        summary[e.kind] = e.message
    except qualify_halted() as e:
        code, summary["halt"] = C.EXIT_HALT, str(e)
        print(f"error: {e}", file=sys.stderr)
    except Exception as e:  # noqa: BLE001 - R11.5
        code = runner_error(a.cmd, e)
        summary["error"] = str(e)
    except KeyboardInterrupt as e:
        code, summary["interrupted"] = C.EXIT_RESUMABLE, str(e)
    if a.cmd == "tick" and code == C.EXIT_OK:            # Gemini, Opus minor: only a clean tick clears it
        try:
            with S.transaction() as st:
                st["last_error"] = None
        except S.StateError:
            pass
    print(json.dumps({"exit": code, **summary}, default=str))
    return code


def qualify_halted():
    from .qualify import HaltedState
    return HaltedState


def runner_error(step: str, e: Exception) -> int:
    """R11.5: commit last_error and exit 1; a second error at the same step
    is a HALT."""
    tb = traceback.format_exc(limit=6)
    print(tb, file=sys.stderr)
    try:
        with S.transaction("runner-error", step=step) as st:
            run = st.get("running") or {}
            where = f"{step}:{run.get('id')}:{run.get('phase')}"
            last = st.get("last_error") or {}
            if last.get("where") == where:
                S.set_halt(st, f"a second runner error at {where}: {e}", step=where,
                           advice="Zak: read the traceback in the tick log, then `batch resume --zak`")
                S.add_report(st, "halt-runner-error")
                st["last_error"] = {"where": where, "error": str(e), "count": 2}
                return C.EXIT_HALT
            st["last_error"] = {"where": where, "error": str(e), "count": 1}
    except S.StateError:
        pass
    return C.EXIT_ERROR


def entry() -> int:
    try:
        code = main(sys.argv[1:])
    except C.ReExec:
        os.environ["RIVER_BATCH_REEXEC"] = "1"
        os.execv(sys.executable, [sys.executable, "-I", str(paths.SCRIPTS / "batch"), *sys.argv[1:]])
    return code
