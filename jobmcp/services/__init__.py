class ServiceError(Exception):
    """Expected, user-facing failure. `code` maps to an HTTP status in the
    API."""

    def __init__(self, message: str, code: int = 400):
        super().__init__(message)
        self.code = code
