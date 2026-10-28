class EngineError(Exception):
    """An error that can be returned by the REST interface."""

    def __init__(self, message, *, status=400, code="invalid_request"):
        super().__init__(message)
        self.status = status
        self.code = code


class CorruptLogError(EngineError):
    def __init__(self, message):
        super().__init__(message, status=503, code="corrupt_event_log")
