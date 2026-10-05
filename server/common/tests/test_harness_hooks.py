"""build_infra pins for the PreToolUse Bash hooks in .claude/settings.json (tj-qenrpk).

Two hooks guard every Bash command an agent runs: the push-deny hook (commit dccd2c5) and the
dot-env read hook (bead tj-10jczr). Each is one shell pipeline: jq pulls tool_input.command out of
the PreToolUse payload on stdin, grep -P matches it, and on a match echo prints a deny decision.
The pipeline ends in `|| true`, and that is the hazard this module exists for: every way it can
fail -- the regex misses, jq is not installed, grep has no -P -- ends the same way, exit 0 with
nothing printed, which Claude Code reads as no objection. A hook that silently allows looks
exactly like a hook that is working.

SO NOTHING HERE COPIES A REGEX. Every test loads .claude/settings.json, takes each PreToolUse hook
whose matcher selects the Bash tool VERBATIM, and runs it the way Claude Code runs a command hook
on Linux -- "The command string is passed to a shell: sh -c on macOS and Linux"
(https://code.claude.com/docs/en/hooks) -- with a PreToolUse payload on stdin. All matching hooks
run, as they do live, and a case is denied when any one of them denies it. A hook edited in
settings.json is therefore the hook under test, with no second copy to drift.

THE TOOLS ARE CHECKED, NOT ASSUMED. A missing jq, or a grep without -P, turns every deny case below
into an allow, and the failure would read as "the regex is wrong". Every run first checks both
tools work and FAILS naming the missing one (pytest.ini: fail, never skip). CI's unit job runs in
debian:bookworm-slim, which has no jq until tj-3mk3u5.40 (HK-CI) installs it: until then the
tests that run a hook are red there, and say why.

KNOWN BYPASSES are strict xfails, the KNOWN_BROKEN shape of data/store/tests/test_http_smoke.py.
Each one was observed, not inferred: the case ran and the hooks allowed it. Each is a FINDING for
the user (agents are refused edits to settings.json), recorded on tj-qenrpk. strict=True is the
point: the day settings.json closes one, that case XPASSes, the run goes red, and its entry comes
out of KNOWN_BYPASSES -- from then on it is held as a plain deny case.

THE CASE STRINGS LIVE ONLY IN THIS FILE. The live hooks read the whole text of every Bash command,
so typing one of these on a command line -- in a note, a commit message, a grep over this file --
is denied. That is the hooks working; the by-design cases below pin it.
"""

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from functools import cache

import pytest

from common.tests.roots import REPO_ROOT


pytestmark = pytest.mark.build_infra

# THE TRUE REPOSITORY ROOT (tj-iontkq.2): .claude/settings.json stays at the top of the repository,
# and the hooks it registers are run with the repository root as their cwd -- that is the directory
# a hook's own path rules are written against. REPO_ROOT, never SERVER_ROOT.
SETTINGS = REPO_ROOT / '.claude' / 'settings.json'
TOOL = 'Bash'
EVENT = 'PreToolUse'

# A word each hook's deny reason must carry, so a case is pinned to the hook that owns it rather
# than to whichever hook happens to deny it.
PUSH_RULE = 'push'
DOT_ENV_RULE = '.env'

# The hooks reference's matcher rules: "*", "" or omitted match every tool; a matcher made only of
# letters, digits, "_", "-", spaces, "," and "|" is an exact name or a list of exact names; anything
# else is an unanchored JavaScript regular expression, which re.search approximates.
_EXACT_MATCHER = re.compile(r'[A-Za-z0-9_\- ,|]*')


# --- The cases ----------------------------------------------------------------------------------

# Every git push spelling the commit names, plus the separators and positions the regex claims.
PUSH_DENIED = {
    'bare': 'git push',
    'force-with-remote': 'git push --force origin x',
    'dash-C-directory': 'git -C /x push',
    'dash-c-config': 'git -c a=b push',
    'no-pager': 'git --no-pager push',
    'after-cd-and': 'cd x && git push',
    'after-semicolon': 'a; git push',
    'after-pipe': 'true | git push origin main',
    'in-subshell-with-remote': '(git push origin main)',
    'git-dir-with-equals': 'git --git-dir=/x/.git push',
    'tab-separated': 'git\tpush',
    'on-a-later-line': 'git status\ngit push origin main',
}

