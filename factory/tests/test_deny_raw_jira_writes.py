import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))

import deny_raw_jira_writes as hook

ROOT = Path(__file__).resolve().parents[2]

DENIED = [
    "acli jira workitem transition --key CHB-108 --status Done",
    "acli jira workitem create --project CHB --type Task --summary x",
    "acli jira workitem create-bulk --from-csv x.csv",
    "acli jira workitem edit --key CHB-1 --summary y",
    "acli jira workitem assign --key CHB-1 --assignee @me",
    "acli jira workitem comment create --key CHB-1 --body hi",
    "acli jira workitem delete --key CHB-1",
    "acli jira workitem clone --key CHB-1",
    "acli jira workitem link create --out CHB-1 --in CHB-2 --type Blocks",
    "acli jira workitem archive --key CHB-1",
    "acli jira workitem attachment delete --id 1",
    "acli jira board create --name x",
    "acli jira board delete --id 2",
    "acli jira project update --key CHB",
    "acli jira field create --name x",
    "acli jira filter update --id 1",
    "acli jira filter change-owner --id 1",
    "acli jira sprint create --board 2",
    "acli jira auth login --web",
    "cd repo && acli jira workitem transition --key CHB-1 --status Done",
    "rtk acli jira workitem transition --key CHB-1",
    "/home/u/.local/bin/acli jira workitem edit --key CHB-1",
    "env FOO=1 acli  jira   workitem   transition --key CHB-1",
    "sh -c 'acli jira workitem transition --key CHB-1'",
    "acli jira workitem view --key CHB-1; acli jira workitem transition --key CHB-1",
    "acli jira workitem --json transition --key CHB-1",
    'acli jira workitem edit --key CHB-1 --summary "-h"',
    "acli jira workitem comment create --key CHB-1 --body --help",
    "acli jira workitem transition --help",
    "acli --profile x jira workitem transition --key CHB-1",
    "curl -X POST https://islomar.atlassian.net/rest/api/3/issue/CHB-1/transitions",
    "curl -sXPUT https://islomar.atlassian.net/rest/api/3/issue/CHB-1",
    "curl --request DELETE 'https://islomar.atlassian.net/rest/api/3/issue/CHB-1'",
    "curl --request=PATCH https://x.atlassian.net/rest/api/3/issue/CHB-1",
    "curl -d '{}' https://islomar.atlassian.net/rest/api/3/issue/CHB-1/comment",
    "curl -sS --data @body.json https://islomar.atlassian.net/rest/api/3/issue",
    "curl --data-raw x https://islomar.atlassian.net/rest/api/3/issue",
    "curl --json '{}' https://islomar.atlassian.net/rest/api/3/issue",
    "curl -F file=@a.png https://islomar.atlassian.net/rest/api/3/issue/CHB-1/attachments",
    'curl -X POST "$SITE/rest/api/3/issue"',
    "wget --method=PUT https://islomar.atlassian.net/rest/api/3/issue/CHB-1",
    "wget --post-data x https://islomar.atlassian.net/rest/api/3/issue",
    "python3 -c \"import requests; requests.post('https://islomar.atlassian.net/rest/api/3/issue')\"",
    "python3 -c \"urllib.request.Request('https://islomar.atlassian.net/rest/api/3/issue', method='PUT')\"",
    "python3 -c \"urllib.request.urlopen('https://islomar.atlassian.net/rest/api/3/issue', data=b'x')\"",
]

ALLOWED = [
    "acli jira workitem view CHB-108",
    "acli jira workitem view --key CHB-108 --json",
    "acli jira workitem search --jql 'project = CHB'",
    "acli jira workitem comment list --key CHB-1",
    "acli jira workitem list-watchers --key CHB-1",
    "acli jira board view --id 2",
    "acli jira board list-sprints --id 2",
    "acli jira filter list-columns --id 1",
    "acli jira project list",
    "acli jira auth status",
    "acli jira workitem --help",
    "acli confluence page view --id 1",
    "factory/bin/jira transition CHB-12 'In Review'",
    "factory/bin/jira comment CHB-12 --body-file c.txt",
    "curl -s https://islomar.atlassian.net/rest/api/3/issue/CHB-1",
    "curl -X GET https://islomar.atlassian.net/rest/api/3/issue/CHB-1",
    "curl -sI https://islomar.atlassian.net",
    "curl -X POST https://example.com/api -d x",
    "wget https://islomar.atlassian.net/rest/api/3/issue/CHB-1",
    "git commit -F message.txt",
    "ls -la",
    "echo racli jira workitem transition",
]


class MatcherTest(unittest.TestCase):
    def test_jira_writes_around_the_wrapper_are_denied(self) -> None:
        for command in DENIED:
            with self.subTest(command):
                self.assertIsNotNone(hook.reason(command))

    def test_reads_and_the_wrapper_are_allowed(self) -> None:
        for command in ALLOWED:
            with self.subTest(command):
                self.assertIsNone(hook.reason(command))

    def test_reason_names_the_command_and_the_wrapper(self) -> None:
        why = hook.reason("acli jira workitem transition --key CHB-1 --status Done") or ""

        self.assertIn("acli jira workitem transition", why)
        self.assertIn("factory/bin/jira", why)


def _main(stdin: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(sys, "stdin", io.StringIO(stdin)), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = hook.main()
    return code, out.getvalue(), err.getvalue()


class ContractTest(unittest.TestCase):
    """The Claude Code hook contract: exit 2 with the reason on stderr denies the tool call."""

    def test_denied_command_exits_2_with_reason_on_stderr(self) -> None:
        event = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": DENIED[0]}}
        code, out, err = _main(json.dumps(event))

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("Blocked", err)

    def test_allowed_command_exits_0_silently(self) -> None:
        event = {"tool_name": "Bash", "tool_input": {"command": "acli jira workitem view CHB-1"}}

        self.assertEqual(_main(json.dumps(event)), (0, "", ""))

    def test_input_without_a_command_is_left_alone(self) -> None:
        self.assertEqual(_main(json.dumps({"tool_name": "Read", "tool_input": {"file_path": "/x"}}))[0], 0)
        self.assertEqual(_main("[]")[0], 0)

    def test_unreadable_input_is_denied(self) -> None:
        self.assertEqual(_main("not json")[0], 2)

    def test_an_error_in_the_matcher_denies(self) -> None:
        event = {"tool_name": "Bash", "tool_input": {"command": "ls"}}
        with mock.patch.object(hook, "reason", side_effect=IndexError("boom")):
            code, _, err = _main(json.dumps(event))

        self.assertEqual(code, 2)
        self.assertIn("hook failed (IndexError)", err)

    def test_project_settings_register_the_hook_on_bash(self) -> None:
        settings = json.loads((ROOT / ".claude" / "settings.json").read_text())
        (group,) = [g for g in settings["hooks"]["PreToolUse"] if "deny_raw_jira_writes.py" in g["hooks"][0]["command"]]

        self.assertEqual(group["matcher"], "Bash")
        self.assertIn("factory/hooks/deny_raw_jira_writes.py", group["hooks"][0]["command"])


if __name__ == "__main__":
    unittest.main()
