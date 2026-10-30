from dataclasses import dataclass


@dataclass(frozen=True)
class Order:
    id: str
    owner: str
    status: str = "open"
