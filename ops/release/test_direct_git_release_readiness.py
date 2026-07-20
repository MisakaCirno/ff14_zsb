from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("Test-DirectGitReleaseReadiness.py")
SPEC = importlib.util.spec_from_file_location("direct_git_release_readiness", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Could not load the direct Git readiness module.")
readiness = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(readiness)


class DirectGitReleaseReadinessTests(unittest.TestCase):
    def test_environment_parser_keeps_values_internal_and_reports_structure(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            environment_path = Path(temporary_directory) / "production.env"
            environment_path.write_text(
                "\ufeff# comment\n"
                "APP_ENV=production\n"
                "SECRET_KEY='secret=value#literal'\n"
                'ALLOWED_HOSTS="example.com,127.0.0.1"\n'
                "DEBUG=False # safe inline comment\n",
                encoding="utf-8",
            )

            values, duplicates, malformed = readiness.parse_environment_file(
                environment_path
            )

        self.assertEqual(
            sorted(values),
            ["ALLOWED_HOSTS", "APP_ENV", "DEBUG", "SECRET_KEY"],
        )
        self.assertEqual(values["SECRET_KEY"], "secret=value#literal")
        self.assertEqual(values["DEBUG"], "False")
        self.assertEqual(duplicates, [])
        self.assertEqual(malformed, [])

    def test_environment_parser_identifies_duplicate_and_malformed_lines(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            environment_path = Path(temporary_directory) / "production.env"
            environment_path.write_text(
                "APP_ENV=production\n"
                "APP_ENV=test\n"
                "NOT AN ASSIGNMENT\n"
                'SECRET_KEY="unterminated\n',
                encoding="utf-8",
            )

            _, duplicates, malformed = readiness.parse_environment_file(
                environment_path
            )

        self.assertEqual(duplicates, ["APP_ENV"])
        self.assertEqual(malformed, [3, 4])

    def test_runtime_dirty_paths_are_blocking_but_local_notes_are_not(self):
        blocking = (
            "ffxivshare/settings.py",
            "frontend/src/main.ts",
            "ops/release/tool.ps1",
            "shares/models.py",
            "static/app/manifest.json",
            "templates/base.html",
            "requirements.txt",
            "preflight_ffxivshare.bat",
            "start_ffxivshare.bat",
            "verify.ps1",
        )
        allowed = (
            "dev_init.bat",
            "document/server_usage.md",
            "sb_renderer/index.html",
            "start_waitress.bat",
        )

        self.assertTrue(all(readiness.is_critical_dirty_path(path) for path in blocking))
        self.assertFalse(any(readiness.is_critical_dirty_path(path) for path in allowed))

    def test_git_status_parser_handles_regular_untracked_and_renamed_paths(self):
        status = (
            " M ffxivshare/settings.py\n"
            "?? document/server_usage.md\n"
            "R  old-name.txt -> templates/new-name.html\n"
        )

        self.assertEqual(
            readiness.extract_dirty_paths(status),
            [
                "document/server_usage.md",
                "ffxivshare/settings.py",
                "old-name.txt",
                "templates/new-name.html",
            ],
        )

    def test_version_parsers_accept_only_declared_shapes(self):
        self.assertEqual(readiness.parse_semver("v22.18.0"), (22, 18, 0))
        self.assertEqual(
            readiness.parse_node_range(">=22.12.0 <23"),
            ((22, 12, 0), 23),
        )
        self.assertIsNone(readiness.parse_semver("22"))
        self.assertIsNone(readiness.parse_node_range("^22"))

    def test_requirement_contract_accepts_only_exact_pins(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            requirements_path = Path(temporary_directory) / "requirements.txt"
            requirements_path.write_text(
                "Django==5.2.16\n"
                "python_dotenv==1.2.2\n"
                "package>=1\n",
                encoding="utf-8",
            )

            pins, unsupported = readiness.parse_requirement_pins(requirements_path)

        self.assertEqual(
            pins,
            {"django": "5.2.16", "python-dotenv": "1.2.2"},
        )
        self.assertEqual(unsupported, [3])

    def test_manifest_requires_entry_and_every_referenced_asset(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            asset_root = Path(temporary_directory)
            manifest_path = asset_root / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "src/main.ts": {
                            "file": "assets/main.js",
                            "css": ["assets/main.css"],
                            "isEntry": True,
                        }
                    }
                ),
                encoding="utf-8",
            )
            (asset_root / "assets").mkdir()
            (asset_root / "assets" / "main.js").write_text("", encoding="utf-8")

            valid, missing = readiness.inspect_manifest(manifest_path, asset_root)
            self.assertFalse(valid)
            self.assertEqual(missing, ["assets/main.css"])

            (asset_root / "assets" / "main.css").write_text("", encoding="utf-8")
            valid, missing = readiness.inspect_manifest(manifest_path, asset_root)

        self.assertTrue(valid)
        self.assertEqual(missing, [])

    def test_collector_keeps_blockers_and_warnings_separate(self):
        collector = readiness.CheckCollector()
        collector.passed("pass", "passed")
        collector.warned("warning", "warning")
        collector.failed("blocker", "blocked")

        self.assertEqual(collector.blocker_ids, ["blocker"])
        self.assertEqual(collector.warning_ids, ["warning"])


if __name__ == "__main__":
    unittest.main()
