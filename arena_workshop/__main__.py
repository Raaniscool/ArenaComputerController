"""Module entry point for the restricted Blender CLI and local Drive commands."""

import sys

from . import drive_cli, linear_cli, local_smoke


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "authorize-drive":
        return drive_cli.authorize_main(argv[1:])
    if argv and argv[0] == "upload-screenshot":
        return drive_cli.upload_main(argv[1:])
    if argv and argv[0] == "configure-linear":
        return linear_cli.configure_main(argv[1:])
    if argv and argv[0] == "linear-submit":
        return linear_cli.submit_main(argv[1:])
    if argv and argv[0] == "linear-worker":
        return linear_cli.worker_main(argv[1:])
    return local_smoke.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
