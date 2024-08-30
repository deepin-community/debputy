from typing import TypedDict, NotRequired, List, Any, Literal, Optional

LintSeverity = Literal["error", "warning", "informational", "pedantic", "spelling"]


class DiagnosticData(TypedDict):
    quickfixes: NotRequired[Optional[List[Any]]]
    lint_severity: NotRequired[Optional[LintSeverity]]
    report_for_related_file: NotRequired[str]