# What the push-deny hook must leave alone: the word push that is not the git subcommand.
PUSH_ALLOWED = {
    'status': 'git status',
    'log-grep-push': 'git log --grep push',
    'stash-push': 'git stash push -m wip',
    'commit-message-says-pushes': 'git commit -m "pushes are done by hand"',
    'the-word-alone': 'echo push',
}

# Denied BY DESIGN, per dccd2c5: "The hook reads the whole command text, so a command that merely
# quotes a push spelling (a heredoc commit message, say) is denied too; pass such text through a file."
PUSH_DENIED_BY_DESIGN = {
    'heredoc-commit-message': "git commit -F - <<'EOF'\ndocs: say the user runs git push by hand\nEOF",
    'note-quoting-a-push': 'bd update tj-x --append-notes "never run git push from an agent"',
}

# The suspected bypasses tj-qenrpk names, then seven more found while probing. All are pushes by the
# design's own words ("any Bash command whose git subcommand is push"), so all are expected denied.
PUSH_PROBES = {
    'absolute-path': '/usr/bin/git push',
    'double-quoted-git': '"git" push',
    'single-quoted-git': "'git' push",
    'sh-dash-c': 'sh -c "git push"',
    'git-dir-separate-value': 'git --git-dir /x/.git push',
    'work-tree-separate-value': 'git --work-tree /x push',
    'env-wrapper': 'env git push',
    'xargs-wrapper': 'echo origin | xargs git push',
    # Beyond the bead's list.
    'subshell': '(git push)',
    'command-substitution': 'echo "$(git push)"',
    'separator-attached': 'git -C /x push;echo done',
    'line-continuation': 'git \\\npush origin main',
    'inline-alias': 'git -c alias.p=push p origin main',
    'backtick-substitution': 'echo `git push`',
    'send-pack-plumbing': 'git send-pack ../remote.git main',
}

DOT_ENV_DENIED = {
    'cat': 'cat .env',
    'grep': 'grep X .env',
    'nested-path': 'cat data/ingest/.env',
    'absolute-path': 'head -n 3 /workspace/.env',
    'dot-slash': 'tail ./.env',
    'source': 'source .env',
    'copy-out': 'cp .env /tmp/copy',
    'python-open': 'python3 -c "print(open(\'.env\').read())"',
    'placeholder-beside-live': 'cat .env.default .env',
}

# The placeholders stay readable -- the user's ruling on tj-10jczr: "agents can read .env.default,
# never the live one" -- and a name that only starts with .env is a different file.
DOT_ENV_ALLOWED = {
    'default': 'cat .env.default',
    'example': 'cat .env.example',
    'sample': 'cat .env.sample',
    'template': 'cat .env.template',
    'diff-of-placeholders': 'diff .env.default data/ingest/.env.default',
    'longer-name': 'cat .envrc',
    'listing-is-not-reading': 'ls -l .env',
}

DOT_ENV_DENIED_BY_DESIGN = {
    'note-quoting-a-read': 'bd update tj-x --append-notes "never cat .env, read the placeholder"'
}

# Not in the bead's list: three ways to reach the live file that the reader-command list misses.
DOT_ENV_PROBES = {
    'dot-source': '. ./.env && env',
    'redirect-before-reader': '< .env cat',
    'line-continuation': 'cat \\\n.env',
}

