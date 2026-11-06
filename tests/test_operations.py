import math
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from arena_workshop.operations import (
    SMOKE_TEST_OBJECT,
    OperationError,
    execute_job,
    remove_default_startup_cube,
    resolve_startup_scene,
)
from arena_workshop.protocol import JobValidationError, validate_job


class FakeMesh:
    def __init__(self, name):
        self.name = name
        self.vertices = []
        self.polygons = []
        self.materials = []
        self.shape_keys = None
        self.users = 1
        self._custom_properties = {}

    def keys(self):
        return self._custom_properties.keys()

    def from_pydata(self, vertices, edges, faces):
        self.vertices = list(vertices)
        self.polygons = list(faces)

    def update(self):
        pass

    def copy(self):
        mesh = self._collection.new(self.name)
        mesh.vertices = list(self.vertices)
        mesh.polygons = list(self.polygons)
        mesh.materials = list(self.materials)
        return mesh


class FakeNodeSocket:
    def __init__(self):
        self.default_value = None


class FakeNodeInputs(dict):
    def __init__(self):
        super().__init__((key, FakeNodeSocket()) for key in ("Base Color", "Roughness", "Metallic"))


class FakeNode:
    def __init__(self):
        self.name = "Principled BSDF"
        self.type = "BSDF_PRINCIPLED"
        self.inputs = FakeNodeInputs()


class FakeNodes(list):
    def __init__(self):
        super().__init__([FakeNode()])

    def get(self, name):
        return next((node for node in self if node.name == name), None)


class FakeNodeTree:
    def __init__(self):
        self.nodes = FakeNodes()


class FakeDataBlock:
    def __init__(self):
        self._custom_properties = {}

    def get(self, key, default=None):
        return self._custom_properties.get(key, default)

    def __getitem__(self, key):
        return self._custom_properties[key]

    def __setitem__(self, key, value):
        self._custom_properties[key] = value

    def keys(self):
        return self._custom_properties.keys()


class FakeMaterial(FakeDataBlock):
    def __init__(self, name):
        super().__init__()
        self.name = name
        self.diffuse_color = None
        self.roughness = None
        self.metallic = None
        self.use_nodes = False
        self.node_tree = FakeNodeTree()


class FakeMaterials(dict):
    def new(self, name):
        if name in self:
            name = name + ".001"
        material = FakeMaterial(name)
        self[name] = material
        return material

    def remove(self, material):
        self.pop(material.name, None)


class FakeObject(FakeDataBlock):
    def __init__(self, name, mesh, data_objects):
        super().__init__()
        self._name = name
        self.data = mesh
        self._data_objects = data_objects
        self._collections = []
        self.location = (0.0, 0.0, 0.0)
        self.rotation_euler = (0.0, 0.0, 0.0)
        self.rotation_mode = "XYZ"
        self.scale = (1.0, 1.0, 1.0)
        self.type = "MESH"
        self.parent = None
        self.animation_data = None
        self.modifiers = []
        self.constraints = []
        self.children = []
        self.material_slots = []
        self.hide_viewport = False
        self.hide_render = False
        self._dimensions = (2.0, 2.0, 2.0)

    @property
    def users_collection(self):
        return tuple(self._collections)

    @property
    def dimensions(self):
        return self._dimensions

    @dimensions.setter
    def dimensions(self, values):
        self._dimensions = tuple(values)

    def copy(self):
        duplicate = self._data_objects.new(self.name, self.data)
        duplicate.location = tuple(self.location)
        duplicate.rotation_euler = tuple(self.rotation_euler)
        duplicate.scale = tuple(self.scale)
        duplicate.dimensions = tuple(self.dimensions)
        duplicate._custom_properties = dict(self._custom_properties)
        return duplicate

    @property
    def name(self):
        return self._name

    @name.setter
    def name(self, new_name):
        old_name = self._name
        self._data_objects.pop(old_name, None)
        self._name = new_name
        self._data_objects[new_name] = self
        for collection in self._collections:
            collection.rename(old_name, new_name, self)


class FakeSceneObjects(dict):
    def link(self, obj):
        self[obj.name] = obj
        obj._collections.append(self)

    def rename(self, old_name, new_name, obj):
        self.pop(old_name, None)
        self[new_name] = obj


