import textwrap
from typing import Iterable, TYPE_CHECKING

from debputy.util import _info

if TYPE_CHECKING:
    from junit_xml import TestSuite, TestCase, to_xml_report_file
else:
    try:
        from junit_xml import TestSuite, TestCase, to_xml_report_file
    except ImportError:
        pass


from debputy.linting.lint_util import (
    LintReport,
    LintDiagnosticResult,
    LintDiagnosticResultState,
    debputy_severity,
)


class JunitLintReport(LintReport):

    def __init__(self, output_filename: str) -> None:
        super().__init__()
        self._output_filename = output_filename

    def finish_report(self) -> None:
        # Nothing to do for now
        all_test_cases = list(self._as_test_cases())
        test_suites = [
            TestSuite(
                "debputy lint",
                test_cases=all_test_cases,
                timestamp=str(self.start_timestamp),
            )
        ]
        with open(self._output_filename, "w", encoding="utf-8") as wfd:
            to_xml_report_file(wfd, test_suites, encoding="utf-8")
        _info(f"Wrote {self._output_filename}")

    def _as_test_cases(self) -> Iterable["TestCase"]:
        for filename, duration in self.durations.items():
            results = self.diagnostics_by_file.get(filename, [])
            yield self._as_test_case(filename, results, duration)

    def _as_test_case(
        self,
        filename: str,
        diagnostic_results: Iterable[LintDiagnosticResult],
        duration: float,
    ) -> "TestCase":
        if not duration:
            duration = 0.000001
        case = TestCase(
            filename,
            # The JUnit schema has `classname` as mandatory
            classname=filename,
            allow_multiple_subelements=True,
            elapsed_sec=duration,
        )
        for diagnostic_result in diagnostic_results:
            if diagnostic_result.result_state == LintDiagnosticResultState.FIXED:
                continue
            diagnostic = diagnostic_result.diagnostic
            severity = debputy_severity(diagnostic)
            if diagnostic_result.is_file_level_diagnostic:
                range_desc = "entire file"
            else:
                range_desc = str(diagnostic.range)
            code = f" [{diagnostic.code}]" if diagnostic.code else ""
            output = textwrap.dedent(
                f"""\
            {filename}{code} ({severity}) {range_desc}: {diagnostic.message}
            """
            )
            case.add_failure_info(
                message=f"[{severity}]: {diagnostic.message}",
                output=output,
            )
        return case
