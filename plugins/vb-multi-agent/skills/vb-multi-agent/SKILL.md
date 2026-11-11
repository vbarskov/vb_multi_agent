---
name: vb-multi-agent
description: Run the configured router, parallel developers, tester, and reviewer on a Git repository when the user explicitly invokes VB Multi Agent.
---

Use the bundled `scripts/run.py` from this skill's directory to coordinate the job. Resolve its path against this installed skill. The runner works at every host reasoning level that supports shell commands. Follow the target repository's applicable instructions and the current session's permissions. Developers edit only their assigned files in separate copies. The runner validates and combines their Git changes. Tester and reviewer inspect the combined result in read-only mode.

1. Use the repository and task the user specified, with the current Git repository as the default target. Use the user's config when supplied. The bundled default uses Codex for all four roles. Run `python3 RUNNER --check` before a run, adding `--config CONFIG` for a custom config.
2. Run `python3 RUNNER --repo REPO --task TASK` and add the same `--config CONFIG` when supplied. Pass task text as a single literal argument and paths as separate arguments. Add `--out PATH` only for a user-selected new output directory. Surface a dirty repository or an unavailable CLI and let the user decide how to proceed.
3. Include `--test-command` only when the user has authorized that exact command in the applicable repository. Pass each command as a JSON array of arguments. The tester can inspect the changes and propose checks when no commands are authorized.
4. Report the paths to the patch and report, which checks actually ran, and the reviewer's actionable findings with file and line references. Apply the patch to the original repository only on a separate explicit request.
