from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .models import BrowserAction, BrowserObservation, DelegationRequest, DelegationResponse


@dataclass(frozen=True)
class BrowserCapabilities:
    tabs: bool
    accessibility_snapshots: bool
    screenshots: bool
    file_upload: bool
    authenticated_shared_session: bool


class BrowserAdapter(Protocol):
    @property
    def adapter_id(self) -> str: ...
    def capabilities(self) -> BrowserCapabilities: ...
    def claim(self, task_id: str) -> tuple[str, ...]: ...
    def observe(self, task_id: str, target: str) -> BrowserObservation: ...
    def act(self, action: BrowserAction) -> BrowserObservation: ...
    def capture(self, task_id: str, action_id: str | None) -> BrowserObservation: ...


class DelegateProvider(Protocol):
    @property
    def provider_id(self) -> str: ...
    def submit(self, request: DelegationRequest) -> str: ...
    def collect(self, handle: str) -> DelegationResponse: ...