# Observed ALLOWED by the hooks at fa496b8, each a finding on tj-qenrpk; see the module docstring.
# "Deny rules" is the other layer, permissions.deny, as https://code.claude.com/docs/en/permissions
# describes it; where the docs say it does not stop a form, that form passes both layers.
_FINDING = 'FINDING tj-qenrpk, observed allowed at fa496b8: '
KNOWN_BYPASSES: dict[str, str] = {
    'push:absolute-path': _FINDING + 'nothing in the prefix class matches "/" before git. '
    'Deny rules: the docs say they do not stop a program invoked by path.',
    'push:double-quoted-git': _FINDING + 'a quote before git is not in the prefix class. '
    'Deny rules: undocumented for a quoted program name.',
    'push:single-quoted-git': _FINDING + 'a quote before git is not in the prefix class. '
    'Deny rules: undocumented for a quoted program name.',
    'push:sh-dash-c': _FINDING + 'the quote opening the sh -c argument sits right before git. '
    'Deny rules: the docs say they do not stop sh -c.',
    'push:git-dir-separate-value': _FINDING + 'only -C and -c may take a separate value; --git-dir <dir> '
    'stops the option loop. Deny rules: no match, the command does not begin with the subcommand.',
    'push:work-tree-separate-value': _FINDING + 'only -C and -c may take a separate value; --work-tree <dir> '
    'stops the option loop. Deny rules: no match, the command does not begin with the subcommand.',
    'push:subshell': _FINDING + 'the subcommand must be followed by whitespace or end of line, not ")". '
    'Deny rules: the docs say they apply inside a subshell, so they cover this bare form.',
    'push:command-substitution': _FINDING + 'the subcommand must be followed by whitespace or end of line, '
    'not ")". Deny rules: the docs say they apply inside a command substitution.',
    'push:separator-attached': _FINDING + 'a separator directly after the subcommand is not whitespace. '
    'Deny rules: no match, because of the -C before the subcommand. Passes both layers.',
    'push:line-continuation': _FINDING + 'grep matches line by line, and a backslash-newline splits git '
    'from its subcommand. Deny rules: undocumented for line continuations.',
    'push:inline-alias': _FINDING + 'a -c alias whose value is the subcommand runs it under another name. '
    'Deny rules: the docs say they do not stop the -c form.',
    'push:backtick-substitution': _FINDING + 'a backtick before git is not in the prefix class. '
    'Deny rules: the docs say they apply inside a command substitution.',
    'push:send-pack-plumbing': _FINDING + 'send-pack updates remote refs without the porcelain subcommand '
    'the regex names. Deny rules: no rule names it.',
    'dot-env:dot-source': _FINDING + '"." is not in the reader list, and sourcing loads every value into '
    'the shell. Read deny rules: undocumented for ".", wherever the file is.',
    'dot-env:redirect-before-reader': _FINDING + 'the reader comes after the file name. Read deny rules '
    'check input-redirect targets: at or under the session cwd, and since 2aefba7 (tj-3mk3u5.51) at any '
    'depth under /workspace from every session, per the docs; not probed with this form.',
    'dot-env:line-continuation': _FINDING + 'grep matches line by line, and a backslash-newline splits the '
    'reader from the file. Read deny rules: undocumented for line continuations.',
}


# --- Running the hooks --------------------------------------------------------------------------


@dataclass(frozen=True)
class HookRun:
    """One hook command run against one tool call: its exit status and what it printed."""

    hook: str
    returncode: int
    stdout: str
    stderr: str

    def deny_reason(self) -> str | None:
        """The reason, when Claude Code would treat this run as a deny; None when it would not.

        Exit 2 blocks whatever was printed. Exit 0 denies only through a JSON object on stdout
        whose hookSpecificOutput names this event and the decision "deny": stdout that is not JSON,
        or JSON that fails the schema, is a non-blocking error, and the call goes ahead.
        """
        if self.returncode == 2:
            return self.stderr.strip() or '(exit 2, no stderr)'
        if self.returncode != 0:
            return None
        text = self.stdout.strip()
        if not (text.startswith('{') and text.endswith('}')):
            return None
        try:
            output = json.loads(text).get('hookSpecificOutput') or {}
        except (json.JSONDecodeError, AttributeError):
            return None
        if output.get('hookEventName') != EVENT or output.get('permissionDecision') != 'deny':
            return None
        return str(output.get('permissionDecisionReason') or '')


def _matches_tool(matcher: str | None) -> bool:
    if matcher in (None, '', '*'):
        return True
    if _EXACT_MATCHER.fullmatch(matcher):
        return TOOL in {name.strip() for name in re.split(r'[|,]', matcher)}
    return re.search(matcher, TOOL) is not None


def _bash_hook_commands() -> list[str]:
    """Every PreToolUse command hook that fires on a Bash call, verbatim from .claude/settings.json.

    Read on every call, never cached, so the hook under test is always the file as it stands.
    """
    settings = json.loads(SETTINGS.read_text(encoding='utf-8'))
    commands = []
    for group in (settings.get('hooks') or {}).get(EVENT) or []:
        if not _matches_tool(group.get('matcher')):
            continue
        for hook in group.get('hooks') or []:
            # Only the shell-command form can be run here. Exec form (args) and a chosen shell run
            # differently, and the other types are not shell at all: extend this, never skip them.
            assert hook.get('type') == 'command' and 'args' not in hook and 'shell' not in hook, (
                f'a {EVENT} hook on {TOOL} that this module cannot run as `sh -c <command>`: {hook}'
            )
            commands.append(hook['command'])
    return commands


