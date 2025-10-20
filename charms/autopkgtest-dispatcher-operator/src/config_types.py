import pydantic
from ops.model import Secret


class DispatcherConfig(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(
        arbitrary_types_allowed=True,
        extra="forbid",
    )

    autopkgtest_git_branch: str
    default_worker_count: int
    releases: str
    swift_auth_url: str
    swift_project_domain_name: str
    swift_project_name: str
    swift_user_domain_name: str
    swift_username: str
    swift_juju_secret: Secret | None = None

    # TODO properly implement validation
    # @pydantic.field_validator("rabbitmq_host")
    # def validate_rabbitmq_host(self, value: str):
    #     err_msg = "rabbitmq_host must be a valid IP address"
    #     parts = value.split(".")
    #     if len(parts) < 4:
    #         raise ValueError(err_msg)
    #     for part in parts:
    #         try:
    #             if int(part) >= 255 or int(part) < 0:
    #                 raise ValueError(err_msg)
    #         except ValueError:
    #             raise ValueError(err_msg)
    #     return value
