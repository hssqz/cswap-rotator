#!/usr/bin/env python3
"""
cswap-rotator: auto-switch active Claude Code account when 5h quota nears limit.

Design principles:
  - Stateless: every run computes fresh from cswap's sequence.json + Anthropic API
  - Two-tier polling: only query active account first; query candidates only when
    a switch is actually about to happen
  - Skip-soon-reset: don't switch if the active account's 5h window will reset
    within RESET_GRACE_MIN — natural recovery is cheaper than a switch
  - Single-instance: flock prevents launchd double-fires
  - JSON line logs to stdout — launchd captures to ~/Library/Logs/

Depends on the cswap (claude-swap) Python package being importable in the same
interpreter (see plist EnvironmentVariables.PYTHONPATH or use cswap's own venv).
"""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ============================================================================
# cswap dependency surface — kept narrow on purpose
# ----------------------------------------------------------------------------
# Public-ish:
#   claude_swap.paths.get_backup_root()     — locates ~/.../sequence.json
#   claude_swap.oauth.fetch_usage_for_account / extract_access_token
#   claude_swap.locking.FileLock            — share cswap's lock when persisting
# Internal (we accept the coupling, document it here):
#   ClaudeAccountSwitcher._read_credentials              (live keychain/file)
#   ClaudeAccountSwitcher._read_account_credentials      (backup keyring/file)
#   ClaudeAccountSwitcher._write_account_credentials     (refresh persist)
# ============================================================================
from claude_swap.paths import get_backup_root
from claude_swap import oauth
from claude_swap.locking import FileLock
from claude_swap.switcher import ClaudeAccountSwitcher


CONFIG_PATH = Path(
    os.environ.get("CSWAP_ROTATOR_CONFIG", "~/.config/cswap-rotator/config.json")
).expanduser()
LOCK_PATH = CONFIG_PATH.parent / ".lock"
HINT_PATH = CONFIG_PATH.parent / ".last-check.json"


# ============================================================================
# Logging — single JSON line per event, never partial
# ============================================================================
def emit(record: dict) -> None:
    record = {"ts": datetime.now(timezone.utc).isoformat(), **record}
    print(json.dumps(record, ensure_ascii=False), flush=True)


def die(reason: str, **extra) -> None:
    emit({"level": "fatal", "reason": reason, **extra})
    sys.exit(1)


# ============================================================================
# Notification hook — currently log-only.
# Plug in your channel of choice here (Lark webhook / macOS notification /
# Slack / email / file append). The emit() line below serves as audit trail
# regardless of whether real channel integration is added.
# ============================================================================
def notify(level: str, message: str) -> None:
    emit({"notification": level, "message": message})
    # TODO: implement your channel, e.g.:
    #   import urllib.request
    #   urllib.request.urlopen("https://lark/webhook", data=json.dumps(...).encode())
    #
    #   subprocess.run(["osascript", "-e",
    #                   f'display notification "{message}" with title "cswap-rotator"'])


# ============================================================================
# Single-instance guard (launchd should never double-fire, but be safe)
# ============================================================================
def acquire_lock():
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    fp = open(LOCK_PATH, "w")
    try:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        emit({"level": "info", "reason": "another instance running, skipping"})
        sys.exit(0)
    return fp


# ============================================================================
# Config — minimal surface, all defaults sensible
# ============================================================================
DEFAULTS = {
    "rotate_threshold_pct": 90,
    "safety_margin_pct": 30,
    "reset_grace_min": 15,
    "dry_run": False,
    "adaptive_polling": True,
}


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        die("config not found", path=str(CONFIG_PATH))
    user = json.loads(CONFIG_PATH.read_text())
    return {**DEFAULTS, **user}


# ============================================================================
# Adaptive polling (Version B gradient):
#   active < 20%   → 30 min   (5h window's 1/10 — safe and quiet)
#   active 20-40%  → 20 min
#   active 40-60%  → 15 min
#   active >= 60%  → 10 min   (matches launchd floor)
#   fetch failed   → 10 min   (retry soon)
#
# Hint file is a CACHE, not state — losing it just causes one extra check.
# Decision logic remains stateless; only the cadence is adaptive.
# ============================================================================
def adaptive_interval_minutes(active_pct, threshold: int) -> int:
    if active_pct is None:
        return 10  # transient failure → retry soon
    distance = threshold - active_pct
    if distance >= 70:
        return 30
    if distance >= 50:
        return 20
    if distance >= 30:
        return 15
    return 10