class FakeSceneCollection:
    def __init__(self, scene_objects):
        self.objects = scene_objects


class FakeScene:
    def __init__(self, name):
        self.name = name
        self.objects = FakeSceneObjects()
        self.collection = FakeSceneCollection(self.objects)
        self.render = FakeRenderSettings()
        self.cycles = FakeCycles()
        self.camera = None


class FakeScenes(dict):
    def __iter__(self):
        return iter(self.values())

    def new(self, name):
        if name in self:
            raise ValueError("scene exists")
        scene = FakeScene(name)
        self[name] = scene
        return scene


class FakeObjects(dict):
    def __init__(self):
        super().__init__()
        self.scenes = []

    def new(self, name, mesh):
        unique_name = name
        suffix = 1
        while unique_name in self:
            unique_name = "{}.{}".format(name, suffix)
            suffix += 1
        obj = FakeObject(unique_name, mesh, self)
        self[unique_name] = obj
        return obj

    def remove(self, obj, do_unlink=True):
        self.pop(obj.name, None)
        if do_unlink:
            for collection in list(obj._collections):
                collection.pop(obj.name, None)


class FakeMeshes(dict):
    def new(self, name):
        unique_name = name
        suffix = 1
        while unique_name in self:
            unique_name = "{}.{}".format(name, suffix)
            suffix += 1
        mesh = FakeMesh(unique_name)
        mesh._collection = self
        self[unique_name] = mesh
        return mesh

    def remove(self, mesh):
        self.pop(mesh.name, None)


class FakeImageSettings:
    def __init__(self):
        self.file_format = "JPEG"
        self.color_mode = "RGB"


class FakeRenderSettings:
    def __init__(self):
        self.filepath = "//existing-render"
        self.use_file_extension = True
        self.resolution_x = 1920
        self.resolution_y = 1080
        self.resolution_percentage = 50
        self.engine = "CYCLES"
        self.display_mode = "WINDOW"
        self.image_settings = FakeImageSettings()


class FakeCycles:
    def __init__(self):
        self.samples = 64


class FakeRenderOperator:
    def __init__(self, bpy):
        self.bpy = bpy
        self.calls = []
        self.error = None
        self.status = {"FINISHED"}

    def render(self, write_still, scene):
        self.calls.append({"write_still": write_still, "scene": scene})
        if self.error is not None:
            raise self.error
        target_scene = self.bpy.data.scenes.get(scene)
        Path(target_scene.render.filepath).write_bytes(b"fake-png-image")
        return self.status


class FakeBpy:
    def __init__(self):
        scenes = FakeScenes()
        startup_scene = scenes.new("Scene")
        switched_scene = scenes.new("SwitchedScene")
        objects = FakeObjects()
        self.data = type("FakeData", (), {})()
        self.data.scenes = scenes
        self.data.objects = objects
        self.data.meshes = FakeMeshes()
        self.data.materials = FakeMaterials()
        cube_mesh = self.data.meshes.new("Cube")
        cube_mesh.from_pydata(
            [(-1, -1, -1), (1, -1, -1), (1, 1, -1), (-1, 1, -1),
             (-1, -1, 1), (1, -1, 1), (1, 1, 1), (-1, 1, 1)],
            [],
            [(0, 1, 2, 3), (4, 7, 6, 5), (0, 4, 5, 1),
             (1, 5, 6, 2), (2, 6, 7, 3), (4, 0, 3, 7)],
        )
        self.default_cube = self.data.objects.new("Cube", cube_mesh)
        startup_scene.collection.objects.link(self.default_cube)
        camera_data = self.data.meshes.new("CameraData")
        self.camera = self.data.objects.new("Camera", camera_data)
        self.camera.type = "CAMERA"
        startup_scene.collection.objects.link(self.camera)
        startup_scene.camera = self.camera
        self.startup_scene = startup_scene
        self.switched_scene = switched_scene
        self.context = type("FakeContext", (), {"scene": startup_scene})()
        self.app = type("FakeApp", (), {"version_string": "5.2.0 test fake"})()
        self.render_operator = FakeRenderOperator(self)
        self.ops = type("FakeOps", (), {})()
        self.ops.render = self.render_operator


