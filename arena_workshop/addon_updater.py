"""Transactional Windows installer for the Blender 5.2 add-on package."""

import argparse
import csv
import os
import re
import shutil
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path

EXPECTED_BRANCH = "arena/aadcacbd-arenablenderworkshop"
_REQUIRED_ADDON_FILES = ("__init__.py", "mailbox.py", "protocol.py", "operations.py")
_BLENDER_PROCESS = re.compile(r"^blender(?:[-_].*)?\.exe$", re.IGNORECASE)


class UpdaterError(RuntimeError):
    """A user-actionable updater failure."""


def _run_git(repository_root, arguments):
    command = ["git", "-C", str(repository_root), *arguments]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise UpdaterError("Could not start Git. Install Git for Windows and retry: {}".format(exc)) from exc
    output = "\n".join(part.strip() for part in (completed.stdout, completed.stderr) if part.strip())
    if completed.returncode:
        detail = output or "Git returned exit code {}.".format(completed.returncode)
        raise UpdaterError("Git command {!r} failed: {}".format(" ".join(arguments), detail))
    return output


def _windows_blender_processes():
    if os.name != "nt":
        raise UpdaterError("The add-on updater is intended to run from Windows PowerShell.")
    try:
        completed = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, check=False
        )
    except OSError as exc:
        raise UpdaterError("Could not check whether Blender is running: {}".format(exc)) from exc
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip() or "tasklist failed."
        raise UpdaterError("Could not check whether Blender is running: {}".format(detail))
    try:
        rows = csv.reader(completed.stdout.splitlines())
        return [row[0] for row in rows if row and _BLENDER_PROCESS.fullmatch(row[0])]
    except (csv.Error, IndexError) as exc:
        raise UpdaterError("Could not parse the Windows process list: {}".format(exc)) from exc


def _verify_addon(directory):
    missing = [name for name in _REQUIRED_ADDON_FILES if not (directory / name).is_file()]
    if missing:
        raise UpdaterError(
            "Add-on directory {!s} is missing required file(s): {}".format(
                directory, ", ".join(missing)
            )
        )


def _is_directory(path):
    return path.is_dir() and not path.is_symlink()


def _rollback_install(destination, rollback_path, backup_path, destination_existed,
                      old_moved, new_promoted, replace_func, copy_tree, remove_tree):
    errors = []
    if destination.exists() and (new_promoted or old_moved or not destination_existed):
        try:
            remove_tree(destination)
        except Exception as exc:  # rollback must continue to the previous copy if possible
            errors.append("could not remove the incomplete install: {}".format(exc))

    if destination_existed and not destination.exists():
        if old_moved and rollback_path.exists():
            try:
                replace_func(rollback_path, destination)
            except Exception as exc:
                errors.append("could not move the previous add-on back: {}".format(exc))
        restored = False
        if destination.exists():
            try:
                _verify_addon(destination)
                restored = True
            except UpdaterError:
                pass
        if not restored and backup_path is not None and backup_path.exists():
            try:
                if destination.exists():
                    remove_tree(destination)
                copy_tree(backup_path, destination)
                _verify_addon(destination)
                restored = True
            except Exception as exc:
                errors.append("could not restore from the backup copy: {}".format(exc))
        if destination_existed and not restored:
            errors.append("the previous add-on could not be verified after rollback")
    return errors


