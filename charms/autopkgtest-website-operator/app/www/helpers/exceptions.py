"""Http exceptions for autopkgtest-web."""

EXAMPLE_URL = (
    "https://autopkgtest.ubuntu.com/request.cgi/?"
    + "release=release&arch=arch&package=pkg&"
    + "trigger=trigger1&trigger=trigger2"
)


class RunningJSONNotFound(FileNotFoundError):
    def exit_code(self):
        return 500


class WebControlException(Exception):
    def __init__(self, message, exit_code):
        super().__init__(message)
        self._code = exit_code

    def exit_code(self):
        return self._code


class LaunchpadUnavailable(WebControlException):
    def __init__(self):
        super().__init__(
            "Launchpad validation is temporarily unavailable. Please try again later.",
            503,
        )


class BadRequest(WebControlException):
    def __init__(self, msg=None):
        if msg is None:
            super().__init__("Bad request - unacceptable passed variables", 400)
        else:
            super().__init__(msg, 400)


class Unauthorized(WebControlException):
    def __init__(self):
        super().__init__("Authorization failure", 401)


class ForbiddenRequest(WebControlException):
    def __init__(self, package, trigger):
        super().__init__(
            (
                "You are not allowed to upload %s or %s to Ubuntu, "
                + "thus you are not allowed to use this service."
            )
            % (package, trigger),
            403,
        )


class NotFound(WebControlException):
    def __init__(self, element_name, element, msg=None):
        if msg is None:
            super().__init__(
                f"{element_name} {element} not found",
                404,
            )
        else:
            super().__init__(
                f"{element_name} {element} {msg}",
                404,
            )


class TooManyRequests(WebControlException):
    def __init__(self, requester):
        super().__init__(
            f"You, {requester}, have requested too many tests. Please try again later.",
            429,
        )


class InvalidArgs(WebControlException):
    def __init__(self, parameters):
        super().__init__(
            (
                "You have passed invalid args: {}\nPlease see an example url below:\n{}".format(
                    ", ".join(parameters.keys()), EXAMPLE_URL
                )
            ),
            400,
        )