def make_job(operation, parameters, job_id="job-001"):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    return {
        "protocol_version": "1.0",
        "job_id": job_id,
        "workspace_id": "test",
        "operation": operation,
        "parameters": parameters,
        "status": "queued",
        "created_at": now.isoformat().replace("+00:00", "Z"),
        "deadline": (now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
        "result": None,
        "error": None,
    }


def validated(job):
    return validate_job(job, {"test"})


class ModelingProtocolTests(unittest.TestCase):
    def test_accepts_each_new_operation(self):
        cases = [
            ("create_primitive", {"primitive_type": "cube", "object_name": "Block"}),
            ("move_object", {"object_name": "Block", "location": [1, 2, 3]}),
            ("rotate_object", {"object_name": "Block", "rotation": [10, 20, 30]}),
            ("scale_object", {"object_name": "Block", "scale": [1, 2, 3]}),
            ("rename_object", {"object_name": "Block", "new_name": "NewBlock"}),
            ("create_material", {
                "material_name": "Mat", "object_name": "Block",
                "base_color": [0.2, 0.4, 0.6, 1.0], "roughness": 0.5, "metallic": 0.1,
            }),
            ("capture_screenshot", {"width": 640, "height": 480}),
            ("duplicate_object", {"object_name": "Block", "new_name": "BlockCopy"}),
            ("delete_object", {"object_name": "Block"}),
            ("set_dimensions", {"object_name": "Block", "dimensions": [1, 2, 3]}),
        ]
        for operation, params in cases:
            with self.subTest(operation=operation):
                self.assertEqual(validated(make_job(operation, params))["operation"], operation)

    def test_create_primitive_rejects_invalid_type_name_vector_and_extra_fields(self):
        base = {"primitive_type": "cube", "object_name": "Block"}
        invalid = [
            dict(base, primitive_type="monkey"),
            dict(base, object_name="  "),
            dict(base, location=[0, 1]),
            dict(base, shell="echo unsafe"),
        ]
        for params in invalid:
            with self.subTest(params=params), self.assertRaises(JobValidationError):
                validated(make_job("create_primitive", params))

    def test_screenshot_and_new_modeling_protocols_reject_invalid_parameters(self):
        invalid = [
            ("capture_screenshot", {"width": 2000}),
            ("capture_screenshot", {"height": 0}),
            ("capture_screenshot", {"execute": "unsafe"}),
            ("duplicate_object", {"object_name": "Block"}),
            ("duplicate_object", {"object_name": "Same", "new_name": "Same"}),
            ("delete_object", {"object_name": "Block", "force": True}),
            ("set_dimensions", {"object_name": "Block", "dimensions": [1, 0, 1]}),
        ]
        for operation, params in invalid:
            with self.subTest(operation=operation, params=params), self.assertRaises(JobValidationError):
                validated(make_job(operation, params))

    def test_transform_protocols_reject_bad_vectors(self):
        cases = [
            ("move_object", {"object_name": "Block", "location": [0, 0, float("inf")]}),
            ("rotate_object", {"object_name": "Block", "rotation": [0, True, 0]}),
            ("scale_object", {"object_name": "Block", "scale": [1, 0, 1]}),
        ]
        for operation, params in cases:
            with self.subTest(operation=operation), self.assertRaises(JobValidationError):
                validated(make_job(operation, params))

    def test_rename_protocol_rejects_invalid_names_and_extra_keys(self):
        for params in (
            {"object_name": "Block", "new_name": ""},
            {"object_name": "Block", "new_name": "New", "extra": 1},
        ):
            with self.subTest(params=params), self.assertRaises(JobValidationError):
                validated(make_job("rename_object", params))

    def test_material_protocol_rejects_out_of_range_values(self):
        base = {
            "material_name": "Mat", "object_name": "Block",
            "base_color": [0.2, 0.4, 0.6, 1.0], "roughness": 0.5, "metallic": 0.1,
        }
        invalid = [
            dict(base, base_color=[0.2, 0.4, 1.1, 1.0]),
            dict(base, roughness=1.1),
            dict(base, metallic=-0.1),
        ]
        for params in invalid:
            with self.subTest(params=params), self.assertRaises(JobValidationError):
                validated(make_job("create_material", params))

    def test_unknown_or_non_string_operation_is_rejected(self):
        for operation in ("run_python", ["create_primitive"]):
            with self.subTest(operation=operation), self.assertRaises(JobValidationError):
                validated(make_job(operation, {}))


class DefaultCubeCleanupTests(unittest.TestCase):
    def setUp(self):
        self.bpy = FakeBpy()
        self.scene = self.bpy.startup_scene

    def test_removes_only_default_cube_and_preserves_camera_and_lights(self):
        light_data = self.bpy.data.meshes.new("LightData")
        light = self.bpy.data.objects.new("Light", light_data)
        light.type = "LIGHT"
        self.scene.collection.objects.link(light)

        self.assertTrue(remove_default_startup_cube(self.bpy, self.scene))
        self.assertIsNone(self.scene.objects.get("Cube"))
        self.assertNotIn("Cube", self.bpy.data.objects)
        self.assertIs(self.scene.objects.get("Camera"), self.bpy.camera)
        self.assertIs(self.scene.objects.get("Light"), light)
        self.assertIs(self.scene.camera, self.bpy.camera)

    def test_preserves_user_modified_cube_and_other_user_objects(self):
        self.bpy.default_cube.location = (0.25, 0.0, 0.0)
        user_mesh = self.bpy.data.meshes.new("UserMesh")
        user_object = self.bpy.data.objects.new("UserCreated", user_mesh)
        self.scene.collection.objects.link(user_object)

        self.assertFalse(remove_default_startup_cube(self.bpy, self.scene))
        self.assertIs(self.bpy.data.objects.get("Cube"), self.bpy.default_cube)
        self.assertIs(self.scene.objects.get("UserCreated"), user_object)

    def test_does_not_remove_arena_tagged_cube(self):
        self.bpy.default_cube["arena_workshop_job_id"] = "linear-BLE-99"

        self.assertFalse(remove_default_startup_cube(self.bpy, self.scene))
        self.assertIs(self.bpy.data.objects.get("Cube"), self.bpy.default_cube)

    def test_cleanup_is_idempotent(self):
        self.assertTrue(remove_default_startup_cube(self.bpy, self.scene))
        self.assertFalse(remove_default_startup_cube(self.bpy, self.scene))
        self.assertNotIn("Cube", self.bpy.data.objects)
        self.assertIs(self.bpy.data.objects.get("Camera"), self.bpy.camera)


class ModelingOperationTests(unittest.TestCase):
    def setUp(self):
        self.bpy = FakeBpy()
        self.active_scene = self.bpy.context.scene
        self.startup_scene = resolve_startup_scene(self.bpy)
        self.original_cube = self.bpy.default_cube
        self.original_cube_mesh = self.original_cube.data

    def run_job(self, operation, parameters, job_id="job-001", startup_scene=None,
                screenshot_directory=None):
        job = validated(make_job(operation, parameters, job_id=job_id))
        return execute_job(
            self.bpy, job, startup_scene=startup_scene,
            screenshot_directory=screenshot_directory,
        )

    def test_create_each_primitive_with_expected_mesh(self):
        expected = {
            "cube": (8, 6),
            "uv_sphere": (482, 512),
            "cylinder": (64, 34),
            "plane": (4, 1),
            "cone": (33, 33),
        }
        for primitive_type, counts in expected.items():
            with self.subTest(primitive_type=primitive_type):
                result = self.run_job(
                    "create_primitive",
                    {"primitive_type": primitive_type, "object_name": "Created" + primitive_type.title()},
                    job_id="primitive-" + primitive_type,
                )
                obj = self.bpy.data.objects[result["object_name"]]
                self.assertEqual((len(obj.data.vertices), len(obj.data.polygons)), counts)
                self.assertEqual(result["scene_name"], self.startup_scene.name)
                self.assertIs(self.startup_scene.objects.get(result["object_name"]), obj)
                self.assertTrue(result["created"])
                self.assertIs(self.bpy.context.scene, self.active_scene)
                self.assertIs(self.bpy.data.objects["Cube"], self.original_cube)
                self.assertIs(self.original_cube.data, self.original_cube_mesh)

    def test_primitive_transforms_and_retry_are_idempotent(self):
        params = {
            "primitive_type": "cube", "object_name": "MovedBlock",
            "location": [4, -2, 1], "rotation": [0, 90, 0], "scale": [2, 3, 4],
        }
        first = self.run_job("create_primitive", params, job_id="primitive-retry")
        obj = self.bpy.data.objects["MovedBlock"]
        before_count = len(self.bpy.data.objects)
        second = self.run_job("create_primitive", params, job_id="primitive-retry")
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(len(self.bpy.data.objects), before_count)
        self.assertEqual(obj.location, (4.0, -2.0, 1.0))
        self.assertAlmostEqual(obj.rotation_euler[1], math.pi / 2)
        self.assertEqual(obj.scale, (2.0, 3.0, 4.0))
        self.assertIs(self.bpy.context.scene, self.active_scene)

    def test_existing_object_is_never_replaced(self):
        original = self.bpy.data.objects.new("Protected", FakeMesh("ProtectedMesh"))
        with self.assertRaisesRegex(OperationError, "already exists") as caught:
            self.run_job("create_primitive", {"primitive_type": "cube", "object_name": "Protected"})
        self.assertEqual(caught.exception.code, "object_name_conflict")
        self.assertIs(self.bpy.data.objects["Protected"], original)

    def test_duplicate_object_copies_mesh_and_preserves_source(self):
        self.original_cube.location = (2.0, 3.0, 4.0)
        params = {"object_name": "Cube", "new_name": "CubeCopy"}
        first = self.run_job("duplicate_object", params, "duplicate-1")
        duplicate = self.bpy.data.objects["CubeCopy"]
        second = self.run_job("duplicate_object", params, "duplicate-1")
        self.assertTrue(first["duplicated"])
        self.assertFalse(second["duplicated"])
        self.assertIsNot(duplicate, self.original_cube)
        self.assertIsNot(duplicate.data, self.original_cube.data)
        self.assertEqual(duplicate.location, (3.0, 3.0, 4.0))
        self.assertEqual(len(duplicate.data.polygons), len(self.original_cube.data.polygons))
        self.assertIs(self.startup_scene.objects.get("CubeCopy"), duplicate)

    def test_set_dimensions_and_delete_only_explicit_object(self):
        target = self.bpy.data.objects.new("Disposable", self.bpy.data.meshes.new("DisposableMesh"))
        protected = self.bpy.data.objects.new("Protected", self.bpy.data.meshes.new("ProtectedMesh"))
        self.startup_scene.collection.objects.link(target)
        self.startup_scene.collection.objects.link(protected)
        result = self.run_job(
            "set_dimensions", {"object_name": "Disposable", "dimensions": [1, 2, 3]}
        )
        self.assertEqual(result["dimensions"], [1.0, 2.0, 3.0])
        self.assertEqual(target.dimensions, (1.0, 2.0, 3.0))
        deleted = self.run_job("delete_object", {"object_name": "Disposable"})
        self.assertTrue(deleted["deleted"])
        self.assertNotIn("Disposable", self.bpy.data.objects)
        self.assertIs(self.bpy.data.objects["Cube"], self.original_cube)
        self.assertIs(self.bpy.data.objects["Protected"], protected)

    def test_create_test_object_remains_idempotent_and_preserves_default_cube(self):
        first = self.run_job("create_test_object", {})
        second = self.run_job("create_test_object", {}, job_id="different-job")
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(len(self.bpy.data.objects), 3)
        self.assertIs(self.bpy.data.objects["Cube"], self.original_cube)
        self.assertIs(self.startup_scene.objects.get(SMOKE_TEST_OBJECT), self.bpy.data.objects[SMOKE_TEST_OBJECT])

    def test_transforms_change_only_named_startup_scene_object(self):
        unrelated = self.bpy.data.objects.new("Unrelated", FakeMesh("UnrelatedMesh"))
        self.startup_scene.collection.objects.link(unrelated)
        created = self.run_job("create_test_object", {})
        target = self.bpy.data.objects[created["object_name"]]
        move_result = self.run_job("move_object", {"object_name": SMOKE_TEST_OBJECT, "location": [3, 2, 1]}, "move-1")
        self.run_job("rotate_object", {"object_name": SMOKE_TEST_OBJECT, "rotation": [0, 0, 180]}, "rotate-1")
        self.run_job("scale_object", {"object_name": SMOKE_TEST_OBJECT, "scale": [2, 2, 2]}, "scale-1")
        self.assertEqual(move_result["scene_name"], self.startup_scene.name)
        self.assertEqual(target.location, (3.0, 2.0, 1.0))
        self.assertAlmostEqual(target.rotation_euler[2], math.pi)
        self.assertEqual(target.scale, (2.0, 2.0, 2.0))
        self.assertEqual(self.original_cube.location, (0.0, 0.0, 0.0))
        self.assertEqual(self.original_cube.rotation_euler, (0.0, 0.0, 0.0))
        self.assertEqual(self.original_cube.scale, (1.0, 1.0, 1.0))
        self.assertEqual(unrelated.location, (0.0, 0.0, 0.0))
        self.assertEqual(unrelated.rotation_euler, (0.0, 0.0, 0.0))
        self.assertEqual(unrelated.scale, (1.0, 1.0, 1.0))
        self.assertIs(self.bpy.context.scene, self.active_scene)

    def test_captured_startup_scene_is_used_after_active_scene_switch(self):
        self.bpy.context.scene = self.bpy.switched_scene
        self.assertIs(resolve_startup_scene(self.bpy), self.startup_scene)
        result = self.run_job(
            "create_primitive",
            {"primitive_type": "cube", "object_name": "StartupTarget"},
            startup_scene=self.startup_scene,
        )
        target = self.bpy.data.objects["StartupTarget"]
        self.assertEqual(result["scene_name"], "Scene")
        self.assertIs(self.startup_scene.objects.get("StartupTarget"), target)
        self.assertIsNone(self.bpy.switched_scene.objects.get("StartupTarget"))
        self.assertIs(self.bpy.context.scene, self.bpy.switched_scene)
        self.assertIs(self.bpy.data.objects["Cube"], self.original_cube)

    def test_rename_updates_same_object_and_retry_does_not_duplicate(self):
        self.run_job("create_test_object", {})
        original = self.bpy.data.objects[SMOKE_TEST_OBJECT]
        params = {"object_name": SMOKE_TEST_OBJECT, "new_name": "RenamedCube"}
        first = self.run_job("rename_object", params, "rename-1")
        second = self.run_job("rename_object", params, "rename-1")
        self.assertTrue(first["renamed"])
        self.assertEqual(first["scene_name"], self.startup_scene.name)
        self.assertFalse(second["renamed"])
        self.assertIs(self.bpy.data.objects["RenamedCube"], original)
        self.assertIs(self.startup_scene.objects.get("RenamedCube"), original)
        self.assertNotIn(SMOKE_TEST_OBJECT, self.bpy.data.objects)
        self.assertIs(self.bpy.data.objects["Cube"], self.original_cube)

    def test_rename_conflict_does_not_replace_target(self):
        self.run_job("create_test_object", {})
        original = self.bpy.data.objects[SMOKE_TEST_OBJECT]
        collision = self.bpy.data.objects.new("TakenName", FakeMesh("TakenMesh"))
        with self.assertRaises(OperationError) as caught:
            self.run_job("rename_object", {"object_name": SMOKE_TEST_OBJECT, "new_name": "TakenName"})
        self.assertEqual(caught.exception.code, "object_name_conflict")
        self.assertIs(self.bpy.data.objects[SMOKE_TEST_OBJECT], original)
        self.assertIs(self.bpy.data.objects["TakenName"], collision)

    def test_material_creation_assignment_and_retry(self):
        self.run_job("create_test_object", {})
        params = {
            "material_name": "WarmMetal", "object_name": SMOKE_TEST_OBJECT,
            "base_color": [0.8, 0.3, 0.1, 1.0], "roughness": 0.35, "metallic": 0.7,
        }
        first = self.run_job("create_material", params, "material-1")
        obj = self.bpy.data.objects[SMOKE_TEST_OBJECT]
        material = self.bpy.data.materials["WarmMetal"]
        second = self.run_job("create_material", params, "material-1")
        self.assertTrue(first["created"])
        self.assertTrue(first["assigned"])
        self.assertEqual(first["scene_name"], self.startup_scene.name)
        self.assertFalse(second["created"])
        self.assertEqual(obj.data.materials, [material])
        self.assertEqual(material.diffuse_color, tuple(params["base_color"]))
        self.assertEqual(material.node_tree.nodes[0].inputs["Roughness"].default_value, 0.35)
        self.assertEqual(material.node_tree.nodes[0].inputs["Metallic"].default_value, 0.7)

    def test_material_name_conflict_does_not_overwrite_existing_material(self):
        self.run_job("create_test_object", {})
        original = FakeMaterial("ProtectedMaterial")
        original.diffuse_color = (0.1, 0.2, 0.3, 1.0)
        self.bpy.data.materials[original.name] = original
        params = {
            "material_name": "ProtectedMaterial", "object_name": SMOKE_TEST_OBJECT,
            "base_color": [0.8, 0.8, 0.8, 1.0], "roughness": 0.2, "metallic": 0.5,
        }
        with self.assertRaises(OperationError) as caught:
            self.run_job("create_material", params)
        self.assertEqual(caught.exception.code, "material_name_conflict")
        self.assertIs(self.bpy.data.materials["ProtectedMaterial"], original)
        self.assertEqual(original.diffuse_color, (0.1, 0.2, 0.3, 1.0))

    def test_phase_3b_verification_workflow_uses_only_temporary_objects(self):
        source_name = "ABWCheckSource"
        duplicate_name = "ABWCheckCopy"
        material_name = "ABWCheckMaterial"
        source_result = self.run_job(
            "create_primitive",
            {"primitive_type": "cube", "object_name": source_name, "location": [2, 0, 0]},
            "verify-source",
        )
        duplicate_result = self.run_job(
            "duplicate_object",
            {"object_name": source_name, "new_name": duplicate_name, "location": [4, 0, 0]},
            "verify-copy",
        )
        dimensions_result = self.run_job(
            "set_dimensions", {"object_name": duplicate_name, "dimensions": [1, 1.5, 2]},
            "verify-dimensions",
        )
        material_result = self.run_job(
            "create_material", {
                "material_name": material_name, "object_name": duplicate_name,
                "base_color": [0.2, 0.5, 0.8, 1], "roughness": 0.4, "metallic": 0.2,
            },
            "verify-material",
        )
        self.assertTrue(source_result["created"])
        self.assertTrue(duplicate_result["duplicated"])
        self.assertEqual(dimensions_result["dimensions"], [1.0, 1.5, 2.0])
        self.assertTrue(material_result["assigned"])
        self.assertIs(self.bpy.data.objects[duplicate_name].data.materials[0],
                      self.bpy.data.materials[material_name])

        with tempfile.TemporaryDirectory() as directory:
            screenshot = self.run_job(
                "capture_screenshot", {}, "verify-before-cleanup",
                screenshot_directory=directory,
            )
            self.assertTrue(Path(screenshot["screenshot_path"]).is_file())
            deleted_copy = self.run_job("delete_object", {"object_name": duplicate_name}, "verify-delete-copy")
            self.assertTrue(deleted_copy["deleted"])
            self.assertNotIn(duplicate_name, self.bpy.data.objects)
            self.assertIn(source_name, self.bpy.data.objects)
            self.assertIs(self.bpy.data.objects["Cube"], self.original_cube)
            self.run_job("delete_object", {"object_name": source_name}, "verify-delete-source")
            final_screenshot = self.run_job(
                "capture_screenshot", {}, "verify-after-cleanup",
                screenshot_directory=directory,
            )
            self.assertTrue(Path(final_screenshot["screenshot_path"]).is_file())
            self.assertNotIn(source_name, self.bpy.data.objects)
            self.assertIs(self.bpy.data.objects["Cube"], self.original_cube)
        self.assertIn(material_name, self.bpy.data.materials)

    def test_screenshot_is_written_with_metadata_and_render_settings_restored(self):
        scene = self.startup_scene
        render = scene.render
        original = (render.filepath, render.use_file_extension, render.resolution_x,
                    render.resolution_y, render.resolution_percentage, render.display_mode,
                    render.image_settings.file_format, render.image_settings.color_mode,
                    scene.cycles.samples)
        with tempfile.TemporaryDirectory() as directory:
            self.bpy.context.scene = self.bpy.switched_scene
            result = self.run_job(
                "capture_screenshot", {"width": 320, "height": 240},
                job_id="screenshot-1", screenshot_directory=directory,
            )
            screenshot = Path(result["screenshot_path"])
            self.assertTrue(screenshot.is_file())
            self.assertEqual(screenshot.read_bytes(), b"fake-png-image")
            self.assertEqual(result["scene_name"], scene.name)
            self.assertEqual(result["camera_name"], self.bpy.camera.name)
            self.assertEqual((result["width"], result["height"]), (320, 240))
            self.assertEqual(result["format"], "PNG")
            self.assertEqual(result["render_engine"], "CYCLES")
            self.assertEqual(result["size_bytes"], len(b"fake-png-image"))
            self.assertEqual(len(result["sha256"]), 64)
            self.assertEqual(self.bpy.render_operator.calls[-1], {
                "write_still": True, "scene": scene.name,
            })
            self.assertEqual((render.filepath, render.use_file_extension, render.resolution_x,
                              render.resolution_y, render.resolution_percentage, render.display_mode,
                              render.image_settings.file_format, render.image_settings.color_mode,
                              scene.cycles.samples), original)
            self.assertIs(self.bpy.context.scene, self.bpy.switched_scene)

    def test_screenshot_reports_missing_camera_and_render_failure(self):
        scene = self.startup_scene
        with tempfile.TemporaryDirectory() as directory:
            scene.camera = None
            with self.assertRaises(OperationError) as missing:
                self.run_job(
                    "capture_screenshot", {}, "missing-camera",
                    screenshot_directory=directory,
                )
            self.assertEqual(missing.exception.code, "camera_not_found")
            self.assertFalse(self.bpy.render_operator.calls)

            scene.camera = self.bpy.camera
            self.bpy.render_operator.error = RuntimeError("render failed")
            original_render_settings = (
                scene.render.filepath, scene.render.use_file_extension,
                scene.render.resolution_x, scene.render.resolution_y,
                scene.render.resolution_percentage, scene.render.display_mode,
                scene.render.image_settings.file_format,
                scene.render.image_settings.color_mode, scene.cycles.samples,
            )
            with self.assertRaises(OperationError) as failed:
                self.run_job(
                    "capture_screenshot", {}, "render-error",
                    screenshot_directory=directory,
                )
            self.assertEqual(failed.exception.code, "screenshot_render_failed")
            self.assertEqual((
                scene.render.filepath, scene.render.use_file_extension,
                scene.render.resolution_x, scene.render.resolution_y,
                scene.render.resolution_percentage, scene.render.display_mode,
                scene.render.image_settings.file_format,
                scene.render.image_settings.color_mode, scene.cycles.samples,
            ), original_render_settings)
            self.assertFalse(list(Path(directory).iterdir()))

    def test_missing_objects_return_clear_errors_for_targeted_operations(self):
        cases = [
            ("move_object", {"object_name": "Absent", "location": [0, 0, 0]}),
            ("rotate_object", {"object_name": "Absent", "rotation": [0, 0, 0]}),
            ("scale_object", {"object_name": "Absent", "scale": [1, 1, 1]}),
            ("rename_object", {"object_name": "Absent", "new_name": "NewName"}),
            ("create_material", {
                "material_name": "Mat", "object_name": "Absent",
                "base_color": [0, 0, 0, 1], "roughness": 0.5, "metallic": 0,
            }),
        ]
        for operation, params in cases:
            with self.subTest(operation=operation), self.assertRaises(OperationError) as caught:
                self.run_job(operation, params)
            self.assertEqual(caught.exception.code, "object_not_found")
            self.assertIn("Absent", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