def install_addon(source_addon, addons_root, backup_root,
                  copy_tree=None, replace_func=None, remove_tree=None, now=None, token=None):
    """Stage, back up, and atomically replace only the arena_workshop package."""
    copy_tree = copy_tree or shutil.copytree
    replace_func = replace_func or os.replace
    remove_tree = remove_tree or shutil.rmtree
    source_addon = Path(source_addon)
    addons_root = Path(addons_root)
    backup_root = Path(backup_root)
    destination = addons_root / "arena_workshop"
    token = token or uuid.uuid4().hex[:10]
    now = now or datetime.now()
    stamp = now.strftime("%Y%m%d-%H%M%S-%f")
    backup_path = None
    stage_path = addons_root / (".arena_workshop-stage-" + token)
    rollback_path = addons_root / (".arena_workshop-rollback-" + token)
    destination_existed = False
    old_moved = False
    new_promoted = False

    _verify_addon(source_addon)
    addons_root.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        if not _is_directory(destination):
            raise UpdaterError(
                "Expected {!s} to be a normal directory; refusing to replace a file or link.".format(destination)
            )
        destination_existed = True

    try:
        if destination_existed:
            backup_root.mkdir(parents=True, exist_ok=True)
            backup_path = backup_root / ("arena_workshop_{}_{}".format(stamp, token))
            copy_tree(destination, backup_path)
            if not _is_directory(backup_path):
                raise UpdaterError("The existing add-on backup could not be verified at {!s}.".format(backup_path))

        copy_tree(source_addon, stage_path, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        _verify_addon(stage_path)

        if destination_existed:
            replace_func(destination, rollback_path)
            old_moved = True
        replace_func(stage_path, destination)
        new_promoted = True
        _verify_addon(destination)

        if old_moved and rollback_path.exists():
            remove_tree(rollback_path)
            old_moved = False
    except Exception as exc:
        rollback_errors = _rollback_install(
            destination, rollback_path, backup_path, destination_existed,
            old_moved, new_promoted, replace_func, copy_tree, remove_tree,
        )
        if rollback_errors:
            detail = "; ".join(rollback_errors)
            backup_note = " Previous add-on backup: {!s}.".format(backup_path) if backup_path else ""
            hold_note = " Previous package still exists at {!s}.".format(rollback_path) if rollback_path.exists() else ""
            raise UpdaterError(
                "Installation failed ({}); rollback needs attention: {}. Target: {!s}.{}{}".format(
                    exc, detail, destination, backup_note, hold_note
                )
            ) from exc
        if destination_existed and not old_moved and not new_promoted and destination.exists():
            raise UpdaterError(
                "Installation failed before replacement; the existing add-on was left untouched ({}). "
                "Backup path, if created: {!s}.".format(exc, backup_path)
            ) from exc
        if destination_existed:
            raise UpdaterError(
                "Installation failed and the previous add-on was restored ({}). Backup: {!s}.".format(
                    exc, backup_path
                )
            ) from exc
        raise UpdaterError("Installation failed; the incomplete add-on was removed: {}".format(exc)) from exc
    finally:
        if stage_path.exists():
            try:
                remove_tree(stage_path)
            except Exception:
                pass

    return {"install_path": str(destination), "backup_path": str(backup_path) if backup_path else None}


def update_addon(repository_root, appdata, localappdata, expected_branch=EXPECTED_BRANCH,
                 git_runner=None, process_checker=None, copy_tree=None, replace_func=None,
                 remove_tree=None, now=None, token=None):
    """Validate repository/system state, fast-forward, then install the add-on."""
    repository_root = Path(repository_root).resolve()
    appdata = Path(appdata)
    localappdata = Path(localappdata)
    git_runner = git_runner or _run_git
    process_checker = process_checker or _windows_blender_processes

    if not repository_root.is_dir() or not (repository_root / ".git").exists():
        raise UpdaterError("Not a Git repository: {!s}".format(repository_root))

    def git(arguments):
        try:
            return git_runner(repository_root, arguments).strip()
        except UpdaterError:
            raise
        except Exception as exc:
            raise UpdaterError("Git command {!r} failed: {}".format(" ".join(arguments), exc)) from exc

    branch = git(["branch", "--show-current"])
    if branch != expected_branch:
        raise UpdaterError(
            "Wrong branch {!r}; expected {!r}. Switch to the expected branch and retry.".format(
                branch, expected_branch
            )
        )

    status = git(["status", "--porcelain", "--untracked-files=all"])
    if status:
        raise UpdaterError(
            "The repository has uncommitted or untracked changes. Commit or move them elsewhere, "
            "then rerun the updater.\n{}".format(status)
        )

    try:
        running = list(process_checker())
    except UpdaterError:
        raise
    except Exception as exc:
        raise UpdaterError("Could not check whether Blender is running: {}".format(exc)) from exc
    if running:
        raise UpdaterError(
            "Blender is still running ({}). Close Blender normally, then rerun this command; "
            "the updater will not stop or restart it.".format(", ".join(map(str, running)))
        )

    pull_output = git(["pull", "--ff-only"])
    commit = git(["rev-parse", "HEAD"])
    source_addon = repository_root / "arena_workshop"
    _verify_addon(source_addon)

    addons_root = appdata / "Blender Foundation" / "Blender" / "5.2" / "scripts" / "addons"
    backup_root = localappdata / "ArenaBlenderWorkshop" / "addon-backups"
    install = install_addon(
        source_addon, addons_root, backup_root,
        copy_tree=copy_tree, replace_func=replace_func, remove_tree=remove_tree,
        now=now, token=token,
    )
    return {
        "commit": commit,
        "branch": branch,
        "install_path": install["install_path"],
        "backup_path": install["backup_path"],
        "pull_output": pull_output,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Update the Blender 5.2 Arena add-on from this clean Git checkout")
    parser.add_argument("--repo-root", required=True, help="path to the ArenaBlenderWorkshop Git checkout")
    parser.add_argument("--expected-branch", default=EXPECTED_BRANCH, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if not os.environ.get("APPDATA") or not os.environ.get("LOCALAPPDATA"):
        print("Updater requires the current Windows user's APPDATA and LOCALAPPDATA directories.", file=sys.stderr)
        return 1
    try:
        result = update_addon(
            args.repo_root, os.environ["APPDATA"], os.environ["LOCALAPPDATA"],
            expected_branch=args.expected_branch,
        )
    except UpdaterError as exc:
        print("Add-on update failed: {}".format(exc), file=sys.stderr)
        return 1

    if result["pull_output"]:
        print(result["pull_output"])
    print("Blender add-on updated successfully.")
    print("Branch: {}".format(result["branch"]))
    print("Installed commit: {}".format(result["commit"]))
    print("Install location: {}".format(result["install_path"]))
    print("Backup location: {}".format(result["backup_path"] or "(no prior add-on was installed)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
