"""The one error type every door reports in the same shape."""


class SiteError(Exception):
    def __init__(self, code: str, message: str, hint: str = "", status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.status = status

    def as_dict(self) -> dict:
        err = {"code": self.code, "message": self.message}
        if self.hint:
            err["hint"] = self.hint
        return {"error": err}


def not_found(what: str, hint: str = "") -> SiteError:
    return SiteError("not_found", f"{what} does not exist.", hint, status=404)
