from pathlib import Path
import tempfile
import unittest

from dyro.read_limits import ObservationLimits, ReadBudget, ReadLimitCode, ReadLimitError


class ReadLimitsTests(unittest.TestCase):
    @unittest.skipUnless(
        Path("/var").is_symlink() and Path("/var").resolve() == Path("/private/var"),
        "requires the macOS /var system alias",
    )
    def test_system_alias_descriptor_read_rejects_other_symlink_components(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dyro-read-limits-") as temporary:
            canonical_root = Path(temporary).resolve()
            try:
                root = Path("/var") / canonical_root.relative_to("/private/var")
            except ValueError:
                self.skipTest("temporary directory is not below /private/var")
            directory = root / "state"
            directory.mkdir()
            directory.joinpath("value").write_bytes(b"safe")
            canonical_directory = canonical_root / "state"
            budget = ReadBudget(ObservationLimits())
            for read_root, read_directory in (
                (root, directory),
                (root, canonical_directory),
                (canonical_root, directory),
                (canonical_root, canonical_directory),
            ):
                with self.subTest(root=read_root, directory=read_directory):
                    self.assertEqual(
                        budget.read_regular_bytes_at(
                            root=read_root,
                            directory=read_directory,
                            name="value",
                            maximum_bytes=4,
                            label="test",
                        ),
                        b"safe",
                    )

            link = root / "other-alias"
            link.symlink_to(directory, target_is_directory=True)
            with self.assertRaises(ReadLimitError) as raised:
                budget.read_regular_bytes_at(
                    root=root,
                    directory=link,
                    name="value",
                    maximum_bytes=4,
                    label="test",
                )
            self.assertIs(raised.exception.code, ReadLimitCode.UNSAFE_FILE)
