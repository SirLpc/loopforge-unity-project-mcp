import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass, field
from itertools import islice
from typing import Any, Iterable

from .errors import ProjectError


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


@dataclass
class Page:
    data: list[dict] = field(default_factory=list)
    truncated: bool = False
    next_cursor: str | None = None
    details: dict = field(default_factory=dict)


class Pagination:
    def __init__(self, commit: str, byte_limit: int):
        self.commit = commit
        self.byte_limit = byte_limit
        self.secret = secrets.token_bytes(32)

    def _signature(self, binding: Any, offset: int) -> str:
        message = json_bytes([self.commit, binding, offset])
        return hmac.new(self.secret, message, hashlib.sha256).hexdigest()[:32]

    def page(
        self, rows: Iterable[dict], limit: int, *, binding: Any = None, cursor: str | None = None,
    ) -> Page:
        offset = 0
        if cursor is not None:
            before, _, signature = cursor.partition(".")
            if not (before.isascii() and before.isdecimal() and len(before) <= 9 and signature.isascii()
                    and hmac.compare_digest(signature, self._signature(binding, int(before)))):
                raise ProjectError("INVALID_CURSOR", "Cursor does not match this query and service baseline")
            offset = int(before)
        results = Page()
        used = 384
        for row in islice(rows, offset, None):
            cost = len(json_bytes(row)) + 1
            if len(results.data) >= limit or used + cost > self.byte_limit:
                if not results.data:
                    raise ProjectError("OUTPUT_LIMIT", "One result exceeds the configured response limit")
                results.truncated = True
                if binding is not None:
                    next_offset = offset + len(results.data)
                    results.next_cursor = f"{next_offset}.{self._signature(binding, next_offset)}"
                break
            results.data.append(row)
            used += cost
        return results
