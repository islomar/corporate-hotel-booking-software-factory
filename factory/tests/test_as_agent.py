import base64
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from as_agent import agent_env, jwt_signing_input, noreply_email


def _decode(part: str) -> dict[str, object]:
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


class JwtSigningInputTest(unittest.TestCase):
    def test_header_and_claims_for_a_github_app(self) -> None:
        header, claims = jwt_signing_input("Iv23client", now=1_000_000).split(".")

        self.assertEqual(_decode(header), {"alg": "RS256", "typ": "JWT"})
        self.assertEqual(_decode(claims), {"iat": 999_940, "exp": 1_000_540, "iss": "Iv23client"})

    def test_parts_are_unpadded_base64url(self) -> None:
        self.assertNotIn("=", jwt_signing_input("Iv23client", now=1_000_000))


class AgentEnvTest(unittest.TestCase):
    def setUp(self) -> None:
        self.env = agent_env(
            {"PATH": "/bin", "GH_TOKEN": "owner-token", "GITHUB_TOKEN": "owner"},
            token="ghs_agent",
            bot_login="chb-factory-agent[bot]",
            bot_id=42,
        )

    def test_gh_uses_the_installation_token(self) -> None:
        self.assertEqual(self.env["GH_TOKEN"], "ghs_agent")
        self.assertNotIn("GITHUB_TOKEN", self.env)
        self.assertEqual(self.env["PATH"], "/bin")

    def test_commits_are_authored_by_the_bot(self) -> None:
        email = "42+chb-factory-agent[bot]@users.noreply.github.com"
        for role in ("AUTHOR", "COMMITTER"):
            self.assertEqual(self.env[f"GIT_{role}_NAME"], "chb-factory-agent[bot]")
            self.assertEqual(self.env[f"GIT_{role}_EMAIL"], email)

    def test_git_pushes_over_https_with_the_token(self) -> None:
        count = int(self.env["GIT_CONFIG_COUNT"])
        config = {self.env[f"GIT_CONFIG_KEY_{i}"]: self.env[f"GIT_CONFIG_VALUE_{i}"] for i in range(count)}

        self.assertEqual(config["url.https://github.com/.insteadOf"], "git@github.com:")
        self.assertEqual(config["credential.https://github.com.helper"], "!gh auth git-credential")


class NoreplyEmailTest(unittest.TestCase):
    def test_github_noreply_format(self) -> None:
        self.assertEqual(noreply_email("bot[bot]", 7), "7+bot[bot]@users.noreply.github.com")


if __name__ == "__main__":
    unittest.main()
