"""Run one role through a local, non-interactive model client."""

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile


BACKENDS = {"codex", "kimi", "dsh", "custom"}
KIMI_EFFORTS = {"low", "medium", "high", "xhigh", "max"}
KIMI_AGENT = """---
name: vb-multi-agent-worker
description: Work on the assigned role in a local checkout.
tools: [{tools}]
---
{instruction}
The final message is the entire handoff.
"""
DSH_DISABLED_ROWS = (
    "tool-agent-team",
    "tool-bash",
    "tool-goal",
    "tool-jobs",
    "tool-plugin-manager",
    "tool-pwsh",
    "tool-skill",
    "tool-subagent",
    "tool-subagent-control",
    "tool-subagent-fork",
    "tool-web",
    "tool-workflow",
    "tool-executor-ptc",
    "session-log-deepseek",
    "session-telemetry-otel",
)


def validate_profile(profile: dict) -> None:
    if not isinstance(profile, dict):
        raise RuntimeError("A role profile must be an object")
    backend = profile.get("backend")
    if not isinstance(backend, str) or backend not in BACKENDS:
        raise RuntimeError("backend must be codex, kimi, dsh, or custom")
    optional = {"model", "reasoning"}
    if backend == "custom":
        allowed = {"backend", "command"} | optional
    else:
        optional.add("executable")
        if backend == "dsh":
            optional.add("provider")
        allowed = {"backend"} | optional
    unknown = profile.keys() - allowed
    if unknown:
        raise RuntimeError(f"{backend}: unknown profile field(s): {', '.join(sorted(unknown))}")
    for key in profile.keys() & optional:
        value = profile[key]
        if not isinstance(value, str) or not value.strip() or "\x00" in value:
            raise RuntimeError(f"{backend}: {key} must be a non-empty string")
    if backend == "custom":
        command = profile.get("command")
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(arg, str) and "\x00" not in arg for arg in command)
            or not command[0].strip()
        ):
            raise RuntimeError("custom: command must be a non-empty argument array")
        for key in ("model", "reasoning"):
            if key not in profile and any("{" + key + "}" in arg for arg in command):
                raise RuntimeError(f"custom: command uses {{{key}}} without setting {key}")
    if backend == "dsh" and "model" not in profile and {"provider", "reasoning"} & profile.keys():
        raise RuntimeError("dsh: provider and reasoning require an explicit model")
    if backend == "kimi" and "reasoning" in profile and profile["reasoning"] not in KIMI_EFFORTS:
        raise RuntimeError("kimi: reasoning must be low, medium, high, xhigh, or max")


def _custom_command(profile: dict) -> list[str]:
    command = profile["command"].copy()
    for key in ("model", "reasoning"):
        if key in profile:
            token = "{" + key + "}"
            command = [arg.replace(token, profile[key]) for arg in command]
    return command


def check_executable(profile: dict) -> str:
    validate_profile(profile)
    backend = profile["backend"]
    name = _custom_command(profile)[0] if backend == "custom" else profile.get("executable", backend)
    name = os.path.expanduser(name)
    path = shutil.which(name)
    if path is None:
        raise RuntimeError(f"{backend}: executable {name!r} is unavailable. Install the CLI or set its path.")
    return os.path.abspath(path)


def _codex_command(executable: str, profile: dict, cwd: Path, access: str) -> list[str]:
    command = [
        executable, "--ask-for-approval", "never", "exec", "--sandbox", access,
        "--ephemeral", "--ignore-user-config", "--color", "never",
    ]
    # Keep executable hooks and external tools outside the role. The temporary checkout
    # has its own path, so it can be marked untrusted without affecting the user's repo.
    settings = (
        "features.apps=false",
        "features.plugins=false",
        "features.hooks=false",
        "features.multi_agent=false",
        "allow_login_shell=false",
        "sandbox_workspace_write.exclude_slash_tmp=true",
        "sandbox_workspace_write.exclude_tmpdir_env_var=true",
        'web_search="disabled"',
        f'projects={{{json.dumps(str(cwd.resolve()), ensure_ascii=False)}={{trust_level="untrusted"}}}}',
    )
    for setting in settings:
        command += ["-c", setting]
    if "model" in profile:
        command += ["--model", profile["model"]]
    if "reasoning" in profile:
        command += ["-c", "model_reasoning_effort=" + json.dumps(profile["reasoning"])]
    return command + ["-"]


