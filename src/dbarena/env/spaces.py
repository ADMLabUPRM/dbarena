from typing import Any

from gymnasium import Space


class JSONSpace(Space):
    def __init__(self, seed=None):
        super().__init__(None, seed)

    def contains(self, x: Any) -> bool:
        if x is None or isinstance(x, (bool, int, float, str)):
            return True
        if isinstance(x, list):
            return all(self.contains(item) for item in x)
        if isinstance(x, dict):
            return all(isinstance(k, str) and self.contains(v) for k, v in x.items())
        return False

    def sample(self, mask=None) -> dict:
        return {"tool": "describe_schema", "arguments": {}}

    def __eq__(self, other) -> bool:
        return isinstance(other, JSONSpace)

    def __repr__(self) -> str:
        return "JSONSpace()"

    def __hash__(self):
        return hash("JSONSpace")
