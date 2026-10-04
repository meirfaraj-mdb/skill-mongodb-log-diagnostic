"""Guard against accidentally omitting executable Atlas/observability skills."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class VendoredSkillPresenceTest(unittest.TestCase):
    def test_atlas_and_observability_files(self):
        for relative in (
            'skills/mongodb-atlas-logs/SKILL.md',
            'skills/mongodb-atlas-logs/references/config-schema.md',
            'skills/mongodb-atlas-logs/scripts/atlas_logs.py',
            'skills/mongodb-observability/SKILL.md',
            'skills/mongodb-observability/references/config-schema.md',
            'skills/mongodb-observability/scripts/observability.py',
        ):
            with self.subTest(file=relative):
                self.assertTrue((ROOT / relative).is_file(), relative)
                self.assertGreater((ROOT / relative).stat().st_size, 0)

    def test_skill_imports(self):
        import sys
        sys.path.insert(0, str(ROOT))
        from agent import skills
        self.assertTrue(callable(skills.atlas_logs().archive_logs))
        self.assertTrue(callable(skills.observability().collect_query_shapes))


if __name__ == '__main__':
    unittest.main()
