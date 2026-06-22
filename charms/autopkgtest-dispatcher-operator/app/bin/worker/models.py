from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass

PACKAGE_NAME_RE = r"^[a-zA-Z0-9+.-]+$"
TIMEOUTS = {
    "short": 300,
    "install": 3000,
    "test": 10000,
    "copy": 900,
    "build": 20000,
}
VM_ARCHES = ["amd64", "amd64v3", "s390x"]


def get_package_hash(package: str) -> str:
    """Generate a package hash, which is the first letter of the package name or lib + first letter.

    This is mostly meant for backwards compatibility with debci standards.
    """
    if package.startswith("lib"):
        return package[:4]
    return package[0]


class PPA:
    """Represents a PPA specified in a test request, which may optionally contain embedded credentials and a fingerprint."""

    user: str
    name: str
    creds_user: str | None
    creds_pass: str | None
    fingerprint: str | None

    def __init__(self, ppa_str: str):
        try:
            creds, _, url = ppa_str.rpartition("@")
            url, _, fingerprint = url.partition(":")
            creds_user, creds_pass = creds.split(":", 1) if creds else (None, None)

            user, name = url.split("/", 1)
        except ValueError:
            raise ValueError(
                f"invalid ppa format: {ppa_str}, must be [user:token]@lpuser/ppa_name[:fingerprint]"
            )

        self.user = user
        self.name = name
        self.creds_user = creds_user
        self.creds_pass = creds_pass
        self.fingerprint = fingerprint

    def is_private(self) -> bool:
        """Return ``True`` if the PPA contains credentials."""
        return bool(self.creds_user)

    def __str__(self) -> str:
        """Return the PPA in an autopkgtest-usable format."""
        prefix = f"{self.creds_user}:{self.creds_pass}@" if self.is_private() else ""
        suffix = f":{self.fingerprint}" if self.fingerprint else ""
        return f"{prefix}{self.user}/{self.name}{suffix}"


