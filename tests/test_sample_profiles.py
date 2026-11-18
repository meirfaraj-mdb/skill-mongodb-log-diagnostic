"""End-to-end tests of ``--profile`` in samples/macos-local-atlas-folder-test.sh.

The real script runs with scripted answers on stdin. A fake ``uname`` reports Darwin, and the
script is cut right before the Python environment/pip step, so no network or macOS is needed.
"""
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "samples/macos-local-atlas-folder-test.sh"
STOP = 'note "Creating Python environment"'
DUMPED = ["STORAGE_PROVIDER", "LOCAL_BUCKET", "PREFIX", "LOG_DATE", "TIMEZONE", "CLUSTER_NAME", "GROUP_ID",
          "ATLAS_PUBLIC_KEY", "ATLAS_PRIVATE_KEY", "ENABLE_OBSERVABILITY", "INDEX_STATS_ENABLED",
          "MONGODB_URI_TEMPLATE", "REPORT_PROVIDER", "ANTHROPIC_MODEL", "ANTHROPIC_BASE_URL_OVERRIDE",
          "ANTHROPIC_API_KEY", "CLUSTER_SUMMARY_SOURCE", "JIRA_MODE", "JIRA_PROJECT_KEY", "JIRA_EMAIL",
          "JIRA_API_TOKEN", "SLACK_MODE", "SLACK_WEBHOOK_URL"]
SKILL_FILES = ["skills/mongodb-atlas-logs/scripts/atlas_logs.py",
               "skills/mongodb-log-diagnostic/scripts/extract_mongodb_log.py",
               "skills/mongodb-log-diagnostic/scripts/ftdc_decoder.py",
               "skills/mongodb-log-diagnostic/scripts/driver_compatibility.py",
               "skills/mongodb-log-diagnostic/references/analysis-prompt.md",
               "skills/mongodb-observability/scripts/observability.py"]


def first_run_answers(bucket):
    return [
        "local", str(bucket), "",                     # storage, folder, prefix
        "2026-11-10", "UTC", "prod", "grp1", "pub-key", "priv-key", "",  # date..private key, log names
        "yes", "", "yes", "mongodb://u:p@{host}/",    # observability, QSI source, indexStats, URI
        "anthropic", "", "https://gw.example.com/v1/messages", "sk-test", "",  # provider..summary
        "yes", "",                                    # notifications, environment
        "create", "ops", "", "", "", "", "", "", "", "https://jira.example.com", "", "me@example.com",
        "jira-token", "yes",                          # Jira token + confirmation
        "send", "", "", "", "", "https://hooks.slack.com/services/T/B/X", "yes",
    ]


