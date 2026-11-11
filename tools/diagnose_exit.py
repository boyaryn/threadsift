"""Find out why Chrome leaves "exit_type":"Crashed" in a profile.

Run it from the project folder, on the machine where the problem shows, and close the
Chrome window by hand when it opens (the X button, not Ctrl+C). It uses a throwaway
profile, never `.session/`, and prints what it saw:

    uv run python tools/diagnose_exit.py plain       # Chrome alone, no automation
    uv run python tools/diagnose_exit.py threadsift  # Chrome driven the way `login` does

Add a profile folder as a second argument to test an existing one (it is kept, not deleted):

    uv run python tools/diagnose_exit.py threadsift .session

To see whether the page matters, open the one `login` opens:

    DIAG_URL=https://www.threads.com/login uv run python tools/diagnose_exit.py threadsift .session

Send the outputs. If `plain` already says Crashed, the cause is Chrome or the machine,
not threadsift.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def exit_type(profile: Path) -> str:
    try:
        return json.loads((profile / "Default" / "Preferences").read_text())["profile"]["exit_type"]
    except (OSError, ValueError, KeyError) as error:
        return f"unreadable ({type(error).__name__})"


def lock_state(profile: Path) -> str:
    lock = profile / "SingletonLock"
    if not (lock.is_symlink() or lock.exists()):
        return "no lock"
    target = os.readlink(lock) if lock.is_symlink() else "(not a symlink)"
    try:
        pid = int(target.rsplit("-", 1)[1])
        os.kill(pid, 0)
        return f"lock {target}, process {pid} alive"
    except (ValueError, IndexError):
        return f"lock {target}, pid unreadable"
    except OSError:
        return f"lock {target}, process gone (stale)"


def chrome_processes(profile: Path) -> int:
    out = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True).stdout
    return sum(1 for line in out.splitlines() if str(profile) in line and "diagnose_exit" not in line)


def say(started: float, message: str) -> None:
    print(f"[{time.monotonic() - started:6.2f}s] {message}", flush=True)


def plain(profile: Path) -> None:
    exe = next((shutil.which(n) for n in ("google-chrome", "google-chrome-stable", "chrome") if shutil.which(n)), None)
    if exe is None:
        sys.exit("No Chrome found on PATH")
    print(f"Chrome: {exe}")
    started = time.monotonic()
    process = subprocess.Popen([exe, f"--user-data-dir={profile}", "--no-first-run", "about:blank"])
    say(started, "Chrome started; close its window now")
    process.wait()
    say(started, f"Chrome process ended with code {process.returncode}; {lock_state(profile)}")
    time.sleep(2)
    say(started, f"two seconds later: exit_type={exit_type(profile)}; {chrome_processes(profile)} processes still use the profile")


def driven(profile: Path) -> None:
    from threadsift.collection import browser
    from threadsift.config import BrowserConfig

    config = BrowserConfig(session_dir=str(profile), headless=False)
    started = time.monotonic()
    with browser.open_browser(config, headless=False) as context:
        # Exactly what `login` does; set DIAG_URL to open a real page (default: a blank one).
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(os.environ.get("DIAG_URL", "about:blank"))
        say(started, f"Chrome started (exit_type now {exit_type(profile)}); close its window now")
        context.wait_for_event("close", timeout=0)
        say(started, f"close event received; {lock_state(profile)}; {chrome_processes(profile)} processes use the profile")
    say(started, f"open_browser returned; {lock_state(profile)}")
    time.sleep(2)
    say(started, f"two seconds later: exit_type={exit_type(profile)}; {chrome_processes(profile)} processes still use the profile")


def main() -> None:
    if len(sys.argv) not in (2, 3) or sys.argv[1] not in ("plain", "threadsift"):
        sys.exit(__doc__)
    existing = len(sys.argv) == 3
    profile = Path(sys.argv[2]).resolve() if existing else Path(tempfile.mkdtemp(prefix="exit-diagnosis-")).resolve()
    print(f"{'Existing' if existing else 'Throwaway'} profile: {profile}")
    print(f"Before: exit_type={exit_type(profile)}; {lock_state(profile)}; {chrome_processes(profile)} processes use the profile")
    (plain if sys.argv[1] == "plain" else driven)(profile)
    if not existing:
        shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    main()
