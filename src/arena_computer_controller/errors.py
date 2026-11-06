"""Error types shared by the controller and its HTTP API."""


class ControllerError(Exception):
    """An expected, safe-to-return controller error."""

    def __init__(self, message: str, code: str = "controller_error", status: int = 400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
