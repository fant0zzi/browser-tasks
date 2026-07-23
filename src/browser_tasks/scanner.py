from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


DENIED_NAMES = re.compile(r"(^|/)(\.env(?:\..*)?|\.config\.ya?ml|[^/]+\.(?:pem|key))$")
PATTERNS = {
    "private_key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "authorization_header": re.compile(rb"(?im)^authorization:\s*(?:bearer|basic)\s+\S+"),
    "cookie_header": re.compile(rb"(?im)^(?:cookie|set-cookie):\s*\S+"),
    "credential_url": re.compile(rb"https?://[^/\s:@]+:[^/\s@]+@"),
    "api_token": re.compile(rb"(?i)(?:api[_-]?key|token|secret)\s*[:=]\s*[\"']?[A-Za-z0-9_\-]{20,}"),
}


@dataclass(frozen=True)
class Finding:
    path: str
    kind: str


def scan_file(root: Path, path: Path, *, max_bytes: int = 5_000_000) -> tuple[Finding, ...]:
    relative = path.relative_to(root).as_posix()
    findings: list[Finding] = []
    if relative.startswith("tasks/"):
        findings.append(Finding(relative, "task_material"))
    if DENIED_NAMES.search(relative):
        findings.append(Finding(relative, "denied_name"))
    if path.is_symlink() or not path.is_file():
        return tuple(findings + [Finding(relative, "unsupported_type")])
    if path.stat().st_size > max_bytes:
        return tuple(findings + [Finding(relative, "size_limit")])
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        data = os.read(fd, max_bytes + 1)
    finally:
        os.close(fd)
    if b"\0" in data:
        findings.append(Finding(relative, "binary_denied"))
        return tuple(findings)
    for kind, pattern in PATTERNS.items():
        if pattern.search(data):
            findings.append(Finding(relative, kind))
    return tuple(findings)
