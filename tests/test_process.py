from pathlib import Path
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

from dyro.process import Result, git_read, run
from dyro.read_limits import (
    ObservationLimits,
    ReadBudget,
    ReadLimitCode,
    ReadLimitError,
    bounded_directory_names,
)


class BoundedProcessTests(unittest.TestCase):
    def test_directory_enumeration_stops_at_record_limit_without_listdir(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("a", "b", "c"):
                root.joinpath(name).touch()
            descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with (
                    patch("dyro.read_limits.os.listdir", side_effect=AssertionError),
                    self.assertRaises(ReadLimitError) as raised,
                ):
                    bounded_directory_names(
                        descriptor,
                        ReadBudget(ObservationLimits()),
                        maximum_records=2,
                        label="test",
                    )
            finally:
                os.close(descriptor)

        self.assertIs(
            raised.exception.code,
            ReadLimitCode.RECORD_LIMIT_EXCEEDED,
        )

    def test_bounded_run_stops_when_output_exceeds_budget(self) -> None:
        with self.assertRaises(ReadLimitError) as raised:
            run(
                (sys.executable, "-c", "print('x' * 4096)"),
                timeout=2,
                maximum_output_bytes=128,
            )

        self.assertIs(
            raised.exception.code,
            ReadLimitCode.AGGREGATE_BYTES_EXCEEDED,
        )

    def test_git_read_uses_remaining_deadline_and_charges_output(self) -> None:
        budget = ReadBudget(ObservationLimits())
        with patch(
            "dyro.process.run",
            return_value=Result(("git",), 0, "ok\n", 3),
        ) as observed_run:
            result = git_read(Path("/tmp/repo"), "status", read_budget=budget)

        self.assertEqual(result.stdout, "ok\n")
        self.assertEqual(budget.bytes_read, 3)
        call = observed_run.call_args
        self.assertLessEqual(call.kwargs["timeout"], 5.0)
        self.assertEqual(
            call.kwargs["maximum_output_bytes"],
            64 * 1024 * 1024,
        )


if __name__ == "__main__":
    unittest.main()
