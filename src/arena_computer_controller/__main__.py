"""Command-line entry point: ``python -m arena_computer_controller``."""

from __future__ import annotations

import argparse
import ipaddress
import os
import sys

from . import __version__
from .backend import SystemDesktopBackend
from .controller import ComputerController
from .integrations.blender import BlenderMailboxAdapter
from .server import create_server


def _is_loopback_host(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        # Treat arbitrary hostnames and wildcard binds as externally reachable.
        return False


def _format_startup_report(report: dict) -> str:
    state = report.get("state", {})
    input_map = report.get("input_map", {})
    capabilities = report.get("capabilities", {})
    os_info = state.get("platform", {})
    os_name = " ".join(
        str(part) for part in (os_info.get("system"), os_info.get("release")) if part
    ) or "Unknown"
    active = state.get("active_window")
    active_name = active.get("application", "Unknown") if active else "None detected"
    applications = state.get("running_applications", [])
    visible_app_names = sorted(
        {item.get("application", "Unknown") for item in state.get("windows", []) if item.get("application")},
        key=str.casefold,
    )
    background_names = [item.get("name", "Unknown") for item in applications if not item.get("has_window")]
    integrations = state.get("integrations", [])

    lines = [
        "COMPUTER STARTUP REPORT",
        f"Operating system: {os_name}",
        f"Active application: {active_name}",
        f"Visible windows: {len(state.get('windows', []))}",
        "Applications with visible windows: " + (", ".join(visible_app_names) if visible_app_names else "None detected"),
        "Background process names without a visible window: "
        + (", ".join(background_names[:12]) if background_names else "None detected"),
        f"Discovered interactive inputs: {input_map.get('count', 0)} (source: {input_map.get('source', 'unknown')})",
        "Application integrations:",
    ]
    for integration in integrations:
        detected = "detected" if integration.get("application_detected") else "not detected"
        lines.append(f"  - {integration.get('name')}: {detected}; connected={integration.get('connected', False)}")
    if not integrations:
        lines.append("  - None loaded")
    visual_status = "available on request" if capabilities.get("screenshots") else "not available on this host/configuration"
    lines.append(f"Screenshots: {visual_status}; no screenshot is sent automatically")
    for warning in input_map.get("warnings", []):
        lines.append(f"Warning: {warning}")
    if not state.get("platform_supported"):
        lines.append("Note: Windows desktop control is not active on this operating system.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the local ArenaComputerController API.")
    parser.add_argument("--host", default=os.environ.get("ARENA_CONTROLLER_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("ARENA_CONTROLLER_PORT", "8765")))
    parser.add_argument("--version", action="version", version=f"ArenaComputerController {__version__}")
    args = parser.parse_args(argv)

    token = os.environ.get("ARENA_CONTROLLER_TOKEN")
    if not _is_loopback_host(args.host) and not token:
        parser.error("non-loopback binds require ARENA_CONTROLLER_TOKEN to be set")
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")

    backend = SystemDesktopBackend()
    controller = ComputerController(backend)
    try:
        report = controller.startup_report()
    except Exception as exc:
        print(f"Startup discovery warning: {type(exc).__name__}", file=sys.stderr)
        report = {"state": {}, "input_map": {"count": 0, "source": "unavailable", "warnings": []}, "capabilities": backend.capabilities()}

    print(_format_startup_report(report), flush=True)
    blender_adapter = None
    connector_root = os.environ.get("ARENA_BLENDER_WORKSHOP_PATH")
    if connector_root:
        try:
            blender_adapter = BlenderMailboxAdapter(connector_root)
            print(f"Blender mailbox adapter: configured from {connector_root}", flush=True)
        except Exception as exc:
            print(f"Blender mailbox adapter: unavailable ({type(exc).__name__})", file=sys.stderr, flush=True)
    else:
        print("Blender mailbox adapter: not configured (set ARENA_BLENDER_WORKSHOP_PATH)", flush=True)
    server = create_server(controller, args.host, args.port, token, blender_adapter=blender_adapter)
    actual_host, actual_port = server.server_address[:2]
    auth_note = "bearer token enabled" if token else "loopback only; no bearer token configured"
    print(f"Controller API listening at http://{actual_host}:{actual_port} ({auth_note}).", flush=True)
    print(
        "Endpoints: GET /health, /v1/state, /v1/inputs, /v1/screenshot; "
        "POST /v1/actions, /v1/refresh, /v1/integrations/blender/probe, "
        "/v1/integrations/blender/commands; GET /v1/integrations/blender/jobs/{job_id}/screenshot",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping controller.", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
