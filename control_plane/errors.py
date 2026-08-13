class ControlPlaneError(Exception):
    """Base error carrying an HTTP-friendly status and code."""

    def __init__(self, message: str, *, status: int = 400, code: str = "bad_request"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


class NotFoundError(ControlPlaneError):
    def __init__(self, message: str):
        super().__init__(message, status=404, code="not_found")


class ConflictError(ControlPlaneError):
    def __init__(self, message: str):
        super().__init__(message, status=409, code="conflict")


class ValidationError(ControlPlaneError):
    def __init__(self, message: str):
        super().__init__(message, status=422, code="validation_error")
