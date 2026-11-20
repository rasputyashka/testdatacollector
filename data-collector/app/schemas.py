"""Version-one wire models, matching data-sender/workload_collector.py."""
from datetime import datetime, timezone
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Counter = Annotated[int, Field(strict=True, ge=0, le=2**64 - 1)]
Text = Annotated[str, Field(strict=True, min_length=1, max_length=4096)]


class WireModel(BaseModel):
    model_config = ConfigDict(extra="allow", allow_inf_nan=False)


class Environment(WireModel):
    type: Literal["bare_metal", "vm", "container", "unknown"]
    tech: Text | None


class CPU(WireModel):
    logical_count: Annotated[int, Field(strict=True, gt=0)] | None
    usage_percent: Annotated[float, Field(strict=True, ge=0, le=100)] | None


class Memory(WireModel):
    total_bytes: Counter
    available_bytes: Counter

    @model_validator(mode="after")
    def consistent(self):
        if self.available_bytes > self.total_bytes:
            raise ValueError("Available memory exceeds total memory")
        return self


class Swap(WireModel):
    total_bytes: Counter
    used_bytes: Counter

    @model_validator(mode="after")
    def consistent(self):
        if self.used_bytes > self.total_bytes:
            raise ValueError("Used swap exceeds total swap")
        return self


class Filesystem(WireModel):
    mountpoint: Text
    fstype: Text | None
    total_bytes: Counter | None
    used_bytes: Counter | None
    available_bytes: Counter | None


class Interface(WireModel):
    interface: Text
    rx_bytes_total: Counter
    tx_bytes_total: Counter


class Report(WireModel):
    schema_version: Annotated[int, Field(strict=True)]
    agent_version: Text
    host_id: UUID
    hostname: Text
    boot_id: UUID | None
    collected_at: datetime
    uptime_seconds: Counter | None
    env: Environment
    cpu: CPU
    memory: Memory | None
    swap: Swap | None
    filesystems: Annotated[list[Filesystem], Field(max_length=1024)] | None
    network: Annotated[list[Interface], Field(max_length=1024)] | None

    @field_validator("schema_version")
    @classmethod
    def version_one(cls, value):
        if value != 1:
            raise ValueError("Unsupported schema version")
        return value

    @field_validator("collected_at", mode="before")
    @classmethod
    def timestamp_string(cls, value):
        if not isinstance(value, str):
            raise ValueError("Expected an ISO 8601 timestamp")
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("Expected an ISO 8601 timestamp") from None

    @field_validator("collected_at")
    @classmethod
    def aware_utc(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Timestamp must include a timezone")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def unique_names(self):
        for entries, key in ((self.network, "interface"), (self.filesystems, "mountpoint")):
            if entries is not None:
                names = [getattr(entry, key) for entry in entries]
                if len(names) != len(set(names)):
                    raise ValueError(f"Duplicate {key}")
        return self