def read_hint() -> dict | None:
    if not HINT_PATH.exists():
        return None
    try:
        data = json.loads(HINT_PATH.read_text())
        data["next_due_at"] = datetime.fromisoformat(data["next_due_at"])
        return data
    except Exception:
        return None  # corrupted → ignore, will be overwritten


def write_hint(active_pct, interval_min: int) -> None:
    now = datetime.now(timezone.utc)
    payload = {
        "checked_at": now.isoformat(),
        "active_pct": active_pct,
        "interval_min": interval_min,
        "next_due_at": (now + timedelta(minutes=interval_min)).isoformat(),
    }
    try:
        HINT_PATH.parent.mkdir(parents=True, exist_ok=True)
        HINT_PATH.write_text(json.dumps(payload, ensure_ascii=False))
    except Exception as e:
        emit({"level": "warn", "stage": "write_hint", "error": repr(e)})


# ============================================================================
# Countdown parsing — cswap's format_reset() emits one of:
#   "{d}d {h}h"  |  "{h}h {m}m"  |  "{m}m"
# Permissive parser tolerates spacing variations.
# ============================================================================
def parse_countdown_to_minutes(s: str) -> int | None:
    if not s:
        return None
    total = 0
    cur = ""
    for ch in s:
        if ch.isdigit():
            cur += ch
        elif ch in "dhms":
            if not cur:
                continue
            n = int(cur)
            if ch == "d":
                total += n * 1440
            elif ch == "h":
                total += n * 60
            elif ch == "m":
                total += n
            cur = ""
    return total


# ============================================================================
# Quota fetch — wraps cswap.oauth with our error semantics
# ============================================================================
def fetch_quota(
    sw: ClaudeAccountSwitcher,
    account_num: str,
    email: str,
    creds: str,
    is_active: bool,
):
    """Returns (pct, reset_in_minutes) or (None, None) on any failure."""
    if not creds or not oauth.extract_access_token(creds):
        return None, None

    def persist(num, em, new_creds):
        with FileLock(sw.lock_file):
            sw._write_account_credentials(num, em, new_creds)

    try:
        usage = oauth.fetch_usage_for_account(
            account_num,
            email,
            creds,
            is_active=is_active,
            persist_credentials=None if is_active else persist,
        )
    except Exception as e:
        emit({"level": "warn", "stage": "fetch_quota",
              "account": account_num, "error": repr(e)})
        return None, None

    if not usage or "five_hour" not in usage:
        return None, None
    h5 = usage["five_hour"]
    return h5.get("pct"), parse_countdown_to_minutes(h5.get("countdown", ""))


