import enum

import pydantic


class SupportedArches(enum.Enum):
    AMD64 = "amd64"
    AMD64V3 = "amd64v3"
    I386 = "i386"
    ARM64 = "arm64"
    ARMHF = "armhf"
    S390X = "s390x"
    PPC64EL = "ppc64el"
    RISCV64 = "riscv64"


class AddRemoteAction(pydantic.BaseModel):
    arch: SupportedArches = pydantic.Field(description="Architecture of the remote.")
    token: str = pydantic.Field(
        description="LXD client token to connect to the remote."
    )


class SetWorkerCountAction(pydantic.BaseModel):
    arch: SupportedArches = pydantic.Field(description="Architecture to configure.")
    count: int = pydantic.Field(10)
