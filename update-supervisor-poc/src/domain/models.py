from dataclasses import dataclass
from typing import Optional

@dataclass
class UpdateTarget:
    target_id: str
    name: str
    version: str
    description: str = ""
    length: Optional[int] = None
    uri: str = ""
    kind: str = "application"  # "application" or "os"