import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import arena_workshop as addon


class FakeObjects(dict):
    pass


class TestScene:
    def __init__(self, name="Scene", objects=None):
        self.name = name
        self.objects = FakeObjects(objects or {})


class TestMesh:
    def __init__(self):
        self.name = "Cube"
        self.vertices = [
            (-1.0, -1.0, -1.0), (1.0, -1.0, -1.0),
            (1.0, 1.0, -1.0), (-1.0, 1.0, -1.0),
            (-1.0, -1.0, 1.0), (1.0, -1.0, 1.0),
            (1.0, 1.0, 1.0), (-1.0, 1.0, 1.0),
        ]
        self.polygons = [
            SimpleNamespace(vertices=face)
            for face in ((0, 1, 2, 3), (4, 7, 6, 5), (0, 4, 5, 1),
                         (1, 5, 6, 2), (2, 6, 7, 3), (4, 0, 3, 7))
        ]
        self.materials = []
        self.shape_keys = None
        self.users = 1

    def keys(self):
        return ()


class TestObject:
    def __init__(self):
        self.name = "Cube"
        self.type = "MESH"
        self.data = TestMesh()
        self.location = (0.0, 0.0, 0.0)
        self.rotation_euler = (0.0, 0.0, 0.0)
        self.rotation_mode = "XYZ"
        self.scale = (1.0, 1.0, 1.0)
        self.parent = None
        self.animation_data = None
        self.modifiers = []
        self.constraints = []
        self.children = []
        self.material_slots = []
        self.hide_viewport = False
        self.hide_render = False
        self.users_collection = (object(),)

    def keys(self):
        return ()


class TestScenes:
    def __init__(self):
        self.startup_scene = TestScene("Scene")
        self.switched_scene = TestScene("SwitchedScene")

    def __iter__(self):
        return iter((self.startup_scene, self.switched_scene))

    def get(self, name):
        return next((scene for scene in self if scene.name == name), None)


class TestObjectCollection(dict):
    def __init__(self, scenes):
        super().__init__()
        self.scenes = scenes
        self.removal_count = 0

    def remove(self, obj, do_unlink=True):
        self.removal_count += 1
        self.pop(obj.name, None)
        if do_unlink:
            for scene in self.scenes:
                scene.objects.pop(obj.name, None)


class FakeTimers:
    def __init__(self):
        self.callbacks = {}

    def register(self, callback, first_interval=0.0):
        if callback in self.callbacks:
            raise ValueError("timer already registered")
        self.callbacks[callback] = first_interval

    def is_registered(self, callback):
        return callback in self.callbacks

    def unregister(self, callback):
        del self.callbacks[callback]

    def run_once(self, callback):
        interval = callback()
        if interval is None:
            self.callbacks.pop(callback, None)
        else:
            self.callbacks[callback] = interval
        return interval


class TestBpy:
    def __init__(self, restricted=False):
        scenes = TestScenes()
        objects = TestObjectCollection(scenes)
        default_cube = TestObject()
        scenes.startup_scene.objects[default_cube.name] = default_cube
        objects[default_cube.name] = default_cube
        self._data = type("Data", (), {"scenes": scenes, "objects": objects})()
        self.allow_data = not restricted
        self.data_accesses = 0
        self.context = type("Context", (), {"scene": self._data.scenes.startup_scene})()
        self.app = type("App", (), {"timers": FakeTimers()})()

    @property
    def data(self):
        self.data_accesses += 1
        if not self.allow_data:
            raise AttributeError("'_RestrictData' object has no attribute 'scenes'")
        return self._data


class TestMailbox:
    root = "fake-mailbox"

    def __init__(self, job):
        self.job = job
        self.result = None

    def claim_next(self, allowed_workspaces):
        return self.job

    def write_result(self, result):
        self.result = result


class SequenceMailbox:
    root = "fake-mailbox"

    def __init__(self, jobs):
        self.jobs = list(jobs)
        self.results = []

    def claim_next(self, allowed_workspaces):
        return self.jobs.pop(0) if self.jobs else None

    def write_result(self, result):
        self.results.append(result)


