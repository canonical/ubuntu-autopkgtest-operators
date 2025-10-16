from typing import List

import pydantic


class JanitorConfig(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(
        extra="forbid",
    )

    @pydantic.field_validator("extra_releases", mode="before")
    def split_space_separated(cls, v):
        return v.split()

    autopkgtest_git_branch: str
    extra_releases: List[str]
    mirror: str
    max_containers: int
    max_virtual_machines: int
