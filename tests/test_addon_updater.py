import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from arena_workshop import addon_updater
from arena_workshop.addon_updater import (
    EXPECTED_BRANCH,
    UpdaterError,
    update_addon,
)


COMMIT = "0123456789abcdef0123456789abcdef01234567"


class FakeGit:
    def __init__(self, branch=EXPECTED_BRANCH, status="", pull_error=None):
        self.branch = branch
        self.status = status
        self.pull_error = pull_error
        self.calls = []

    def __call__(self, repository_root, arguments):
        arguments = tuple(arguments)
        self.calls.append(arguments)
        if arguments == ("branch", "--show-current"):
            return self.branch
        if arguments == ("status", "--porcelain", "--untracked-files=all"):
            return self.status
        if arguments == ("pull", "--ff-only"):
            if self.pull_error:
                raise UpdaterError(self.pull_error)
            return "Already up to date."
        if arguments == ("rev-parse", "HEAD"):
            return COMMIT
        raise AssertionError("unexpected Git arguments: {!r}".format(arguments))


class AddonUpdaterTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.repository = self.root / "ArenaBlenderWorkshop"
        self.repository.mkdir()
        (self.repository / ".git").mkdir()
        self.source_addon = self.repository / "arena_workshop"
        self._write_addon(self.source_addon, "new version")
        self.appdata = self.root / "Roaming"
        self.localappdata = self.root / "Local"
        self.git = FakeGit()
        self.process_check = lambda: []

    @staticmethod
    def _write_addon(directory, contents):
        directory.mkdir(parents=True, exist_ok=True)
        for filename in ("__init__.py", "mailbox.py", "protocol.py", "operations.py"):
            (directory / filename).write_text(contents + " " + filename, encoding="utf-8")

    @property
    def addons_root(self):
        return (self.appdata / "Blender Foundation" / "Blender" / "5.2"
                / "scripts" / "addons")

    def run_update(self, **overrides):
        arguments = {
            "repository_root": self.repository,
            "appdata": self.appdata,
            "localappdata": self.localappdata,
            "git_runner": self.git,
            "process_checker": self.process_check,
            "now": datetime(2026, 11, 3, 12, 0, 0),
            "token": "testtoken",
        }
        arguments.update(overrides)
        return update_addon(**arguments)

    def test_rejects_wrong_branch_before_pull(self):
        self.git.branch = "main"
        with self.assertRaisesRegex(UpdaterError, "Wrong branch 'main'"):
            self.run_update()
        self.assertNotIn(("pull", "--ff-only"), self.git.calls)

    def test_rejects_dirty_working_tree_before_process_check_or_pull(self):
        self.git.status = " M README.md"
        checked = []
        self.process_check = lambda: checked.append(True) or []
        with self.assertRaisesRegex(UpdaterError, "uncommitted or untracked changes"):
            self.run_update()
        self.assertFalse(checked)
        self.assertNotIn(("pull", "--ff-only"), self.git.calls)

    def test_stops_if_blender_is_running_without_stopping_it(self):
        self.process_check = lambda: ["blender.exe"]
        with self.assertRaisesRegex(UpdaterError, "Close Blender normally"):
            self.run_update()
        self.assertNotIn(("pull", "--ff-only"), self.git.calls)

    def test_handles_missing_addons_directory_and_installs_package(self):
        result = self.run_update()
        destination = self.addons_root / "arena_workshop"
        self.assertTrue(destination.is_dir())
        self.assertTrue((destination / "operations.py").is_file())
        self.assertEqual(result["commit"], COMMIT)
        self.assertEqual(result["branch"], EXPECTED_BRANCH)
        self.assertIsNone(result["backup_path"])
        self.assertIn(("pull", "--ff-only"), self.git.calls)
        self.assertFalse(any(self.addons_root.glob(".arena_workshop-stage-*")))

    def test_backs_up_existing_addon_and_never_removes_prior_backups(self):
        destination = self.addons_root / "arena_workshop"
        self._write_addon(destination, "old version")
        (destination / "user-note.txt").write_text("preserve old installed data", encoding="utf-8")
        existing_backup = self.localappdata / "ArenaBlenderWorkshop" / "addon-backups" / "keep-me"
        existing_backup.mkdir(parents=True)
        (existing_backup / "marker.txt").write_text("old backup", encoding="utf-8")

        result = self.run_update()

        backup = Path(result["backup_path"])
        self.assertTrue((backup / "user-note.txt").is_file())
        self.assertEqual((backup / "__init__.py").read_text(encoding="utf-8"), "old version __init__.py")
        self.assertEqual((destination / "__init__.py").read_text(encoding="utf-8"), "new version __init__.py")
        self.assertEqual((existing_backup / "marker.txt").read_text(encoding="utf-8"), "old backup")
        self.assertFalse(any(self.addons_root.glob(".arena_workshop-rollback-*")))

    def test_failed_install_restores_previous_addon_and_keeps_backup(self):
        destination = self.addons_root / "arena_workshop"
        self._write_addon(destination, "installed old")

        def fail_new_directory_promotion(source, target):
            if Path(source).name.startswith(".arena_workshop-stage-"):
                raise OSError("simulated promotion failure")
            os.replace(source, target)

        with self.assertRaisesRegex(UpdaterError, "previous add-on was restored"):
            self.run_update(replace_func=fail_new_directory_promotion)

        self.assertEqual((destination / "__init__.py").read_text(encoding="utf-8"),
                         "installed old __init__.py")
        backups = list((self.localappdata / "ArenaBlenderWorkshop" / "addon-backups").glob("arena_workshop_*"))
        self.assertEqual(len(backups), 1)
        self.assertTrue((backups[0] / "operations.py").is_file())
        self.assertFalse(any(self.addons_root.glob(".arena_workshop-stage-*")))
        self.assertFalse(any(self.addons_root.glob(".arena_workshop-rollback-*")))

    def test_git_pull_failure_leaves_installed_addon_untouched(self):
        destination = self.addons_root / "arena_workshop"
        self._write_addon(destination, "installed old")
        self.git.pull_error = "simulated network failure"

        with self.assertRaisesRegex(UpdaterError, "simulated network failure"):
            self.run_update()

        self.assertEqual((destination / "__init__.py").read_text(encoding="utf-8"),
                         "installed old __init__.py")
        self.assertFalse((self.localappdata / "ArenaBlenderWorkshop" / "addon-backups").exists())
        self.assertIn(("pull", "--ff-only"), self.git.calls)

    def test_rejects_incomplete_source_addon_before_install(self):
        (self.source_addon / "operations.py").unlink()
        with self.assertRaisesRegex(UpdaterError, "missing required file.*operations.py"):
            self.run_update()
        self.assertFalse(self.addons_root.exists())

    def test_windows_process_check_recognizes_blender_process_names(self):
        output = '"blender.exe","1234","Console","1","20,000 K"\n'
        output += '"blender-launcher.exe","5678","Console","1","5,000 K"\n'
        output += '"python.exe","9012","Console","1","10,000 K"\n'
        completed = SimpleNamespace(returncode=0, stdout=output, stderr="")
        with patch.object(addon_updater.os, "name", "nt"), patch.object(
            addon_updater.subprocess, "run", return_value=completed
        ):
            self.assertEqual(
                addon_updater._windows_blender_processes(),
                ["blender.exe", "blender-launcher.exe"],
            )

    def test_powershell_entrypoint_delegates_to_updater_module_from_repo(self):
        script = (Path(__file__).resolve().parents[1] / "update_blender_addon.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("$PSScriptRoot", script)
        self.assertIn("arena_workshop.addon_updater", script)
        self.assertIn("Get-Command python", script)
        self.assertNotIn("Stop-Process", script)


if __name__ == "__main__":
    unittest.main()
