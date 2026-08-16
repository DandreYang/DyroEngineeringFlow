from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]

IDENTITY = {
    "README.md": "delivery physics engine",
    "README.zh-CN.md": "交付物理引擎",
    "README.ko.md": "전달 물리 엔진",
    "README.es.md": "física de entrega",
    "README.fr.md": "physique de livraison",
    "README.de.md": "Delivery-Physik-Engine",
    "README.pt-BR.md": "física de entrega",
    "README.ru.md": "физики поставки",
}


class ReadmeIdentityTests(unittest.TestCase):
    def test_every_readme_language_locks_delivery_physics(self) -> None:
        found = {path.name for path in ROOT.glob("README*.md")}
        self.assertEqual(found, set(IDENTITY))
        for name, marker in IDENTITY.items():
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertIn(marker, text, msg=name)
            self.assertIn("verify-bundle", text, msg=name)
            self.assertIn("--git-dir", text, msg=name)
            self.assertIn("inconclusive", text, msg=name)
            self.assertNotIn("Symphony", text)
            self.assertNotIn("Gas Town", text)
