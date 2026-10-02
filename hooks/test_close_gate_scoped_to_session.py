#!/usr/bin/env python3
"""The close gate and the close commit must scope to THIS session, not its worktree.

Two parallel sessions on one plain checkout both have worktree `main`; two
sessions sharing a worktree both have its slug. Anything that attributes
session-close artifacts by worktree cannot tell them apart:

  - verify-session-close-cascade.py passed gate 1 if ANY file today carried the
    slug, so session A's gate went green on session B's note; and gate 3
    flagged ANY uncommitted Sessions/ file carrying the slug, so A was blocked
    on B's half-written note. On a plain checkout the gate did not run at all.
  - session-end-hook.sh staged every Decisions/ file dated today and touched
    in the last 10 minutes, so A's close commit swept up B's decisions.

The fix: the closing-signal marker records this session's exact
`session_file`; the gate checks THAT file. A file or decision whose frontmatter
names its owner (`session_id:`) is attributed by owner. Only when neither
exists does the old worktree behavior apply.

Assertions (each runs the real hook as a subprocess, hermetic HOME + vault):
  1. A without a note, B with one (main) -> A is BLOCKED, naming A's file.
  2. Same, two sessions sharing one worktree slug -> A is BLOCKED.
  3. A committed, B's note uncommitted -> A PASSES (main and shared worktree).
  4. A committed, B's owned decision uncommitted -> A PASSES.
  5. Marker already consumed (retry turn): A's owned note committed, B's
     uncommitted -> A PASSES (identity from the note's own frontmatter).
  6. The close commit stages A's decisions, never B's.
  7. The injected cascade tells the model to stamp decisions with the id.
  NEGATIVE CONTROLS - the gate still has teeth:
  8. A's own note uncommitted -> BLOCKED, naming A's file and not B's.
  9. A's own decision uncommitted -> BLOCKED, naming it.
 10. A's note committed and nothing else -> PASSES (not an always-block).
 11. The close commit still stages A's own decision AND an untagged one.
  FALLBACK (no marker, no owner) - unchanged behavior:
 12. Plain checkout, no marker -> the gate skips, as before.
 13. Worktree, no marker, no note for the slug -> BLOCKED, as before.
 14. A trivial close (the cascade told it to skip itself) is not gated.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

HOOKS = Path(__file__).resolve().parent
REPO = HOOKS.parent
GATE = HOOKS / "verify-session-close-cascade.py"
DETECT = HOOKS / "detect-closing-signal.py"
END_HOOK = REPO / "scripts" / "session-end-hook.sh"

META = "⚙️ Meta"
TODAY = datetime.now().strftime("%Y-%m-%d")
A, B = "sessA-1111-aaaa", "sessB-2222-bbbb"
WT = "wt-shared"

failures: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    if cond:
        print(f"PASS: {label}")
    else:
        failures.append(label)
        print(f"FAIL: {label}" + (f"\n      {detail}" if detail else ""))


def git(vault: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(vault), *args],
        capture_output=True, text=True, check=True,
    ).stdout


class Env:
    """One hermetic world: a git vault with the cascade installed + a HOME."""

    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="close-gate-")).resolve()
        self.vault = self.root / "vault"
        self.home = self.root / "home"
        (self.home / ".claude").mkdir(parents=True)
        self.sessions = self.vault / META / "Sessions"
        self.decisions = self.vault / META / "Decisions"
        for d in (self.sessions, self.decisions, self.vault / META / "scripts"):
            d.mkdir(parents=True)
        # Runner installed => the gate ENFORCES (hard-block), not advisory.
        (self.vault / META / "scripts" / "session-close-runner.sh").write_text("#!/bin/bash\n")
        (self.vault / "README.md").write_text("# vault\n")
        git(self.vault, "init", "-q")
        git(self.vault, "config", "user.email", "t@example.com")
        git(self.vault, "config", "user.name", "t")
        git(self.vault, "add", "-A")
        git(self.vault, "commit", "-qm", "init")
        self.report = self.root / "runner.report"
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.report.write_text(f"...\nRUNNER COMPLETE @ {stamp}\n")
        self.transcript = self.root / "transcript.jsonl"
        self.transcript.write_text(json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text",
                                     "text": "Cascade complete. Closing the session."}]},
        }) + "\n")

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def cwd(self, mode: str) -> Path:
        if mode == "main":
            return self.vault
        wt = self.vault / ".claude" / "worktrees" / WT
        wt.mkdir(parents=True, exist_ok=True)
        return wt

    def note(self, slug: str, sid: str, owned: bool = False, commit: bool = False) -> Path:
        """A session note named the way detect-closing-signal names it."""
        path = self.sessions / f"{TODAY}T15-25-{slug}-{sid[:8].replace('-', '')}.md"
        owner = f'session_id: "{sid}"\n' if owned else ""
        path.write_text(
            f"---\ntype: session\nworktree: {slug}\n{owner}session_date: {TODAY}\n---\n"
            f"# Session\n\nbody for {sid}\n"
        )
        if commit:
            self.commit(path)
        return path

    def decision(self, slug: str, sid: str | None, name: str, commit: bool = False) -> Path:
        path = self.decisions / f"{TODAY}-{name}.md"
        owner = f'session_id: "{sid}"\n' if sid else ""
        path.write_text(
            f"---\ntype: decision\nworktree: {slug}\n{owner}decision_date: {TODAY}\n---\n"
            f"# Decision {name}\n"
        )
        if commit:
            self.commit(path)
        return path

    def commit(self, path: Path) -> None:
        git(self.vault, "add", "--", str(path))
        git(self.vault, "commit", "-qm", f"add {path.name}")

    def marker(self, sid: str, session_file: Path) -> Path:
        m = self.home / ".claude" / f".closing-signal-{sid}.json"
        m.write_text(json.dumps({"session_file": str(session_file), "is_trivial": False}))
        return m

    def _env(self) -> dict:
        env = {k: v for k, v in os.environ.items()
               if k not in ("VERIFY_CASCADE_BYPASS", "VERIFY_CASCADE_SOFT")}
        env.update(HOME=str(self.home), VAULT_ROOT=str(self.vault),
                   ABS_RUNNER_REPORT=str(self.report))
        return env

    def gate(self, sid: str, mode: str) -> tuple[int, str]:
        payload = {"session_id": sid, "transcript_path": str(self.transcript),
                   "cwd": str(self.cwd(mode))}
        r = subprocess.run([sys.executable, str(GATE)], input=json.dumps(payload),
                           capture_output=True, text=True, env=self._env(),
                           cwd=str(self.cwd(mode)), timeout=60)
        return r.returncode, r.stderr

    def end_hook(self, sid: str) -> None:
        env = self._env()
        env.update(CLOSE_MAX_LOAD_PER_CORE="99999", CLOSE_MUTEX=str(self.root / "close.lock"))
        # transcript_path "" keeps the Haiku fallback out of a hermetic test.
        subprocess.run(["bash", str(END_HOOK)],
                       input=json.dumps({"session_id": sid, "transcript_path": ""}),
                       capture_output=True, text=True, env=env, cwd=str(self.vault),
                       timeout=120)

    def tracked(self, path: Path) -> bool:
        rel = path.relative_to(self.vault).as_posix()
        return bool(git(self.vault, "ls-files", "--", rel).strip())


def scenario(fn):
    env = Env()
    try:
        fn(env)
    except Exception as e:  # a crash in one scenario must not hide the rest
        failures.append(f"{fn.__name__} crashed: {e!r}")
        print(f"FAIL: {fn.__name__} crashed: {e!r}")
    finally:
        env.cleanup()


# ── 1 + 2: the false green (A has no note, B does) ────────────────────────────
for _mode, _slug in (("main", "main"), ("worktree", WT)):
    def _false_green(e: Env, mode=_mode, slug=_slug) -> None:
        own = e.sessions / f"{TODAY}T15-25-{slug}-sessA1111.md"  # never written
        e.note(slug, B, owned=True, commit=True)
        e.marker(A, own)
        rc, err = e.gate(A, mode)
        check(rc == 2, f"[{mode}] A with no note is BLOCKED although B has one (rc={rc})", err[-400:])
        check(own.name in err, f"[{mode}] the block names A's own file", err[-400:])
    _false_green.__name__ = f"false_green_{_mode}"
    scenario(_false_green)


# ── 3: the false block (B's note uncommitted) ─────────────────────────────────
for _mode, _slug in (("main", "main"), ("worktree", WT)):
    def _false_block(e: Env, mode=_mode, slug=_slug) -> None:
        own = e.note(slug, A, owned=True, commit=True)
        e.note(slug, B, owned=True, commit=False)
        e.marker(A, own)
        rc, err = e.gate(A, mode)
        check(rc == 0, f"[{mode}] A PASSES while B's note is uncommitted (rc={rc})", err[-400:])
    _false_block.__name__ = f"false_block_{_mode}"
    scenario(_false_block)


# ── 4: B's owned decision uncommitted does not block A ────────────────────────
def decision_false_block(e: Env) -> None:
    own = e.note(WT, A, owned=True, commit=True)
    e.decision(WT, B, "b-decision", commit=False)
    e.marker(A, own)
    rc, err = e.gate(A, "worktree")
    check(rc == 0, f"A PASSES while B's owned decision is uncommitted (rc={rc})", err[-400:])
scenario(decision_false_block)


# ── 5: marker consumed (retry turn) -> identity from the note's frontmatter ───
def retry_without_marker(e: Env) -> None:
    e.note(WT, A, owned=True, commit=True)
    e.note(WT, B, owned=True, commit=False)
    rc, err = e.gate(A, "worktree")
    check(rc == 0, f"retry with the marker gone: A PASSES on its own owned note (rc={rc})", err[-400:])
scenario(retry_without_marker)


# ── 6 + 11: the close commit stages this session's decisions only ────────────
def end_hook_scopes_decisions(e: Env) -> None:
    own = e.note("main", A, owned=True)
    mine = e.decision("main", A, "a-decision")
    theirs = e.decision("main", B, "b-decision")
    untagged = e.decision("main", None, "hand-written")
    e.marker(A, own)
    e.end_hook(A)
    check(e.tracked(own), "close commit includes A's session note")
    check(not e.tracked(theirs), "close commit does NOT stage B's decision")
    check(e.tracked(mine), "NEGATIVE CONTROL: close commit still stages A's own decision")
    check(e.tracked(untagged), "FALLBACK: an untagged decision is staged as before")
scenario(end_hook_scopes_decisions)


# ── 7: the cascade tells the model to stamp decisions with this session ──────
def cascade_asks_for_owner(e: Env) -> None:
    lines = [json.dumps({"type": "user", "message": {"content": f"msg {i}"}}) for i in range(6)]
    e.transcript.write_text("\n".join(lines) + "\n")
    payload = {"prompt": "ok bye", "session_id": A, "cwd": str(e.vault),
               "transcript_path": str(e.transcript)}
    env = e._env()
    env.pop("ANTHROPIC_API_KEY", None)
    r = subprocess.run([sys.executable, str(DETECT)], input=json.dumps(payload),
                       capture_output=True, text=True, env=env, cwd=str(e.vault), timeout=60)
    try:
        ctx = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
    except Exception:
        ctx = r.stdout
    check("Decisions dir" in ctx, "detect-closing-signal emitted the full cascade", ctx[:300])
    check(f'session_id: "{A}"' in ctx,
          "the cascade asks for session_id in each decision's frontmatter", ctx[:600])
scenario(cascade_asks_for_owner)


# ── 8 + 9 + 10: negative controls ─────────────────────────────────────────────
def own_note_uncommitted(e: Env) -> None:
    own = e.note("main", A, owned=True, commit=False)
    other = e.note("main", B, owned=True, commit=False)
    e.marker(A, own)
    rc, err = e.gate(A, "main")
    check(rc == 2, f"NEGATIVE CONTROL: A's own uncommitted note BLOCKS (rc={rc})", err[-400:])
    check(own.name in err and other.name not in err,
          "NEGATIVE CONTROL: the block names A's note and not B's", err[-600:])
scenario(own_note_uncommitted)


def own_decision_uncommitted(e: Env) -> None:
    own = e.note("main", A, owned=True, commit=True)
    mine = e.decision("main", A, "a-decision", commit=False)
    e.marker(A, own)
    rc, err = e.gate(A, "main")
    check(rc == 2, f"NEGATIVE CONTROL: A's own uncommitted decision BLOCKS (rc={rc})", err[-400:])
    check(mine.name in err, "NEGATIVE CONTROL: the block names A's decision", err[-600:])
scenario(own_decision_uncommitted)


def trivial_close_not_blocked(e: Env) -> None:
    # A trivial session (<5 user messages) is told to SKIP the cascade, so its
    # marker names a note that is never built. Gating it would block a goodbye
    # the cascade itself said to give.
    own = e.sessions / f"{TODAY}T15-25-main-sessA1111.md"
    m = e.marker(A, own)
    m.write_text(json.dumps({"session_file": str(own), "is_trivial": True}))
    rc, err = e.gate(A, "main")
    check(rc == 0, f"a trivial close (cascade skipped by design) is not blocked (rc={rc})", err[-400:])
scenario(trivial_close_not_blocked)


def clean_close_passes(e: Env) -> None:
    own = e.note("main", A, owned=True, commit=True)
    e.marker(A, own)
    rc, err = e.gate(A, "main")
    check(rc == 0, f"NEGATIVE CONTROL: a fully committed close PASSES (rc={rc})", err[-400:])
scenario(clean_close_passes)


# ── 12 + 13: no marker, no owner -> unchanged behavior ────────────────────────
def fallback_plain_skips(e: Env) -> None:
    rc, err = e.gate(A, "main")
    check(rc == 0, f"FALLBACK: plain checkout with no marker skips, as before (rc={rc})", err[-400:])
scenario(fallback_plain_skips)


def fallback_worktree_has_teeth(e: Env) -> None:
    rc, err = e.gate(A, "worktree")
    check(rc == 2, f"FALLBACK: worktree with no marker and no note BLOCKS, as before (rc={rc})", err[-400:])
scenario(fallback_worktree_has_teeth)


if failures:
    print(f"\n{len(failures)} assertion(s) failed")
    sys.exit(1)
print("\nAll close-gate session-scoping assertions passed")