class AddonResultTests(unittest.TestCase):
    def test_registration_defers_scene_capture_and_cleans_timers(self):
        previous = (addon._bpy, addon._mailbox, addon._startup_scene, addon._registered)
        bpy_instance = TestBpy(restricted=True)
        mailbox = SimpleNamespace(root="deferred-mailbox")
        timers = bpy_instance.app.timers

        try:
            with patch.dict(sys.modules, {"bpy": bpy_instance}), patch.object(
                addon, "FileMailbox", return_value=mailbox
            ) as create_mailbox:
                addon.register()
                self.assertEqual(bpy_instance.data_accesses, 0)
                self.assertIsNone(addon._startup_scene)
                self.assertIsNone(addon._mailbox)
                self.assertEqual(set(timers.callbacks), {addon._initialize_addon})

                with patch.object(addon._LOG, "exception") as log_exception:
                    self.assertEqual(timers.run_once(addon._initialize_addon), addon._POLL_SECONDS)
                log_exception.assert_called_once()
                create_mailbox.assert_not_called()
                self.assertNotIn(addon._poll_mailbox, timers.callbacks)

                bpy_instance.allow_data = True
                self.assertIsNone(timers.run_once(addon._initialize_addon))
                create_mailbox.assert_called_once()
                captured_scene = addon._startup_scene
                self.assertIs(captured_scene, bpy_instance._data.scenes.startup_scene)
                self.assertEqual(bpy_instance._data.objects.removal_count, 1)
                self.assertNotIn("Cube", captured_scene.objects)
                self.assertEqual(set(timers.callbacks), {addon._poll_mailbox})

                # Re-running initialization against the same scene is a no-op.
                self.assertIsNone(addon._initialize_addon())
                self.assertEqual(bpy_instance._data.objects.removal_count, 1)

                bpy_instance.context.scene = bpy_instance._data.scenes.switched_scene
                job = {"job_id": "captured-target"}
                with patch.object(addon, "execute_job", return_value={"scene_name": "Scene"}) as execute:
                    addon._execute(job)
                execute.assert_called_once_with(
                    bpy_instance, job, startup_scene=captured_scene,
                    screenshot_directory=Path("deferred-mailbox") / "screenshots",
                )
                self.assertIs(addon._startup_scene, captured_scene)

                addon.unregister()
                self.assertFalse(timers.callbacks)
                self.assertFalse(addon._registered)
                self.assertIsNone(addon._bpy)
                self.assertIsNone(addon._mailbox)
                self.assertIsNone(addon._startup_scene)

                addon.register()
                self.assertEqual(set(timers.callbacks), {addon._initialize_addon})
                addon.unregister()
                self.assertFalse(timers.callbacks)

                addon.register()
                self.assertEqual(set(timers.callbacks), {addon._initialize_addon})
                self.assertIsNone(timers.run_once(addon._initialize_addon))
                self.assertEqual(set(timers.callbacks), {addon._poll_mailbox})
                self.assertEqual(bpy_instance._data.objects.removal_count, 1)
                self.assertNotIn("Cube", bpy_instance._data.scenes.startup_scene.objects)
                addon.unregister()
                self.assertFalse(timers.callbacks)
        finally:
            if addon._bpy is bpy_instance:
                addon.unregister()
            addon._bpy, addon._mailbox, addon._startup_scene, addon._registered = previous

    def test_sequential_jobs_publish_screenshot_metadata_results(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)

        def job(job_id, operation):
            return {
                "protocol_version": "1.0", "job_id": job_id, "workspace_id": "test",
                "operation": operation, "parameters": {}, "status": "queued",
                "created_at": now.isoformat().replace("+00:00", "Z"),
                "deadline": (now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
                "result": None, "error": None,
            }

        screenshot = {
            "screenshot_path": "C:/queue/screenshots/job-shot.png",
            "scene_name": "Scene", "width": 640, "height": 480, "format": "PNG",
        }
        mailbox = SequenceMailbox([job("job-shot", "capture_screenshot"),
                                  job("job-next", "create_test_object")])
        previous = (addon._bpy, addon._mailbox, addon._startup_scene, addon._registered)
        test_bpy = TestBpy()
        addon._bpy = test_bpy
        addon._mailbox = mailbox
        addon._startup_scene = test_bpy.data.scenes.startup_scene
        addon._registered = True
        try:
            with patch.object(addon, "execute_job", side_effect=[screenshot, {"created": True}]) as execute:
                self.assertEqual(addon._poll_mailbox(), addon._POLL_SECONDS)
                self.assertEqual(addon._poll_mailbox(), addon._POLL_SECONDS)
            self.assertEqual(execute.call_count, 2)
        finally:
            addon._bpy, addon._mailbox, addon._startup_scene, addon._registered = previous

        self.assertEqual([result["job_id"] for result in mailbox.results], ["job-shot", "job-next"])
        self.assertEqual(mailbox.results[0]["status"], "completed")
        self.assertEqual(mailbox.results[0]["result"], screenshot)
        self.assertEqual(mailbox.results[0]["result"]["screenshot_path"], screenshot["screenshot_path"])

    def test_execute_uses_scene_captured_at_registration(self):
        previous_bpy, previous_scene, previous_mailbox = addon._bpy, addon._startup_scene, addon._mailbox
        bpy_instance = TestBpy()
        startup_scene = bpy_instance.data.scenes.get("Scene")
        job = {"job_id": "scene-target"}
        addon._bpy, addon._startup_scene, addon._mailbox = bpy_instance, startup_scene, None
        try:
            with patch.object(addon, "execute_job", return_value={"scene_name": "Scene"}) as execute:
                self.assertEqual(addon._execute(job), {"scene_name": "Scene"})
            execute.assert_called_once_with(
                bpy_instance, job, startup_scene=startup_scene, screenshot_directory=None
            )
        finally:
            addon._bpy, addon._startup_scene, addon._mailbox = previous_bpy, previous_scene, previous_mailbox

    def test_missing_object_is_written_as_structured_failure_result(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        job = {
            "protocol_version": "1.0",
            "job_id": "missing-object-job",
            "workspace_id": "test",
            "operation": "move_object",
            "parameters": {"object_name": "Absent", "location": [0, 0, 0]},
            "status": "queued",
            "created_at": now.isoformat().replace("+00:00", "Z"),
            "deadline": (now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
            "result": None,
            "error": None,
        }
        mailbox = TestMailbox(job)
        previous = (addon._bpy, addon._mailbox, addon._startup_scene, addon._registered)
        test_bpy = TestBpy()
        addon._bpy = test_bpy
        addon._mailbox = mailbox
        addon._startup_scene = test_bpy.data.scenes.get("Scene")
        addon._registered = True
        try:
            self.assertEqual(addon._poll_mailbox(), addon._POLL_SECONDS)
        finally:
            addon._bpy, addon._mailbox, addon._startup_scene, addon._registered = previous

        self.assertEqual(mailbox.result["status"], "failed")
        self.assertEqual(mailbox.result["job_id"], "missing-object-job")
        self.assertIsNone(mailbox.result["result"])
        self.assertEqual(mailbox.result["error"]["code"], "object_not_found")
        self.assertIn("Absent", mailbox.result["error"]["message"])
        self.assertIn("completed_at", mailbox.result)


if __name__ == "__main__":
    unittest.main()