class Request:
    """Represents a test request received by the worker."""

    package: str
    arch: str
    env: list[str]
    release: str
    triggers: list[str]
    all_proposed: bool
    ppas: list[PPA]
    requester: str
    readable_by: list[str]
    swiftuser: str
    submit_time: str
    testname: str
    test_git: str
    build_git: str

    # the run id should be generated once at initialization time
    run_id: str

    # classifications
    is_big_pkg: bool
    is_long_test: bool
    is_vm_pkg: bool
    is_never_run: bool

    def __init__(self, **kwargs):
        """Initialize a request from a dict of parameters and validate them. Raises ``ValueError`` if any required parameters are invalid."""
        self.package = kwargs.get("package")
        if not self.package or not re.match(PACKAGE_NAME_RE, self.package):
            raise ValueError(f"invalid package name: {self.package}")
        self.arch = kwargs.get("arch")
        self.env = kwargs.get("env") or []
        self.release = kwargs.get("release")
        self.triggers = kwargs.get("triggers") or []
        self.requester = kwargs.get("requester", "")
        self.readable_by = kwargs.get("readable-by") or []
        self.testname = kwargs.get("testname", "")
        self.swiftuser = kwargs.get("swiftuser", "")

        # be mindful of _ vs - here
        self.all_proposed = "all-proposed" in kwargs
        self.test_git = kwargs.get("test-git", "")
        self.build_git = kwargs.get("build-git", "")
        self.submit_time = kwargs.get("submit-time", "")

        self.ppas = [PPA(ppa_str) for ppa_str in kwargs.get("ppas") or []]
        self.is_big_pkg = kwargs.get("is_big_pkg", False)
        self.is_long_test = kwargs.get("is_long_test", False)
        self.is_vm_pkg = kwargs.get("is_vm_pkg", False)
        self.is_never_run = kwargs.get("is_never_run", False)

        # a request carrying ppa creds without a swiftuser is either impossible to read
        # or would leak private test information, so reject it
        if any(ppa.is_private() for ppa in self.ppas) and not self.swiftuser:
            raise ValueError("a request with private ppas must also have a swiftuser")

        self.run_id = self._get_run_id()

    def __str__(self) -> str:
        """Return a deterministic string representation of the request, as it is used to generate the ``run_id``."""
        body_str = " ".join(
            f"{k}={v}" for k, v in sorted(vars(self).items()) if k != "run_id"
        )
        return body_str

    def is_private(self) -> bool:
        return bool(self.swiftuser)

    def get_skip_reason(self) -> str | None:
        """Human readable reason for why this request should be skipped, or ``None`` if it should run."""
        if self.is_never_run:
            return "This package is marked to never run. To get the entry removed, contact a member of the Ubuntu Release Team."
        return None

    def get_container_name(self) -> str:
        """Generate a swift container name for this request."""
        container = f"autopkgtest-{self.release}"
        if self.ppas:
            # for backwards compatibility, we only use the last ppa specified
            container += f"-{self.ppas[-1].user}-{self.ppas[-1].name}"
        if self.is_private():
            container = f"private-{container}"
        return container

    def get_swift_dir(self) -> str:
        """Generate a swift path for this request's artifacts."""
        return f"{self.release}/{self.arch}/{get_package_hash(self.package)}/{self.package}/{self.run_id}"

    def _get_run_id(self) -> str:
        """Generate a run id for this request, which is a timestamp plus a hash of the request body for uniqueness."""
        timestamp = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
        self_hash = hashlib.sha1(str(self).encode()).hexdigest()[:5]
        return f"{timestamp}_{self_hash}@"

    def compute_timeouts(self) -> dict[str, int]:
        """Compute timeouts for this request based on its classifications, with special adjustments for ``riscv64``."""
        timeouts = TIMEOUTS.copy()
        if self.arch == "riscv64":
            # xypron suggested a factor of 4 for riscv64
            timeouts = {k: v * 4 for k, v in timeouts.items()}
        if self.is_long_test:
            timeouts["test"] *= 4
            timeouts["build"] *= 2
        elif self.is_big_pkg:
            timeouts["test"] *= 2

        return timeouts

    def get_params(self) -> dict[str, str | list[str]]:
        """Backwards compatible dict of request parameters for status updates."""
        params = {}
        if self.requester:
            params["requester"] = self.requester
        if self.submit_time:
            params["submit-time"] = self.submit_time
        if self.triggers:
            params["triggers"] = list(self.triggers)
        if self.all_proposed:
            params["all-proposed"] = "1"
        if self.test_git:
            params["test-git"] = self.test_git
        if self.build_git:
            params["build-git"] = self.build_git
        if self.ppas:
            params["ppas"] = [str(p) for p in self.ppas]
        if self.env:
            params["env"] = list(self.env)
        if self.testname:
            params["testname"] = self.testname
        return params

    def build_args(self) -> list[str]:
        """Build a list of command line arguments to invoke autopkgtest with for this request."""
        args = []

        # i386 is special and must have its test arch invoked explicitly
        if self.arch == "i386":
            args += [f"--test-architecture={self.arch}"]

        args += ["--apt-upgrade"]

        # install triggering packages from proposed pocket unless
        # the only trigger is migration-reference/0
        # or all-proposed is enabled, in which case install everything from proposed
        if not self.test_git and (not self.ppas or self.all_proposed):
            pocket_arg = "--apt-pocket=proposed"
            if not self.all_proposed:
                triggers = [
                    "src:" + t.split("/")[0]
                    for t in self.triggers
                    if t != "migration-reference/0"
                ]
                if triggers:
                    pocket_arg += "=" + ",".join(triggers)
                else:
                    pocket_arg = ""
            if pocket_arg:
                args += [pocket_arg]

        # add the package name to test, unless it is a git test
        # in which case we want to build the package
        if self.test_git:
            test_args = ["--no-built-binaries", self.test_git]
        elif self.build_git:
            test_args = [self.build_git]
        else:
            test_args = [self.package]

        args += test_args

        for ppa in self.ppas:
            args += [f"--add-apt-source=ppa:{str(ppa)}"]

        for t, v in self.compute_timeouts().items():
            args += [f"--timeout-{t}={v}"]

        if self.triggers:
            args += [f"--env=ADT_TEST_TRIGGERS={' '.join(self.triggers)}"]

        for e in self.env:
            args += [f"--env={e}"]

        if self.testname:
            args += [f"--testname={self.testname}"]

        return args


@dataclass
class Result:
    """Represents the result of a test request."""

    request: Request
    container: str
    run_id: str
    duration: int
    exitcode: int

    # these fields only need to be populated manually if we need to mock a result
    testinfo: dict | None = None
    testpkg_version: str | None = None
    logs: str | None = None

    @staticmethod
    def mock_result(request: Request, log_msg: str) -> Result:
        """Generate a mock result for a request that is being skipped or failed early, with a given log message explaining the reason for the skip/failure."""
        logging.warning(
            f"skipping test {request.package}/{request.arch}/{request.release} with reason '{log_msg}'"
        )
        mock_result = Result(
            request=request,
            container=request.get_container_name(),
            run_id=request.run_id,
            duration=0,
            exitcode=99,
            testinfo={
                "custom_environment": [
                    f"ADT_TEST_TRIGGERS={' '.join(request.triggers)}"
                ]
            },
            testpkg_version=f"{request.package} blacklisted",
            logs=log_msg,
        )
        return mock_result
