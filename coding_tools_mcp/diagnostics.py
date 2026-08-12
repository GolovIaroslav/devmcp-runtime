from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class CodeDiagnostic:
    message: str
    severity: str = "error"
    source: str = "compiler"
    code: str | None = None
    path: str | None = None
    line: int | None = None
    column: int | None = None

    def to_dict(self) -> dict[str, object]:
        return {key: value for key, value in asdict(self).items() if value is not None}


class DiagnosticProvider(Protocol):
    name: str

    def normalize(self, text: str, *, source: str) -> list[CodeDiagnostic]: ...


class CompilerTextProvider:
    """Small format-agnostic compiler/traceback normalizer.

    This is deliberately an optimization layer, not a parser trusted for
    security decisions.  Filesystem authorization remains in Runtime.
    """

    name = "compiler-text"
    _compiler_line = re.compile(
        r"^(?P<path>.+?):(?P<line>\d+)(?::(?P<column>\d+))?:\s*"
        r"(?:(?P<severity>error|warning|note|info)\s*:\s*)?"
        r"(?P<message>.+)$",
        re.IGNORECASE,
    )
    _python_frame = re.compile(
        r'^\s*File\s+["\'](?P<path>.+?)["\'],\s+line\s+(?P<line>\d+)(?:,\s+in\s+(?P<message>.+))?$'
    )

    def normalize(self, text: str, *, source: str) -> list[CodeDiagnostic]:
        result: list[CodeDiagnostic] = []
        for raw_line in text.splitlines():
            line = raw_line.rstrip()
            if not line:
                continue
            match = self._compiler_line.match(line)
            if match is not None:
                groups = match.groupdict()
                severity = (groups.get("severity") or "error").lower()
                result.append(
                    CodeDiagnostic(
                        message=str(groups["message"]),
                        severity=severity,
                        source=source,
                        path=str(groups["path"]),
                        line=int(groups["line"]),
                        column=(
                            int(groups["column"])
                            if groups.get("column") is not None
                            else None
                        ),
                    )
                )
                continue
            match = self._python_frame.match(line)
            if match is not None:
                groups = match.groupdict()
                result.append(
                    CodeDiagnostic(
                        message=str(groups.get("message") or "Python traceback frame"),
                        severity="error",
                        source=source,
                        path=str(groups["path"]),
                        line=int(groups["line"]),
                    )
                )
        return result


class DiagnosticsRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, DiagnosticProvider] = {}
        self.register(CompilerTextProvider())

    def register(self, provider: DiagnosticProvider) -> None:
        self._providers[provider.name] = provider

    def providers(self) -> list[str]:
        return sorted(self._providers)

    def normalize(
        self,
        text: str,
        *,
        source: str = "compiler",
        provider: str = "compiler-text",
        max_results: int = 200,
    ) -> list[dict[str, object]]:
        selected = self._providers.get(provider)
        if selected is None:
            raise KeyError(provider)
        return [
            diagnostic.to_dict()
            for diagnostic in selected.normalize(text, source=source)[:max_results]
        ]


def normalize_diagnostic_path(path: str, *, cwd: Path) -> str:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = cwd / candidate
    return str(candidate.resolve(strict=False))
