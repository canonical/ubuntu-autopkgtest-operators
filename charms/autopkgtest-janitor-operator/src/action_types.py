import pydantic


class AddWorkerAction(pydantic.BaseModel):
    arch: str = pydantic.Field(description="Architecture of the worker.")
    token: str = pydantic.Field(description="LXD client token to connect to a remote.")


class RemoveWorkerAction(pydantic.BaseModel):
    arch: str = pydantic.Field(description="Architecture of the worker.")
