import pydantic
from ops.model import Secret


class JanitorConfig(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(
        extra="forbid",
    )

    @pydantic.field_validator("releases", mode="before")
    def split_space_separated(cls, v):
        return v.split()

    autopkgtest_git_branch: str
    releases: list[str]
    mirror: str
    max_instances: int
    swift_auth_url: str
    swift_project_domain_name: str
    swift_project_name: str
    swift_user_domain_name: str
    swift_username: str
    swift_juju_secret: Secret | None = None