@unittest.skipUnless(shutil.which("bash"), "bash is required")
class SampleProfiles(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        repo = self.tmp / "repo"
        (repo / "samples").mkdir(parents=True)
        for name in ("local-atlas-storage.sh", "local-atlas-profiles.sh"):
            shutil.copy(ROOT / "samples" / name, repo / "samples" / name)
        text = SCRIPT.read_text()
        self.assertIn(STOP, text)
        dump = "\n".join(f'printf "%s\\0%s\\0" {v} "${{{v}-}}" >> "$DUMP_FILE"' for v in DUMPED)
        (repo / "samples/run.sh").write_text(text[:text.index(STOP)] + dump + "\nexit 0\n")
        for rel in SKILL_FILES:
            (repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (repo / rel).write_text("")
        fake = self.tmp / "bin"
        fake.mkdir()
        (fake / "uname").write_text("#!/bin/sh\necho Darwin\n")
        (fake / "uname").chmod(0o755)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.bucket = self.tmp / "bucket"
        self.repo = repo
        self.profiles = self.home / ".localsample.profiles"
        self.env = {"PATH": f"{fake}:{os.environ['PATH']}", "HOME": str(self.home), "TMPDIR": str(self.tmp)}

    def run_script(self, args, answers, check=True):
        dump = self.tmp / "dump"
        dump.unlink(missing_ok=True)
        proc = subprocess.run(["bash", str(self.repo / "samples/run.sh"), *args],
                              input="".join(a + "\n" for a in answers) if answers is not None else "",
                              capture_output=True, text=True, timeout=60,
                              env={**self.env, "DUMP_FILE": str(dump)})
        if check:
            self.assertEqual(proc.returncode, 0, proc.stdout[-3000:] + proc.stderr[-3000:])
        values = {}
        if dump.exists():
            parts = dump.read_text().split("\0")
            values = dict(zip(parts[0::2], parts[1::2]))
        return proc, values

    def create_profile(self, save_secrets="yes"):
        return self.run_script(["--profile", "prod"], first_run_answers(self.bucket) + ["", save_secrets])

    def test_profile_saves_values_and_secrets_in_private_files(self):
        _, values = self.create_profile()
        self.assertEqual(values["JIRA_PROJECT_KEY"], "OPS")
        saved = json.loads((self.profiles / "prod.json").read_text())["values"]
        self.assertEqual(saved["CLUSTER_NAME"], "prod")
        self.assertEqual(saved["LOCAL_BUCKET"], str(self.bucket.resolve()))
        self.assertEqual(saved["ANTHROPIC_BASE_URL_OVERRIDE"], "https://gw.example.com/v1/messages")
        self.assertEqual((saved["JIRA_MODE"], saved["SLACK_MODE"]), ("create", "send"))
        self.assertEqual(saved["LOG_DATE"], "2026-11-10")
        secrets = json.loads((self.profiles / "prod.secrets.json").read_text())["secrets"]
        self.assertEqual(secrets, {"ATLAS_PRIVATE_KEY": "priv-key", "ANTHROPIC_API_KEY": "sk-test",
                                   "MONGODB_URI_TEMPLATE": "mongodb://u:p@{host}/",
                                   "JIRA_API_TOKEN": "jira-token",
                                   "SLACK_WEBHOOK_URL": "https://hooks.slack.com/services/T/B/X"})
        for key in secrets.values():  # secrets never land in the non-secret file
            self.assertNotIn(key, (self.profiles / "prod.json").read_text())
        for path in (self.profiles / "prod.json", self.profiles / "prod.secrets.json"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.profiles.stat().st_mode), 0o700)
        # profile mode leaves the global remembered defaults alone
        self.assertFalse((self.home / ".localsample").exists())
        self.assertFalse((self.home / ".localsample.atlas-folder.json").exists())

    def test_yes_replays_everything_without_any_input(self):
        self.create_profile()
        proc, values = self.run_script(["--profile", "prod", "--yes"], None)
        self.assertEqual(values["ATLAS_PRIVATE_KEY"], "priv-key")
        self.assertEqual(values["ANTHROPIC_API_KEY"], "sk-test")
        self.assertEqual(values["MONGODB_URI_TEMPLATE"], "mongodb://u:p@{host}/")
        self.assertEqual(values["ANTHROPIC_BASE_URL_OVERRIDE"], "https://gw.example.com/v1/messages")
        self.assertEqual((values["JIRA_MODE"], values["SLACK_MODE"]), ("create", "send"))
        self.assertEqual(values["CLUSTER_NAME"], "prod")
        self.assertIn("already confirmed in profile prod", proc.stdout)
        for secret in ("priv-key", "sk-test", "jira-token", "u:p@", "hooks.slack.com/services"):
            self.assertNotIn(secret, proc.stdout + proc.stderr)

    def test_interactive_enter_keeps_saved_answers_but_real_calls_are_reconfirmed(self):
        self.create_profile()
        proc, values = self.run_script(["--profile", "prod"], [""] * 60)
        self.assertEqual(values["ATLAS_PRIVATE_KEY"], "priv-key")
        self.assertEqual(values["GROUP_ID"], "grp1")
        # Enter at "Continue with real calls?" means no: both fall back to json and drop the token.
        self.assertEqual((values["JIRA_MODE"], values["SLACK_MODE"]), ("json", "json"))
        saved = json.loads((self.profiles / "prod.json").read_text())["values"]
        self.assertEqual(saved["JIRA_MODE"], "json")
        secrets = json.loads((self.profiles / "prod.secrets.json").read_text())["secrets"]
        self.assertNotIn("JIRA_API_TOKEN", secrets)
        self.assertEqual(secrets["ATLAS_PRIVATE_KEY"], "priv-key")

    def test_dash_clears_a_saved_answer(self):
        self.create_profile()
        answers = ["", "", "", "", "", "-"] + [""] * 60   # CLUSTER_NAME -> blank (every cluster)
        _, values = self.run_script(["--profile", "prod"], answers)
        self.assertEqual(values["CLUSTER_NAME"], "")

    def test_declining_secrets_saves_answers_only_and_removes_old_secrets(self):
        self.create_profile(save_secrets="no")
        self.assertTrue((self.profiles / "prod.json").exists())
        self.assertFalse((self.profiles / "prod.secrets.json").exists())
        self.create_profile(save_secrets="yes")
        self.assertTrue((self.profiles / "prod.secrets.json").exists())
        self.create_profile(save_secrets="no")
        self.assertFalse((self.profiles / "prod.secrets.json").exists())

    def test_yes_still_asks_for_secrets_that_were_not_saved(self):
        self.create_profile(save_secrets="no")
        answers = ["new-priv", "mongodb://x@{host}/", "sk-2", "jt-2", "https://hooks.slack.com/services/N"]
        _, values = self.run_script(["--profile", "prod", "--yes"], answers)
        self.assertEqual((values["ATLAS_PRIVATE_KEY"], values["ANTHROPIC_API_KEY"]), ("new-priv", "sk-2"))
        self.assertFalse((self.profiles / "prod.secrets.json").exists())  # --yes never rewrites

    def test_world_readable_secrets_file_is_refused(self):
        self.create_profile()
        (self.profiles / "prod.secrets.json").chmod(0o644)
        proc, _ = self.run_script(["--profile", "prod", "--yes"], None, check=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("readable by others", proc.stderr + proc.stdout)

    def test_option_errors_list_and_delete(self):
        for args, message in ((["--yes"], "only valid with --profile"),
                              (["--profile", "../x"], "Invalid profile name"),
                              (["--profile", "new", "--yes"], "needs an existing profile"),
                              (["--bogus"], "Unknown option")):
            proc, _ = self.run_script(args, None, check=False)
            self.assertNotEqual(proc.returncode, 0, args)
            self.assertIn(message, proc.stderr + proc.stdout, args)
        self.create_profile()
        proc, _ = self.run_script(["--list-profiles"], None)
        self.assertRegex(proc.stdout, r"prod\s+secrets=yes")
        self.assertNotIn("priv-key", proc.stdout)
        self.run_script(["--delete-profile", "prod"], ["yes"])
        self.assertFalse((self.profiles / "prod.json").exists())
        self.assertFalse((self.profiles / "prod.secrets.json").exists())

    def test_without_profile_behaviour_is_unchanged(self):
        answers = first_run_answers(self.bucket)
        _, values = self.run_script([], answers + ["no"])  # "Save non-secret defaults?" -> no
        self.assertEqual(values["ATLAS_PRIVATE_KEY"], "priv-key")
        self.assertFalse(self.profiles.exists())
        remembered = json.loads((self.home / ".localsample.atlas-folder.json").read_text())
        self.assertNotIn("atlas_private_key", remembered)
        self.assertEqual(remembered["atlas_public_key"], "pub-key")


if __name__ == "__main__":
    unittest.main()