@cache
def _hook_tools_problem() -> str:
    """Why the hooks' own tools would not work here, or '' when they do. Checked once per run.

    Both are checked by running them, not by finding them: a jq that cannot parse, or a grep that
    rejects -P, fails the hook open exactly as a missing one does.
    """
    problems = []
    jq = subprocess.run(
        ['sh', '-c', 'printf \'{"a":"ok"}\' | jq -r .a'], capture_output=True, text=True, check=False, timeout=30
    )
    if jq.stdout.strip() != 'ok':
        problems.append(f'jq does not work here (which: {shutil.which("jq")}; stderr: {jq.stderr.strip()!r})')
    pcre = subprocess.run(
        ['sh', '-c', "printf 'ab\\n' | grep -qP 'a(?=b)'"], capture_output=True, text=True, check=False, timeout=30
    )
    if pcre.returncode != 0:
        problems.append(
            f'grep -P does not work here (which: {shutil.which("grep")}; exit {pcre.returncode}; '
            f'stderr: {pcre.stderr.strip()!r})'
        )
    if not problems:
        return ''
    return (
        '; '.join(problems) + '. Both hooks in .claude/settings.json pipe the command through jq into '
        'grep -P and end in `|| true`, so without these tools each hook ALLOWS every command, silently. '
        'This is a missing tool, not a regex failure. The agent image installs jq (.devcontainer/Dockerfile); '
        "CI's unit job gets it from tj-3mk3u5.40 (HK-CI)."
    )


def _run_hooks(tool_command: str) -> list[HookRun]:
    """Run every Bash PreToolUse hook against a Bash call carrying `tool_command`."""
    problem = _hook_tools_problem()
    if problem:
        pytest.fail(problem)
    hooks = _bash_hook_commands()
    assert hooks, f'{SETTINGS.relative_to(REPO_ROOT)} registers no {EVENT} command hook on {TOOL}'
    payload = json.dumps(
        {
            'session_id': 'tj-qenrpk',
            'transcript_path': '/dev/null',
            'cwd': str(REPO_ROOT),
            'permission_mode': 'default',
            'hook_event_name': EVENT,
            'tool_name': TOOL,
            'tool_input': {'command': tool_command, 'description': 'harness hook test case'},
            'tool_use_id': 'toolu_tj_qenrpk',
        }
    )
    runs = []
    for hook in hooks:
        result = subprocess.run(
            ['sh', '-c', hook], input=payload, capture_output=True, text=True, cwd=REPO_ROOT, check=False, timeout=30
        )
        runs.append(HookRun(hook, result.returncode, result.stdout, result.stderr))
    return runs


def _describe(runs: list[HookRun]) -> str:
    return '\n'.join(
        f'  hook {index}: exit {run.returncode}, stdout {run.stdout.strip()!r}, stderr {run.stderr.strip()!r}'
        for index, run in enumerate(runs)
    )


def _assert_denied(tool_command: str, rule: str) -> None:
    runs = _run_hooks(tool_command)
    reasons = [reason for run in runs if (reason := run.deny_reason()) is not None]
    assert any(rule in reason for reason in reasons), (
        f'no hook denied {tool_command!r} with a reason naming {rule!r}; it would RUN.\n{_describe(runs)}'
    )


def _assert_allowed(tool_command: str) -> None:
    runs = _run_hooks(tool_command)
    denied = [run for run in runs if run.deny_reason() is not None]
    assert not denied, f'{tool_command!r} was denied, and it is a command the hooks must allow.\n{_describe(runs)}'
    # Allowed CLEANLY: a hook that errored also allows, and an error here means a broken pipeline.
    broken = [run for run in runs if run.returncode != 0 or run.stderr]
    assert not broken, f'a hook did not run cleanly on {tool_command!r}.\n{_describe(runs)}'


def _params(cases: dict[str, str], prefix: str = '') -> list:
    params = []
    for case_id, command in cases.items():
        reason = KNOWN_BYPASSES.get(prefix + case_id)
        # raises=AssertionError: only "the hooks allowed it" counts as the known bypass. A missing jq
        # or grep -P is pytest.fail, which is not an AssertionError, so it still FAILS these cases
        # instead of hiding behind the xfail.
        marks = [pytest.mark.xfail(strict=True, raises=AssertionError, reason=reason)] if reason else []
        params.append(pytest.param(command, id=case_id, marks=marks))
    return params


