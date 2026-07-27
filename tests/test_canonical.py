from __future__ import annotations

import unittest

from dyro.canonical import canonical_json_bytes


class CanonicalJsonTests(unittest.TestCase):
    def test_rfc8785_output_is_order_independent_and_normalizes_numbers(self) -> None:
        first = {"z": -0.0, "a": 4.50, "nested": {"b": 2, "a": 1}}
        second = {"nested": {"a": 1, "b": 2}, "a": 4.5, "z": 0}

        self.assertEqual(
            canonical_json_bytes(first),
            b'{"a":4.5,"nested":{"a":1,"b":2},"z":0}',
        )
        self.assertEqual(canonical_json_bytes(first), canonical_json_bytes(second))


if __name__ == "__main__":
    unittest.main()
