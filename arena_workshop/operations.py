"""Restricted Blender data operations plus one bounded render operator for screenshots."""

import hashlib
import json
import math
import os
from pathlib import Path
from math import cos, pi, sin

from .protocol import JobValidationError

DEFAULT_SCENE_NAME = "Scene"
DEFAULT_CUBE_NAME = "Cube"
SMOKE_TEST_OBJECT = "ArenaWorkshopTestCube"
_TAG_OPERATION = "arena_workshop_operation"
_TAG_JOB_ID = "arena_workshop_job_id"
_TAG_FINGERPRINT = "arena_workshop_fingerprint"


class OperationError(RuntimeError):
    """A safe, structured operation failure suitable for a mailbox result."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def resolve_startup_scene(bpy):
    """Resolve the normal startup scene once, preferring its original default cube."""
    scenes = list(bpy.data.scenes)
    named_startup = bpy.data.scenes.get(DEFAULT_SCENE_NAME)
    if named_startup is not None and named_startup.objects.get(DEFAULT_CUBE_NAME) is not None:
        return named_startup

    cube_scenes = [scene for scene in scenes
                   if scene.objects.get(DEFAULT_CUBE_NAME) is not None]
    if len(cube_scenes) == 1:
        return cube_scenes[0]

    active_scene = getattr(getattr(bpy, "context", None), "scene", None)
    if active_scene is not None and any(scene.name == active_scene.name for scene in cube_scenes):
        return active_scene
    if named_startup is not None:
        return named_startup
    if active_scene is not None:
        return active_scene
    return scenes[0] if scenes else None


def _matches_default_cube_geometry(mesh):
    """Require the untouched eight-vertex, six-quad Blender startup mesh."""
    expected_vertices = {
        (x, y, z)
        for x in (-1.0, 1.0)
        for y in (-1.0, 1.0)
        for z in (-1.0, 1.0)
    }
    vertices = []
    try:
        for vertex in mesh.vertices:
            coordinates = getattr(vertex, "co", vertex)
            point = tuple(float(component) for component in coordinates)
            if len(point) != 3:
                return False
            vertices.append(point)
    except (AttributeError, TypeError, ValueError):
        return False
    if len(vertices) != 8 or set(vertices) != expected_vertices:
        return False

    expected_faces = {
        frozenset(vertex for vertex in expected_vertices if vertex[axis] == side)
        for axis in range(3)
        for side in (-1.0, 1.0)
    }
    actual_faces = set()
    try:
        for polygon in mesh.polygons:
            indices = tuple(getattr(polygon, "vertices", polygon))
            if len(indices) != 4 or any(index < 0 or index >= len(vertices) for index in indices):
                return False
            actual_faces.add(frozenset(vertices[index] for index in indices))
    except (AttributeError, TypeError, ValueError):
        return False
    return len(actual_faces) == 6 and actual_faces == expected_faces


def _has_custom_properties(datablock):
    """Treat unknown custom-property state as customized; deletion must be conservative."""
    try:
        return bool(list(datablock.keys()))
    except (AttributeError, TypeError, RuntimeError):
        return True


def _untouched_default_cube(obj):
    if (getattr(obj, "name", None) != DEFAULT_CUBE_NAME
            or getattr(obj, "type", None) != "MESH"):
        return False
    mesh = getattr(obj, "data", None)
    if mesh is None or getattr(mesh, "name", None) != DEFAULT_CUBE_NAME:
        return False
    if getattr(mesh, "users", None) != 1 or not _matches_default_cube_geometry(mesh):
        return False
    try:
        if len(obj.users_collection) != 1:
            return False
    except (AttributeError, TypeError):
        return False

    defaults = (
        ("location", (0.0, 0.0, 0.0)),
        ("rotation_euler", (0.0, 0.0, 0.0)),
        ("scale", (1.0, 1.0, 1.0)),
    )
    try:
        if any(tuple(float(value) for value in getattr(obj, name)) != expected
               for name, expected in defaults):
            return False
    except (AttributeError, TypeError, ValueError):
        return False

    if (getattr(obj, "rotation_mode", None) != "XYZ"
            or getattr(obj, "parent", None) is not None
            or getattr(obj, "animation_data", None) is not None
            or getattr(mesh, "shape_keys", None) is not None):
        return False
    for name in ("modifiers", "constraints", "children", "material_slots"):
        try:
            if len(getattr(obj, name)) != 0:
                return False
        except (AttributeError, TypeError):
            return False
    for name in ("materials",):
        try:
            if len(getattr(mesh, name)) != 0:
                return False
        except (AttributeError, TypeError):
            return False
    if bool(getattr(obj, "hide_viewport", True)) or bool(getattr(obj, "hide_render", True)):
        return False
    hide_get = getattr(obj, "hide_get", None)
    if callable(hide_get):
        try:
            if hide_get():
                return False
        except Exception:
            return False
    return not _has_custom_properties(obj) and not _has_custom_properties(mesh)


def remove_default_startup_cube(bpy, startup_scene):
    """Remove only Blender's unchanged, unshared startup cube; otherwise do nothing."""
    if startup_scene is None:
        return False
    obj = startup_scene.objects.get(DEFAULT_CUBE_NAME)
    if obj is None or not _untouched_default_cube(obj):
        return False
    if bpy.data.objects.get(DEFAULT_CUBE_NAME) is not obj:
        return False

    linked_scenes = [scene for scene in bpy.data.scenes
                     if scene.objects.get(DEFAULT_CUBE_NAME) is obj]
    if len(linked_scenes) != 1 or linked_scenes[0] is not startup_scene:
        return False

    bpy.data.objects.remove(obj, do_unlink=True)
    return True