# ============================================================================
# Switch action — uses cswap CLI (most stable interface)
# ============================================================================
def perform_switch(target_num: str) -> bool:
    try:
        result = subprocess.run(
            ["cswap", "--switch-to", str(target_num)],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception as e:
        emit({"level": "error", "stage": "switch", "error": repr(e)})
        return False
    if result.returncode != 0:
        emit({"level": "error", "stage": "switch",
              "rc": result.returncode, "stderr": result.stderr[:500]})
        return False
    return True


# ============================================================================
# Main decision logic — stateless, ~30 lines of actual flow
# ============================================================================
def main():
    cfg = load_config()
    threshold = cfg["rotate_threshold_pct"]
    margin = cfg["safety_margin_pct"]
    grace = cfg["reset_grace_min"]
    dry_run = cfg["dry_run"]
    adaptive = cfg["adaptive_polling"]

    lock_fp = acquire_lock()
    try:
        # ------------------------------------------------------------------
        # Adaptive polling: silent skip if hint says not yet due.
        # No log, no API call. launchd wakeup cost (~50ms) is the only spend.
        # ------------------------------------------------------------------
        if adaptive:
            hint = read_hint()
            if hint and datetime.now(timezone.utc) < hint["next_due_at"]:
                return  # silent — hint file's mtime is your "still alive" signal

        seq_path = get_backup_root() / "sequence.json"
        if not seq_path.exists():
            die("sequence.json missing — is cswap initialized?", path=str(seq_path))

        sequence = json.loads(seq_path.read_text())
        active_num = (
            str(sequence["activeAccountNumber"])
            if sequence.get("activeAccountNumber") is not None
            else None
        )
        accounts = sequence.get("accounts", {})

        if not active_num or active_num not in accounts:
            emit({"decision": "skip", "reason": "no active managed account"})
            return

        if len(accounts) < 2:
            emit({"decision": "skip", "reason": "only one managed account"})
            return

        sw = ClaudeAccountSwitcher()
        active_email = accounts[active_num]["email"]

        # ------------------------------------------------------------------
        # Tier 1: only check active account (1 API call)
        # ------------------------------------------------------------------
        active_creds = sw._read_credentials() or ""
        active_pct, reset_min = fetch_quota(
            sw, active_num, active_email, active_creds, is_active=True
        )

        # Update hint immediately after Tier 1 — fresh data drives next cadence.
        # Failure case (active_pct is None) → retry interval (10 min).
        if adaptive:
            write_hint(active_pct, adaptive_interval_minutes(active_pct, threshold))

        base = {"tier": 1, "active": active_num, "active_pct": active_pct,
                "reset_in_min": reset_min}

        if active_pct is None:
            emit({**base, "decision": "skip", "reason": "fetch active failed"})
            return

        if active_pct < threshold:
            emit({**base, "decision": "skip", "reason": "below threshold"})
            return

        if reset_min is not None and reset_min < grace:
            emit({**base, "decision": "skip", "reason": "active resets soon"})
            return

        # ------------------------------------------------------------------
        # Tier 2: query candidates (N-1 API calls, only when ready to switch)
        # ------------------------------------------------------------------
        candidates: dict[str, int] = {}
        for num, info in accounts.items():
            if num == active_num:
                continue
            email = info["email"]
            creds = sw._read_account_credentials(num, email)
            if not creds:
                emit({"level": "warn", "account": num,
                      "reason": "credentials missing for candidate"})
                continue
            pct, _ = fetch_quota(sw, num, email, creds, is_active=False)
            if pct is None:
                emit({"level": "warn", "account": num,
                      "reason": "fetch candidate quota failed"})
                continue
            candidates[num] = pct

        # Strictly-better filter — must be lower than active to be useful at all.
        # safety_margin is no longer a hard gate; it now distinguishes
        # "comfortable" switches from "under pressure" switches (see below).
        strictly_better = {n: p for n, p in candidates.items() if p < active_pct}

        full = {"tier": 2, "active": active_num, "active_pct": active_pct,
                "reset_in_min": reset_min, "candidates": candidates}

        if not strictly_better:
            # Truly nowhere to go — every account is at or above active.
            # Switching would worsen the situation, so we don't.
            emit({**full, "decision": "skip",
                  "reason": "no candidate better than active"})
            notify("critical",
                   f"ALL cswap accounts at limit (active={active_pct}%); manual intervention needed")
            return

        target = min(strictly_better, key=strictly_better.get)
        target_pct = strictly_better[target]
        under_pressure = target_pct >= active_pct - margin

        if dry_run:
            emit({**full, "decision": "would_switch", "target": target,
                  "target_pct": target_pct, "under_pressure": under_pressure,
                  "dry_run": True})
            return

        ok = perform_switch(target)
        if not ok:
            emit({**full, "decision": "switch_failed", "target": target})
            return

        if under_pressure:
            emit({**full, "decision": "switched_under_pressure",
                  "target": target, "target_pct": target_pct})
            notify("warning",
                   f"all cswap accounts near limit; switched to least-bad "
                   f"(account {target}, {target_pct}% used)")
        else:
            emit({**full, "decision": "switched",
                  "target": target, "target_pct": target_pct})
    finally:
        try:
            lock_fp.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
