from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class BrowserCapabilities:
    tabs: bool
    accessibility_snapshots: bool
    screenshots: bool
    file_upload: bool
    authenticated_shared_session: bool


class BrowserAdapter(Protocol):
    def capabilities(self) -> BrowserCapabilities: ...
    def observe(self, target: str) -> dict: ...
    def act(self, action: dict) -> dict: ...
    def capture(self, request: dict) -> dict: ...


class DelegateProvider(Protocol):
    def submit(self, request: dict) -> dict: ...
    def collect(self, handle: dict) -> dict: ...
