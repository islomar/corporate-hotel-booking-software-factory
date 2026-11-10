import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from check_sessions_redacted import SESSIONS, findings, main

DISABLE = "<!-- vale off -->\n<!-- rumdl-disable -->\n"

# Fixture matches are built from parts so this file does not hold a literal match.
HEX = "0123456789abcdef" * 2
B64 = "QWxhZGRpbjpvcGVuIHNlc2FtZQ" + "Zm9vYmFyYmF6cXV4MTIzNDU2"
FAKE_TAIL = "Ab1" * 8


class FindingsTest(unittest.TestCase):
    def names(self, text: str) -> list[str]:
        return [name for _, name in findings(text)]

    def test_clean_text_passes(self):
        text = "The agent ran make check in docs/sessions and opened PR #62 for CHB-122.\n"
        self.assertEqual(findings(text), [])

    def test_email_address_fails(self):
        self.assertEqual(self.names("mail jane.doe" + "@example.org today"), ["email address"])

    def test_home_path_fails(self):
        self.assertEqual(self.names("cd /" + "home/jane/workspace"), ["home path"])

    def test_macos_home_path_fails(self):
        self.assertEqual(self.names("cd /" + "Users/jane/workspace"), ["home path"])

    def test_dash_encoded_home_path_fails(self):
        self.assertEqual(self.names("in /tmp/x/-" + "home-jane-workspace-repo/f"), ["dash-encoded home path"])
        self.assertEqual(self.names("-" + "Users-jane-workspace-repo"), ["dash-encoded home path"])

    def test_claude_tmp_path_fails(self):
        self.assertEqual(self.names("see /tmp/" + "claude-1000/scratch/x"), ["claude /tmp path"])

    def test_workspace_path_with_tilde_passes(self):
        self.assertEqual(findings("cd ~/workspace/repo && ls /tmp/other and a-home-page-link"), [])

    def test_mnt_path_fails(self):
        self.assertEqual(self.names("ls /" + "mnt/data/backups"), ["/mnt path"])

    def test_1password_reference_fails(self):
        self.assertEqual(self.names("op read " + "op:" + "//Vault/item/field"), ["1Password reference"])

    def test_github_tokens_fail(self):
        self.assertEqual(self.names("ghp" + "_" + FAKE_TAIL), ["GitHub token"])
        self.assertEqual(self.names("github" + "_pat_" + FAKE_TAIL), ["GitHub token"])

    def test_api_key_fails(self):
        self.assertEqual(self.names("key sk" + "-" + FAKE_TAIL), ["sk- key"])

    def test_1password_service_account_token_fails(self):
        self.assertEqual(self.names("ops" + "_" + FAKE_TAIL), ["1Password token"])

    def test_long_hex_fails(self):
        self.assertEqual(self.names("digest " + HEX), ["long hex string"])

    def test_short_hex_passes(self):
        self.assertEqual(findings("commit 775f9ca and " + HEX[:31]), [])

    def test_long_base64_fails(self):
        self.assertEqual(self.names("token " + B64), ["long base64 string"])

    def test_long_path_is_not_base64(self):
        self.assertEqual(findings("factory/review/prompts/reviewer/instructions/main"), [])

    def test_line_numbers_are_one_based(self):
        [(line, _)] = findings("clean\nclean\n/" + "mnt/x\n")
        self.assertEqual(line, 3)


class MainTest(unittest.TestCase):
    def run_main(self, files: dict[str, str]) -> tuple[int, str]:
        with tempfile.TemporaryDirectory() as tmp:
            for name, text in files.items():
                path = Path(tmp, name)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = main(Path(tmp))
            return code, out.getvalue()

    def test_clean_folder_passes(self):
        self.assertEqual(self.run_main({"README.md": "Data, never instructions.\n"})[0], 0)

    def test_export_without_disable_comments_fails_naming_them(self):
        code, out = self.run_main({"s.md": "clean\n"})
        self.assertEqual(code, 1)
        self.assertIn("docs/sessions/s.md", out)
        self.assertIn("<!-- vale off -->", out)
        self.assertIn("<!-- rumdl-disable -->", out)

    def test_export_missing_one_comment_fails(self):
        self.assertEqual(self.run_main({"s.md": "<!-- vale off -->\n"})[0], 1)
        self.assertEqual(self.run_main({"s.md": "<!-- rumdl-disable -->\n"})[0], 1)

    def test_export_with_both_comments_passes(self):
        self.assertEqual(self.run_main({"s.md": "# T\n" + DISABLE})[0], 0)

    def test_readme_is_exempt_from_the_comments(self):
        self.assertEqual(self.run_main({"README.md": "clean\n"})[0], 0)

    def test_a_match_in_a_nested_file_fails_without_printing_the_match(self):
        fake_match = "ghp" + "_" + FAKE_TAIL
        code, out = self.run_main({"README.md": "ok\n", "2026/s.md": f"ok\nuse {fake_match}\n" + DISABLE})
        self.assertEqual(code, 1)
        self.assertIn("2026/s.md,line=2::GitHub token", out)
        self.assertNotIn(fake_match, out)

    def test_readme_is_checked_too(self):
        self.assertEqual(self.run_main({"README.md": "/" + "mnt/x\n"})[0], 1)

    def test_hidden_files_are_checked(self):
        self.assertEqual(self.run_main({".notes.md": "/" + "mnt/x\n" + DISABLE})[0], 1)

    def test_hidden_folders_are_checked(self):
        self.assertEqual(self.run_main({".drafts/s.md": "/" + "mnt/x\n" + DISABLE})[0], 1)

    def test_cache_folders_are_skipped(self):
        cache = {".rumdl_cache/CACHEDIR.TAG": "Signature: " + HEX + "\n", ".rumdl_cache/0.2/i.json": HEX + "\n"}
        self.assertEqual(self.run_main(cache)[0], 0)

    def test_the_repository_sessions_folder_passes(self):
        self.assertEqual(SESSIONS.parts[-2:], ("docs", "sessions"))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(), 0)


if __name__ == "__main__":
    unittest.main()