def _target_scene(bpy, startup_scene=None):
    scene = startup_scene if startup_scene is not None else resolve_startup_scene(bpy)
    if scene is None:
        raise OperationError("startup_scene_unavailable", "No Blender scene is available for modeling.")
    return scene


def _fingerprint(job):
    payload = json.dumps(
        {"operation": job["operation"], "parameters": job["parameters"]},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _retry_matches(datablock, job):
    stored_job_id = datablock.get(_TAG_JOB_ID)
    if stored_job_id != job["job_id"]:
        return False
    if (datablock.get(_TAG_OPERATION) != job["operation"]
            or datablock.get(_TAG_FINGERPRINT) != _fingerprint(job)):
        raise OperationError(
            "idempotency_conflict",
            "This job ID was already used with different operation data.",
        )
    return True


def _tag(datablock, job):
    datablock[_TAG_OPERATION] = job["operation"]
    datablock[_TAG_JOB_ID] = job["job_id"]
    datablock[_TAG_FINGERPRINT] = _fingerprint(job)


def _find_object(scene, name):
    obj = scene.objects.get(name)
    if obj is None:
        raise OperationError(
            "object_not_found",
            "Object {!r} was not found in scene {}.".format(name, scene.name),
        )
    return obj


def _mesh_data(primitive_type):
    if primitive_type == "cube":
        return (
            [
                (-1.0, -1.0, -1.0), (1.0, -1.0, -1.0),
                (1.0, 1.0, -1.0), (-1.0, 1.0, -1.0),
                (-1.0, -1.0, 1.0), (1.0, -1.0, 1.0),
                (1.0, 1.0, 1.0), (-1.0, 1.0, 1.0),
            ],
            [
                (0, 1, 2, 3), (4, 7, 6, 5), (0, 4, 5, 1),
                (1, 5, 6, 2), (2, 6, 7, 3), (4, 0, 3, 7),
            ],
        )
    if primitive_type == "plane":
        return (
            [(-1.0, -1.0, 0.0), (1.0, -1.0, 0.0),
             (1.0, 1.0, 0.0), (-1.0, 1.0, 0.0)],
            [(0, 1, 2, 3)],
        )
    if primitive_type in {"cylinder", "cone"}:
        segments = 32
        lower = [(cos(2 * pi * i / segments), sin(2 * pi * i / segments), -1.0)
                 for i in range(segments)]
        if primitive_type == "cylinder":
            upper = [(x, y, 1.0) for x, y, _ in lower]
            vertices = lower + upper
            sides = [
                (i, (i + 1) % segments, segments + (i + 1) % segments, segments + i)
                for i in range(segments)
            ]
            return vertices, sides + [
                tuple(reversed(range(segments))),
                tuple(range(segments, segments * 2)),
            ]
        apex_index = segments
        vertices = lower + [(0.0, 0.0, 1.0)]
        sides = [(i, (i + 1) % segments, apex_index) for i in range(segments)]
        return vertices, sides + [tuple(reversed(range(segments)))]
    if primitive_type == "uv_sphere":
        segments = 32
        rings = 16
        vertices = [(0.0, 0.0, 1.0)]
        for ring in range(1, rings):
            phi = pi * ring / rings
            radius = sin(phi)
            z = cos(phi)
            vertices.extend(
                (radius * cos(2 * pi * segment / segments),
                 radius * sin(2 * pi * segment / segments), z)
                for segment in range(segments)
            )
        bottom_index = len(vertices)
        vertices.append((0.0, 0.0, -1.0))
        faces = []
        first_ring = 1
        for segment in range(segments):
            next_segment = (segment + 1) % segments
            faces.append((0, first_ring + segment, first_ring + next_segment))
        for ring in range(rings - 2):
            upper = 1 + ring * segments
            lower = upper + segments
            for segment in range(segments):
                next_segment = (segment + 1) % segments
                faces.append((upper + segment, upper + next_segment,
                              lower + next_segment, lower + segment))
        last_ring = 1 + (rings - 2) * segments
        for segment in range(segments):
            next_segment = (segment + 1) % segments
            faces.append((last_ring + segment, bottom_index, last_ring + next_segment))
        return vertices, faces
    raise OperationError("unsupported_primitive", "The primitive type is not allowed.")


def _remove_created(bpy, obj, mesh):
    if obj is not None:
        try:
            bpy.data.objects.remove(obj, do_unlink=True)
        except Exception:
            pass
    if mesh is not None:
        try:
            bpy.data.meshes.remove(mesh)
        except Exception:
            pass


def _new_mesh_object(bpy, scene, name, primitive_type):
    vertices, faces = _mesh_data(primitive_type)
    mesh = None
    obj = None
    try:
        mesh = bpy.data.meshes.new(name + "Mesh")
        mesh.from_pydata(vertices, [], faces)
        mesh.update()
        obj = bpy.data.objects.new(name, mesh)
        if obj.name != name:
            raise OperationError("object_name_conflict", "Blender could not preserve the requested object name.")
        scene.collection.objects.link(obj)
        return obj, mesh
    except OperationError:
        _remove_created(bpy, obj, mesh)
        raise
    except Exception as exc:
        _remove_created(bpy, obj, mesh)
        raise OperationError(
            "blender_creation_failed",
            "Blender could not create the mesh object ({}).".format(type(exc).__name__),
        ) from exc


def _test_object_result(obj, mesh, created, scene):
    result = {
        "object_name": obj.name,
        "scene_name": scene.name,
        "created": created,
    }
    if created:
        result["vertex_count"] = len(mesh.vertices)
        result["polygon_count"] = len(mesh.polygons)
    else:
        result["message"] = "The test object already exists; no duplicate was created."
    return result


def _create_test_object(bpy, scene):
    existing = scene.objects.get(SMOKE_TEST_OBJECT)
    if existing is not None:
        return _test_object_result(existing, existing.data, False, scene)
    if bpy.data.objects.get(SMOKE_TEST_OBJECT) is not None:
        raise OperationError(
            "object_name_conflict",
            "An object named {} already exists outside scene {}.".format(SMOKE_TEST_OBJECT, scene.name),
        )
    obj, mesh = _new_mesh_object(bpy, scene, SMOKE_TEST_OBJECT, "cube")
    return _test_object_result(obj, mesh, True, scene)


def _create_primitive(bpy, job, scene):
    params = job["parameters"]
    existing = bpy.data.objects.get(params["object_name"])
    if existing is not None:
        existing_in_scene = scene.objects.get(params["object_name"])
        if existing_in_scene is not None and _retry_matches(existing_in_scene, job):
            return {
                "object_name": existing_in_scene.name,
                "scene_name": scene.name,
                "primitive_type": params["primitive_type"],
                "created": False,
                "message": "This job already created the object; no duplicate was made.",
            }
        raise OperationError(
            "object_name_conflict",
            "Object {!r} already exists and will not be replaced.".format(params["object_name"]),
        )

    obj, mesh = _new_mesh_object(bpy, scene, params["object_name"], params["primitive_type"])
    try:
        obj.location = tuple(float(value) for value in params.get("location", (0.0, 0.0, 0.0)))
        obj.rotation_euler = tuple(math.radians(float(value))
                                   for value in params.get("rotation", (0.0, 0.0, 0.0)))
        obj.scale = tuple(float(value) for value in params.get("scale", (1.0, 1.0, 1.0)))
        _tag(obj, job)
    except Exception as exc:
        _remove_created(bpy, obj, mesh)
        raise OperationError(
            "blender_creation_failed",
            "Blender could not initialize the new object ({}).".format(type(exc).__name__),
        ) from exc
    return {
        "object_name": obj.name,
        "scene_name": scene.name,
        "primitive_type": params["primitive_type"],
        "created": True,
        "location": list(params.get("location", (0.0, 0.0, 0.0))),
        "rotation": list(params.get("rotation", (0.0, 0.0, 0.0))),
        "scale": list(params.get("scale", (1.0, 1.0, 1.0))),
        "vertex_count": len(mesh.vertices),
        "polygon_count": len(mesh.polygons),
    }


def _transform_object(job, scene):
    params = job["parameters"]
    obj = _find_object(scene, params["object_name"])
    operation = job["operation"]
    if operation == "move_object":
        values = tuple(float(value) for value in params["location"])
        obj.location = values
        result_field = "location"
    elif operation == "rotate_object":
        degrees = tuple(float(value) for value in params["rotation"])
        obj.rotation_euler = tuple(math.radians(value) for value in degrees)
        values = degrees
        result_field = "rotation"
    else:
        values = tuple(float(value) for value in params["scale"])
        obj.scale = values
        result_field = "scale"
    return {
        "object_name": obj.name,
        "scene_name": scene.name,
        result_field: list(values),
        "updated": True,
    }


def _rename_object(bpy, job, scene):
    params = job["parameters"]
    old_name = params["object_name"]
    new_name = params["new_name"]
    obj = scene.objects.get(old_name)
    if obj is None:
        renamed = scene.objects.get(new_name)
        if renamed is not None and _retry_matches(renamed, job):
            return {"old_name": old_name, "object_name": new_name,
                    "scene_name": scene.name, "renamed": False}
        raise OperationError(
            "object_not_found",
            "Object {!r} was not found in scene {}.".format(old_name, scene.name),
        )
    if old_name == new_name:
        return {"old_name": old_name, "object_name": new_name,
                "scene_name": scene.name, "renamed": False}
    collision = bpy.data.objects.get(new_name)
    if collision is not None:
        raise OperationError(
            "object_name_conflict",
            "Object {!r} already exists and will not be replaced.".format(new_name),
        )
    try:
        obj.name = new_name
        if obj.name != new_name:
            raise OperationError("object_name_conflict", "Blender could not preserve the requested name.")
        _tag(obj, job)
    except OperationError:
        raise
    except Exception as exc:
        raise OperationError(
            "blender_rename_failed",
            "Blender could not rename the object ({}).".format(type(exc).__name__),
        ) from exc
    return {"old_name": old_name, "object_name": new_name,
            "scene_name": scene.name, "renamed": True}


def _set_principled(material, color, roughness, metallic):
    material.diffuse_color = color
    material.roughness = roughness
    material.metallic = metallic
    material.use_nodes = True
    node_tree = material.node_tree
    if node_tree is None:
        return
    nodes = node_tree.nodes
    node = nodes.get("Principled BSDF")
    if node is None:
        node = next((candidate for candidate in nodes
                     if getattr(candidate, "type", None) == "BSDF_PRINCIPLED"), None)
    if node is None:
        raise OperationError(
            "material_node_missing",
            "Blender did not provide a Principled BSDF node for the new material.",
        )
    for socket_name, value in (("Base Color", color), ("Roughness", roughness), ("Metallic", metallic)):
        socket = node.inputs.get(socket_name)
        if socket is not None:
            socket.default_value = value


def _create_material(bpy, job, scene):
    params = job["parameters"]
    obj = _find_object(scene, params["object_name"])
    slots = getattr(obj.data, "materials", None)
    if slots is None:
        raise OperationError(
            "material_assignment_unsupported",
            "The target object does not support mesh material assignment.",
        )

    material = bpy.data.materials.get(params["material_name"])
    created = material is None
    if material is not None:
        if not _retry_matches(material, job):
            raise OperationError(
                "material_name_conflict",
                "Material {!r} already exists and will not be replaced.".format(params["material_name"]),
            )
    else:
        material = bpy.data.materials.new(params["material_name"])
        try:
            if material.name != params["material_name"]:
                raise OperationError("material_name_conflict", "Blender could not preserve the requested material name.")
            color = tuple(float(value) for value in params["base_color"])
            _set_principled(material, color, float(params["roughness"]), float(params["metallic"]))
            _tag(material, job)
        except OperationError:
            try:
                bpy.data.materials.remove(material)
            except Exception:
                pass
            raise
        except Exception as exc:
            try:
                bpy.data.materials.remove(material)
            except Exception:
                pass
            raise OperationError(
                "material_creation_failed",
                "Blender could not create the material ({}).".format(type(exc).__name__),
            ) from exc

    if material not in slots:
        slots.append(material)
    return {
        "material_name": material.name,
        "object_name": obj.name,
        "scene_name": scene.name,
        "base_color": list(params["base_color"]),
        "roughness": params["roughness"],
        "metallic": params["metallic"],
        "created": created,
        "assigned": True,
    }


def _duplicate_object(bpy, job, scene):
    params = job["parameters"]
    source = _find_object(scene, params["object_name"])
    if getattr(source, "type", None) != "MESH" or getattr(source, "data", None) is None:
        raise OperationError(
            "duplicate_unsupported_object",
            "Only mesh objects with mesh data can be duplicated.",
        )

    new_name = params["new_name"]
    existing = bpy.data.objects.get(new_name)
    if existing is not None:
        if scene.objects.get(new_name) is existing and _retry_matches(existing, job):
            return {"object_name": existing.name, "source_name": source.name,
                    "scene_name": scene.name, "duplicated": False}
        raise OperationError(
            "object_name_conflict",
            "Object {!r} already exists and will not be replaced.".format(new_name),
        )

    duplicate = None
    mesh_copy = None
    try:
        duplicate = source.copy()
        mesh_copy = source.data.copy()
        duplicate.data = mesh_copy
        duplicate.name = new_name
        if duplicate.name != new_name:
            raise OperationError("object_name_conflict", "Blender could not preserve the requested object name.")
        scene.collection.objects.link(duplicate)
        if "location" in params:
            duplicate.location = tuple(float(value) for value in params["location"])
        else:
            duplicate.location = (float(source.location[0]) + 1.0,
                                  float(source.location[1]), float(source.location[2]))
        _tag(duplicate, job)
    except OperationError:
        if duplicate is not None:
            bpy.data.objects.remove(duplicate, do_unlink=True)
        if mesh_copy is not None:
            bpy.data.meshes.remove(mesh_copy)
        raise
    except Exception as exc:
        if duplicate is not None:
            bpy.data.objects.remove(duplicate, do_unlink=True)
        if mesh_copy is not None:
            bpy.data.meshes.remove(mesh_copy)
        raise OperationError(
            "blender_duplicate_failed",
            "Blender could not duplicate the mesh object ({}).".format(type(exc).__name__),
        ) from exc
    return {"object_name": duplicate.name, "source_name": source.name,
            "scene_name": scene.name, "duplicated": True,
            "location": list(duplicate.location)}


def _delete_object(bpy, job, scene):
    name = job["parameters"]["object_name"]
    obj = _find_object(scene, name)
    bpy.data.objects.remove(obj, do_unlink=True)
    return {"object_name": name, "scene_name": scene.name, "deleted": True}


def _set_dimensions(job, scene):
    params = job["parameters"]
    obj = _find_object(scene, params["object_name"])
    dimensions = tuple(float(value) for value in params["dimensions"])
    try:
        obj.dimensions = dimensions
    except Exception as exc:
        raise OperationError(
            "dimensions_update_failed",
            "Blender could not set the object dimensions ({}).".format(type(exc).__name__),
        ) from exc
    return {"object_name": obj.name, "scene_name": scene.name,
            "dimensions": list(dimensions), "updated": True}


def _capture_screenshot(bpy, job, scene, screenshot_directory):
    if screenshot_directory is None:
        raise OperationError(
            "screenshot_storage_unavailable",
            "No screenshot output directory was configured.",
        )
    camera = getattr(scene, "camera", None)
    if camera is None or scene.objects.get(camera.name) is None:
        raise OperationError(
            "camera_not_found",
            "The captured startup scene has no linked active camera for rendering.",
        )

    params = job["parameters"]
    width = params.get("width", 640)
    height = params.get("height", 480)
    directory = Path(screenshot_directory)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OperationError(
            "screenshot_storage_failed",
            "Could not prepare the screenshot directory ({}).".format(type(exc).__name__),
        ) from exc

    job_id = job["job_id"]
    final_path = directory / (job_id + ".png")
    temporary_path = directory / ("." + job_id + ".partial.png")
    render = scene.render
    image_settings = render.image_settings
    render_engine = render.engine
    original = {
        "filepath": render.filepath,
        "use_file_extension": render.use_file_extension,
        "resolution_x": render.resolution_x,
        "resolution_y": render.resolution_y,
        "resolution_percentage": render.resolution_percentage,
        "file_format": image_settings.file_format,
        "color_mode": image_settings.color_mode,
    }
    cycles = getattr(scene, "cycles", None)
    original_samples = getattr(cycles, "samples", None) if cycles is not None else None
    original_display_mode = getattr(render, "display_mode", None)
    try:
        render.filepath = str(temporary_path)
        if original_display_mode is not None:
            render.display_mode = "NONE"
        render.use_file_extension = False
        render.resolution_x = width
        render.resolution_y = height
        render.resolution_percentage = 100
        image_settings.file_format = "PNG"
        image_settings.color_mode = "RGBA"
        if cycles is not None and original_samples is not None and render.engine == "CYCLES":
            cycles.samples = min(original_samples, 8)

        status = bpy.ops.render.render(write_still=True, scene=scene.name)
        if status is not None and "FINISHED" not in status:
            raise OperationError("screenshot_render_failed", "Blender cancelled the screenshot render.")
        if not temporary_path.is_file() or temporary_path.stat().st_size == 0:
            raise OperationError("screenshot_render_failed", "Blender produced no screenshot image.")
        os.replace(str(temporary_path), str(final_path))
    except OperationError:
        try:
            temporary_path.unlink()
        except OSError:
            pass
        raise
    except Exception as exc:
        try:
            temporary_path.unlink()
        except OSError:
            pass
        code = "screenshot_storage_failed" if isinstance(exc, OSError) else "screenshot_render_failed"
        raise OperationError(
            code,
            "Could not capture the Blender scene screenshot ({}).".format(type(exc).__name__),
        ) from exc
    finally:
        render.filepath = original["filepath"]
        render.use_file_extension = original["use_file_extension"]
        render.resolution_x = original["resolution_x"]
        render.resolution_y = original["resolution_y"]
        render.resolution_percentage = original["resolution_percentage"]
        image_settings.file_format = original["file_format"]
        image_settings.color_mode = original["color_mode"]
        if original_display_mode is not None:
            render.display_mode = original_display_mode
        if cycles is not None and original_samples is not None:
            cycles.samples = original_samples

    try:
        screenshot_bytes = final_path.read_bytes()
    except OSError as exc:
        raise OperationError(
            "screenshot_storage_failed",
            "Could not read the completed screenshot ({}).".format(type(exc).__name__),
        ) from exc
    digest = hashlib.sha256(screenshot_bytes).hexdigest()
    return {
        "scene_name": scene.name,
        "camera_name": camera.name,
        "screenshot_path": str(final_path.resolve()),
        "format": "PNG",
        "render_engine": render_engine,
        "width": width,
        "height": height,
        "size_bytes": len(screenshot_bytes),
        "sha256": digest,
    }


def execute_job(bpy, job, startup_scene=None, screenshot_directory=None):
    """Execute one validated job using Blender's main-thread APIs."""
    operation = job["operation"]
    params = job["parameters"]
    if operation == "hello_test":
        active_scene = getattr(getattr(bpy, "context", None), "scene", None)
        return {
            "message": params["message"],
            "blender_version": bpy.app.version_string,
            "active_scene": active_scene.name if active_scene else None,
        }

    scene = _target_scene(bpy, startup_scene)
    if operation == "create_test_object":
        return _create_test_object(bpy, scene)
    if operation == "create_primitive":
        return _create_primitive(bpy, job, scene)
    if operation in {"move_object", "rotate_object", "scale_object"}:
        return _transform_object(job, scene)
    if operation == "rename_object":
        return _rename_object(bpy, job, scene)
    if operation == "create_material":
        return _create_material(bpy, job, scene)
    if operation == "duplicate_object":
        return _duplicate_object(bpy, job, scene)
    if operation == "delete_object":
        return _delete_object(bpy, job, scene)
    if operation == "set_dimensions":
        return _set_dimensions(job, scene)
    if operation == "capture_screenshot":
        return _capture_screenshot(bpy, job, scene, screenshot_directory)
    raise JobValidationError("unsupported_operation")
