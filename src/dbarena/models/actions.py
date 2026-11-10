from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ActionCall:
    tool: str
    arguments: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"tool": self.tool, "arguments": dict(self.arguments)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ActionCall":
        if not isinstance(data, dict) or "tool" not in data:
            raise ValueError("action call requires a 'tool' field")
        return cls(tool=str(data["tool"]), arguments=dict(data.get("arguments", {})))