# --- The tests ----------------------------------------------------------------------------------


def test_the_hook_pipelines_tools_work_here() -> None:
    """Jq and grep -P, by name. Every other test here fails the same way without them; this one says it once."""
    assert not _hook_tools_problem(), _hook_tools_problem()


def test_every_known_bypass_names_a_probe_case() -> None:
    """A KNOWN_BYPASSES key that names no case marks nothing, and its bypass would vanish from the run."""
    cases = {f'push:{case}' for case in PUSH_PROBES} | {f'dot-env:{case}' for case in DOT_ENV_PROBES}
    assert set(KNOWN_BYPASSES) <= cases, f'entries that name no probe case: {sorted(set(KNOWN_BYPASSES) - cases)}'


def test_the_settings_register_both_bash_hooks() -> None:
    """Two Bash command hooks, each saying in its deny text which rule it enforces.

    The deny cases below find their hook by that word, so a hook whose text stopped naming its rule
    would fail every one of its cases at once; this names the cause instead.
    """
    hooks = _bash_hook_commands()
    for rule in (PUSH_RULE, DOT_ENV_RULE):
        assert any('permissionDecisionReason' in hook and rule in hook for hook in hooks), (
            f'no {EVENT} hook on {TOOL} in {SETTINGS.relative_to(REPO_ROOT)} carries a deny reason naming {rule!r}'
        )


@pytest.mark.parametrize('tool_command', _params(PUSH_DENIED))
def test_the_push_hook_denies_every_push_spelling(tool_command: str) -> None:
    """Commit dccd2c5: "denies any Bash command whose git subcommand is push, whatever global options precede it"."""
    _assert_denied(tool_command, PUSH_RULE)


@pytest.mark.parametrize('tool_command', _params(PUSH_ALLOWED))
def test_the_push_hook_allows_commands_that_do_not_push(tool_command: str) -> None:
    """The other half of dccd2c5's check: the word push elsewhere, and ordinary git commands, run."""
    _assert_allowed(tool_command)


@pytest.mark.parametrize('tool_command', _params(PUSH_DENIED_BY_DESIGN))
def test_a_command_that_only_quotes_a_push_spelling_is_denied_by_design(tool_command: str) -> None:
    """The cost dccd2c5 accepted: the hook reads the whole text. Text like this goes through a file."""
    _assert_denied(tool_command, PUSH_RULE)


@pytest.mark.parametrize('tool_command', _params(PUSH_PROBES, prefix='push:'))
def test_a_suspected_push_bypass_is_denied(tool_command: str) -> None:
    """Every spelling tj-qenrpk suspected of slipping past, run for real. A strict xfail is an observed bypass."""
    _assert_denied(tool_command, PUSH_RULE)


@pytest.mark.parametrize('tool_command', _params(DOT_ENV_DENIED))
def test_the_dot_env_hook_denies_reading_the_live_file(tool_command: str) -> None:
    """tj-10jczr: a reader command naming the live file, at any path, is denied."""
    _assert_denied(tool_command, DOT_ENV_RULE)


@pytest.mark.parametrize('tool_command', _params(DOT_ENV_ALLOWED))
def test_the_dot_env_hook_allows_the_placeholders(tool_command: str) -> None:
    """tj-10jczr: the placeholder files stay readable, and the hook does not fire on a mere name."""
    _assert_allowed(tool_command)


@pytest.mark.parametrize('tool_command', _params(DOT_ENV_DENIED_BY_DESIGN))
def test_a_command_that_only_quotes_a_dot_env_read_is_denied_by_design(tool_command: str) -> None:
    """The same whole-text cost as the push hook's: a note that spells the read out is itself denied."""
    _assert_denied(tool_command, DOT_ENV_RULE)


@pytest.mark.parametrize('tool_command', _params(DOT_ENV_PROBES, prefix='dot-env:'))
def test_a_suspected_dot_env_bypass_is_denied(tool_command: str) -> None:
    """Reads of the live file that do not put a listed reader command before its name."""
    _assert_denied(tool_command, DOT_ENV_RULE)