def _kimi_command(
    executable: str, profile: dict, prompt: str, scratch: Path, writable: bool
) -> list[str]:
    agent = scratch / "agent.md"
    tools = "Read, Grep, Glob"
    instruction = "Use only local reads for this task."
    if writable:
        tools += ", Write, Edit"
        instruction = (
            "Edit only assigned files in the current checkout. "
            "Use relative paths and keep all edits within this checkout."
        )
    agent.write_text(KIMI_AGENT.format(tools=tools, instruction=instruction), encoding="utf-8")
    # Print mode approves allowed tools. The agent file gates them before execution.
    command = [
        executable, "--environment", "local", "--agent-file", str(agent), "--output-format", "stream-json",
    ]
    if "model" in profile:
        command += ["--model", profile["model"]]
    # Kimi Code's print mode takes the prompt as an argument. Large prompts can
    # hit the OS's argument limit, so pass a private file in that case.
    if len(prompt.encode("utf-8")) > 64_000:
        prompt_file = scratch / "task.txt"
        prompt_file.write_text(prompt, encoding="utf-8")
        prompt = f"Read the full task in {prompt_file} with Read and follow its instructions."
    return command + ["-p", prompt]


def _dsh_command(executable: str, profile: dict, scratch: Path, access: str) -> list[str]:
    rows = [{"id": name, "disabled": True} for name in DSH_DISABLED_ROWS]
    if "model" in profile:
        selection = {"provider": profile.get("provider", "deepseek-official"), "model": profile["model"]}
        if "reasoning" in profile:
            selection["reasoningEffort"] = profile["reasoning"]
        rows.append({"id": "agent-default-model", "config": selection})
    # JSON is a YAML subset. Using JSON here also quotes all user-provided model IDs.
    overlay = scratch / "headless.patch.yml"
    overlay.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    return [
        executable, "--profile", "headless", "--patch", str(overlay),
        "--permission-mode", access, "--approval-policy", "never", "-",
    ]


def _kimi_answer(output: str) -> str:
    final = ""
    for line in output.split("\n"):
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeError("kimi: stdout did not match the stream-json format") from exc
        if not isinstance(message, dict):
            continue
        if message.get("role") == "tool" or message.get("tool_calls"):
            final = ""
        elif message.get("role") == "assistant":
            content = message.get("content")
            if isinstance(content, str):
                final = content
            elif isinstance(content, list):
                final = "".join(
                    part["text"]
                    for part in content
                    if isinstance(part, dict) and part.get("type") == "text"
                    and isinstance(part.get("text"), str)
                )
            else:
                final = ""
    return final


def _capture(
    command: list[str], prompt: str | None, cwd: Path, env: dict, timeout: float, backend: str
) -> str:
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE if prompt is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=os.name == "posix",
        )
    except OSError as exc:
        raise RuntimeError(f"{backend}: could not start the CLI ({exc.strerror})") from exc

    with process:
        try:
            input_bytes = prompt.encode("utf-8") if prompt is not None else None
            output, _ = process.communicate(input_bytes, timeout=timeout)
        except BaseException as exc:
            # Kill the group so a CLI child cannot retain pipes after the deadline.
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except ProcessLookupError:
                pass
            if isinstance(exc, subprocess.TimeoutExpired):
                raise RuntimeError(f"{backend}: timed out after {timeout:g}s") from exc
            raise
        if process.returncode:
            # Provider diagnostics may contain prompt text or credential material.
            raise RuntimeError(
                f"{backend}: CLI exited with code {process.returncode}. Check its version, authentication, and model settings."
            )
    return output.decode("utf-8", errors="replace")


def run_agent(
    profile: dict, prompt: str, cwd: Path, timeout: float, *, writable: bool = False
) -> str:
    executable = check_executable(profile)
    backend = profile["backend"]
    access = "workspace-write" if writable else "read-only"
    env = os.environ.copy()
    with tempfile.TemporaryDirectory(prefix="vb-agent-") as scratch:
        path = Path(scratch)
        if backend == "codex":
            command = _codex_command(executable, profile, cwd, access)
        elif backend == "kimi":
            command = _kimi_command(executable, profile, prompt, path, writable)
            if "reasoning" in profile:
                env["KIMI_MODEL_THINKING_EFFORT"] = profile["reasoning"]
            prompt = None
        elif backend == "dsh":
            command = _dsh_command(executable, profile, path, access)
        else:
            command = [executable, *_custom_command(profile)[1:]]
            env["VB_MULTI_AGENT_ACCESS"] = access
            for key in ("model", "reasoning"):
                if key in profile:
                    env["VB_MULTI_AGENT_" + key.upper()] = profile[key]
        output = _capture(command, prompt, cwd, env, timeout, backend)
    if backend == "kimi":
        output = _kimi_answer(output)
    if not output.strip():
        raise RuntimeError(f"{backend}: no final assistant message")
    return output.rstrip("\n")
