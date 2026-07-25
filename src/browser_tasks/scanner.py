from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path


# Case-insensitive: the default macOS volume is case-insensitive, so `.ENV`
# and `TASKS/x` reach the same bytes as their lowercase spelling.
EXCLUDED_ROOTS = ("tasks/", "archive/")
DENIED_NAMES = re.compile(
    r"""(^|/)(
        \.env(?!\.example$)(\..*)?
      | \.config\.ya?ml
      | \.netrc
      | \.npmrc
      | \.pypirc
      | \.git-credentials
      | credentials(\.json|\.yaml|\.yml)?
      | id_[a-z0-9]+
      | [^/]+\.(pem|key|p12|pfx|jks|keystore|p8|ppk|kdbx)
    )$""",
    re.IGNORECASE | re.VERBOSE,
)
PATTERNS = {
    "private_key": re.compile(rb"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY"),
    "authorization_header": re.compile(rb"(?im)^authorization:\s*(?:bearer|basic)\s+\S+"),
    "cookie_header": re.compile(rb"(?im)^(?:cookie|set-cookie):\s*\S+"),
    "credential_url": re.compile(rb"https?://[^/\s:@]+:[^/\s@]+@"),
    "api_token": re.compile(
        rb"(?i)(?:api[_-]?key|access[_-]?key|secret[_-]?access[_-]?key|token|secret|password)"
        rb"[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9_\-/+]{20,}"
    ),
}


@dataclass(frozen=True)
class Finding:
    path: str
    kind: str


def is_excluded_path(relative: str) -> bool:
    lowered = relative.casefold()
    return lowered.startswith(EXCLUDED_ROOTS) or any(
        part == ".git" for part in Path(lowered).parts
    )


def scan_file(root: Path, path: Path, *, max_bytes: int = 5_000_000) -> tuple[Finding, ...]:
    trusted_root = root.resolve()
    relative = path.relative_to(root).as_posix()
    findings: list[Finding] = []
    if is_excluded_path(relative):
        findings.append(Finding(relative, "task_material"))
    if DENIED_NAMES.search(relative):
        findings.append(Finding(relative, "denied_name"))
    current = trusted_root
    for part in Path(relative).parts:
        current = current / part
        if current.is_symlink():
            return tuple(findings + [Finding(relative, "unsupported_type")])
    # No component is a symlink at this point, so comparing resolved forms
    # catches a caller that passed a path outside the trusted root.
    if current.resolve() != path.resolve() or not path.is_file():
        return tuple(findings + [Finding(relative, "unsupported_type")])
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            return tuple(findings + [Finding(relative, "unsupported_type")])
        if opened.st_size > max_bytes:
            return tuple(findings + [Finding(relative, "size_limit")])
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


def scan_paths(
    root: Path, relatives: tuple[str, ...], *, max_bytes: int = 5_000_000
) -> tuple[Finding, ...]:
    """Scan an explicit list of repository-relative paths under one root."""

    findings: list[Finding] = []
    for relative in relatives:
        candidate = Path(relative)
        if candidate.is_absolute() or ".." in candidate.parts:
            findings.append(Finding(relative, "unsupported_path"))
            continue
        findings.extend(scan_file(root, root / candidate, max_bytes=max_bytes))
    return tuple(findings)
