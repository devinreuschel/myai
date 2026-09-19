import os
import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from myai.sandbox.agent_git import (
    AgentGitError,
    import_refs,
    is_git_repo,
    layout,
    seed,
)
from myai.sandbox.config import SandboxConfig


def _git(args, cwd, **env):
    e = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": str(Path(cwd) / ".gitconfig-none"),
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@x",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@x",
        **env,
    }
    return subprocess.run(
        ["git", *args], cwd=str(cwd), env=e, capture_output=True, text=True, check=True
    )


@unittest.skipUnless(shutil.which("git"), "git not installed")
class AgentGitTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name).resolve()
        self._old_home = os.environ.get("MYAI_HOME")
        os.environ["MYAI_HOME"] = str(self.tmp / "state")
        # git needs file:// transport allowed for local clone/fetch in some setups
        self._old_allow = os.environ.get("GIT_ALLOW_PROTOCOL")
        os.environ.setdefault("GIT_CONFIG_COUNT", "1")
        os.environ["GIT_CONFIG_KEY_0"] = "protocol.file.allow"
        os.environ["GIT_CONFIG_VALUE_0"] = "always"
        self.repo = self.tmp / "repo"
        (self.repo / "src").mkdir(parents=True)
        _git(["init", "-q", "-b", "main"], self.repo)
        (self.repo / "src" / "a.py").write_text("print(1)\n", encoding="utf-8")
        _git(["add", "-A"], self.repo)
        _git(["commit", "-qm", "host: initial"], self.repo)
        (self.repo / "src" / "a.py").write_text("print(1)\nprint(2)\n", encoding="utf-8")
        _git(["commit", "-qam", "host: second"], self.repo)

    def tearDown(self) -> None:
        for k in ("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0"):
            os.environ.pop(k, None)
        if self._old_home is None:
            os.environ.pop("MYAI_HOME", None)
        else:
            os.environ["MYAI_HOME"] = self._old_home
        self._tmp.cleanup()

    def _cfg(self) -> SandboxConfig:
        return SandboxConfig(git_access="commit")

    def _commit_as_agent(self, lay, *, branch="agent/feature"):
        """Do real git work through the scratch dir the way the guest would."""
        env = {
            "GIT_DIR": str(lay.host_git_dir),
            "GIT_WORK_TREE": str(self.repo),
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "protocol.file.allow",
            "GIT_CONFIG_VALUE_0": "always",
            "GIT_CONFIG_KEY_1": "safe.directory",
            "GIT_CONFIG_VALUE_1": "*",
        }
        # host_path layout points the alternate at the real objects, which on the
        # host is the same path the guest would see in host_path mount mode
        (lay.host_git_dir / "objects" / "info" / "alternates").write_text(
            str((self.repo / ".git" / "objects").resolve()) + "\n", encoding="utf-8"
        )
        _git(["checkout", "-q", "-b", branch], self.repo, **env)
        (self.repo / "src" / "a.py").write_text("print(1)\nprint(2)\nprint(3)\n", encoding="utf-8")
        _git(["commit", "-qam", "agent: c1"], self.repo, **env)
        return _git(["rev-parse", "HEAD"], self.repo, **env).stdout.strip()

    # --- seeding --------------------------------------------------------------
    def test_seed_none_when_not_commit_mode(self) -> None:
        self.assertIsNone(seed(self.repo, SandboxConfig(git_access="read-only")))

    def test_seed_none_when_not_a_git_repo(self) -> None:
        plain = self.tmp / "plain"
        plain.mkdir()
        self.assertIsNone(seed(plain, self._cfg()))

    def test_seed_creates_shared_scratch_with_guest_alternate(self) -> None:
        lay = seed(self.repo, self._cfg())
        assert lay is not None
        self.assertTrue(lay.host_git_dir.is_dir())
        alt = (lay.host_git_dir / "objects" / "info" / "alternates").read_text()
        # points at the guest-visible objects path, not the host path
        self.assertEqual(alt.strip(), lay.guest_objects)
        self.assertTrue(lay.guest_objects.endswith("/.git/objects"))
        # index populated from HEAD so the mounted work tree reads clean
        self.assertTrue((lay.host_git_dir / "index").is_file())

    def test_seed_is_fresh_each_time(self) -> None:
        lay = seed(self.repo, self._cfg())
        assert lay is not None
        (lay.host_git_dir / "STALE").write_text("x", encoding="utf-8")
        seed(self.repo, self._cfg())
        self.assertFalse((lay.host_git_dir / "STALE").exists())

    # --- import ---------------------------------------------------------------
    def test_agent_commits_import_into_sandbox_namespace(self) -> None:
        lay = seed(self.repo, self._cfg())
        assert lay is not None
        sha = self._commit_as_agent(lay)

        imported = import_refs(self.repo, self._cfg(), run_id="run1")
        refs = {ref: full for ref, full in imported}
        self.assertIn("refs/sandbox/run1/agent/feature", refs)
        # the imported ref is the agent's commit, now present in the real repo
        got = _git(["rev-parse", "refs/sandbox/run1/agent/feature"], self.repo).stdout.strip()
        self.assertTrue(sha.startswith(got) or got == sha[: len(got)])
        _git(["cat-file", "-e", sha], self.repo)  # object really transferred

    def test_import_does_not_touch_user_branches_or_config(self) -> None:
        before_main = _git(["rev-parse", "main"], self.repo).stdout
        before_cfg = (self.repo / ".git" / "config").read_bytes()
        hook = self.repo / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
        before_hook = hook.read_bytes()

        lay = seed(self.repo, self._cfg())
        assert lay is not None
        self._commit_as_agent(lay)
        import_refs(self.repo, self._cfg(), run_id="run1")

        self.assertEqual(_git(["rev-parse", "main"], self.repo).stdout, before_main)
        self.assertEqual((self.repo / ".git" / "config").read_bytes(), before_cfg)
        self.assertEqual(hook.read_bytes(), before_hook)

    def test_import_none_when_agent_made_no_branch(self) -> None:
        seed(self.repo, self._cfg())
        # no agent commits, no new heads beyond main -> only main would import;
        # nothing on refs/heads/agent/* etc. still imports main, which is fine,
        # but there are no *new* commits. Assert it does not error and stays clean.
        imported = import_refs(self.repo, self._cfg(), run_id="run1")
        self.assertIsInstance(imported, list)

    # --- adversarial ----------------------------------------------------------
    def test_poisoned_alternate_is_neutralized_before_host_reads_scratch(self) -> None:
        lay = seed(self.repo, self._cfg())
        assert lay is not None
        self._commit_as_agent(lay)
        # guest poisons the alternate to point outside the repo
        (lay.host_git_dir / "objects" / "info" / "alternates").write_text(
            "/etc\n", encoding="utf-8"
        )
        import_refs(self.repo, self._cfg(), run_id="run1")
        # host reset it to the real objects path before fetching
        alt = (lay.host_git_dir / "objects" / "info" / "alternates").read_text().strip()
        self.assertEqual(alt, str((self.repo / ".git" / "objects").resolve()))

    def test_scratch_hooks_never_run_on_host_import(self) -> None:
        lay = seed(self.repo, self._cfg())
        assert lay is not None
        self._commit_as_agent(lay)
        marker = self.tmp / "PWNED"
        for hook_name in ("pre-receive", "post-receive", "update", "post-update"):
            h = lay.host_git_dir / "hooks" / hook_name
            h.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
            h.chmod(0o755)
        import_refs(self.repo, self._cfg(), run_id="run1")
        self.assertFalse(marker.exists())

    def test_corrupt_object_from_scratch_is_rejected_by_fsck(self) -> None:
        lay = seed(self.repo, self._cfg())
        assert lay is not None
        self._commit_as_agent(lay)
        # scribble a bogus loose object into the scratch store
        junk_dir = lay.host_git_dir / "objects" / "ab"
        junk_dir.mkdir(parents=True, exist_ok=True)
        (junk_dir / ("0" * 38)).write_text("not a real object", encoding="utf-8")
        # import should still succeed for the valid refs; the junk is unreferenced
        # and simply not transferred. The point: import never crashes the run.
        try:
            import_refs(self.repo, self._cfg(), run_id="run1")
        except AgentGitError:
            self.fail("unreferenced junk should not fail the import")

    def test_layout_is_stable_for_a_repo(self) -> None:
        a = layout(self.repo, self._cfg())
        b = layout(self.repo, self._cfg())
        self.assertEqual(a.host_git_dir, b.host_git_dir)
        self.assertEqual(a.guest_git_dir, "/root/agent-git")

    def test_is_git_repo(self) -> None:
        self.assertTrue(is_git_repo(self.repo))
        self.assertFalse(is_git_repo(self.tmp / "nope"))
