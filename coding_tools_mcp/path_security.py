from __future__ import annotations

from pathlib import PurePosixPath
from typing import Iterable


SENSITIVE_DIR_NAMES = {
    ".git",
    ".ssh",
    ".aws",
    ".gnupg",
    ".kube",
    ".azure",
    ".docker",
    ".terraform.d",
}

SENSITIVE_FILE_NAMES = {
    ".npmrc",
    ".pypirc",
    ".netrc",
    "_netrc",
    ".git-credentials",
    "credentials.json",
    "application_default_credentials.json",
    "credentials.tfrc.json",
}

SENSITIVE_CONFIG_SUBTREES = {
    (".config", "gcloud"),
    (".config", "gh"),
    (".config", "aws"),
    (".config", "azure"),
}


def normalize_path_parts(raw_path: str) -> tuple[str, ...]:
    pure = PurePosixPath(raw_path.replace("\\", "/"))
    return tuple(part for part in pure.parts if part not in {"", "/", "."})


def sensitive_path_reason(parts: Iterable[str]) -> str | None:
    normalized = tuple(str(part) for part in parts if str(part) not in {"", "/", "."})
    lowered = tuple(part.lower() for part in normalized)
    for part in lowered:
        if part in SENSITIVE_DIR_NAMES:
            return f"protected credential/runtime directory: {part}"
        if part == ".env" or part.startswith(".env."):
            if part == ".env.example":
                continue
            return f"protected environment path: {part}"
    for index in range(len(lowered) - 1):
        if (lowered[index], lowered[index + 1]) in SENSITIVE_CONFIG_SUBTREES:
            return f"protected credential config subtree: {lowered[index]}/{lowered[index + 1]}"
    if lowered:
        name = lowered[-1]
        if name in SENSITIVE_FILE_NAMES:
            return f"protected credential file: {name}"
        if name.endswith((".pem", ".key", ".p12", ".pfx")):
            return f"protected key/certificate file: {name}"
    return None


def sensitive_raw_path_reason(raw_path: str) -> str | None:
    return sensitive_path_reason(normalize_path_parts(raw_path))
