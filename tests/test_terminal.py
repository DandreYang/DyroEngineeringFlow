from __future__ import annotations

from io import StringIO
import os
import unittest
from unittest.mock import patch

from dyro.terminal import color_enabled, style, title, value


class _TtyBuffer(StringIO):
    def isatty(self) -> bool:
        return True


class TerminalPresentationTests(unittest.TestCase):
    def test_plain_output_is_preserved_for_non_interactive_streams(self) -> None:
        with patch.dict(os.environ, {"DYRO_COLOR": "auto"}, clear=True):
            self.assertFalse(color_enabled(StringIO()))
            self.assertEqual(title("章节", stream=StringIO()), "章节")

    def test_tty_uses_semantic_ansi_roles(self) -> None:
        with patch.dict(
            os.environ, {"DYRO_COLOR": "auto", "TERM": "xterm"}, clear=True
        ):
            self.assertTrue(color_enabled(_TtyBuffer()))
            self.assertEqual(
                value("feat/demo", stream=_TtyBuffer()), "\033[1;36mfeat/demo\033[0m"
            )

    def test_no_color_overrides_a_forced_color_preference(self) -> None:
        with patch.dict(
            os.environ, {"DYRO_COLOR": "always", "NO_COLOR": "1"}, clear=True
        ):
            self.assertFalse(color_enabled(_TtyBuffer()))
            self.assertEqual(style("文本", "title", stream=_TtyBuffer()), "文本")
