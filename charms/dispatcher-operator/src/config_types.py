import pydantic

class DispatcherConfig(pydantic.BaseModel):
    # charm
    default_worker_count: int
    releases: str
    worker_upstream_percentage: int
    stable_release_percentage: int

    # TODO swift

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
