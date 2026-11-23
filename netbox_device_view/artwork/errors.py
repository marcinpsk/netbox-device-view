from dataclasses import dataclass

from .limits import MAX_ISSUES


@dataclass(frozen=True)
class Issue:
    """One reason to reject input: the file, the line when one applies, and the message."""

    path: str
    line: int | None
    message: str

    def __str__(self):
        where = self.path if self.line is None else f"{self.path}:{self.line}"
        return f"{where}: {self.message}"


class ArtworkError(ValueError):
    """Input that the import rejects, with every issue found before it stopped."""

    def __init__(self, issues: list[Issue]):
        self.issues = list(issues)[:MAX_ISSUES]
        super().__init__("\n".join(map(str, self.issues)))
