from __future__ import annotations

from dataclasses import dataclass

ROLE_RANK = {"viewer": 1, "editor": 2, "admin": 3}


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: str
    org_id: str
    role: str
    email: str

    def at_least(self, role: str) -> bool:
        return ROLE_RANK.get(self.role, 0) >= ROLE_RANK[role]
