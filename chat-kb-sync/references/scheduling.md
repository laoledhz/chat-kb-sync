# Scheduling and recovery

Set up a schedule only after the owner has approved automated processing and a manual fictional dry-run has confirmed discovery, indexing, revision update and idempotence.

1. Confirm the desired **local wall-clock hour** (often 03:00) and computer time zone. Choose one local scheduler; don't enable several for the same archive. Have it use a fixed Python interpreter, absolute script/config paths and the same persistent private work directory. Copy no secrets into its user-visible prompt or arguments. If the scheduler cannot inherit safely provided credentials or reach loopback, leave automatic publishing off.
2. In Codex Desktop use [Scheduled tasks](https://learn.chatgpt.com/docs/automations) for a local project task. Keep the machine and desktop app running, ensure that the private working path and local RAGFlow are available, and confirm its unattended permissions using a synthetic setup. Ask the scheduler to run the configured command, not to read private histories into an agent prompt. Do not schedule by modifying undocumented app settings.
3. A compatible shell/task scheduler can run the same CLI. Use the OS service's local execution environment, a current supported Python and protected credentials. This Skill installs **no scheduler by itself**. A missed run isn't data loss: the next run checks the current source and saved hashes, not a rolling “last 24 hours” cutoff.
4. Record aggregate counts only. Retry pending ingestion at the next authorized run; if a source is held or unreadable, keep prior notes and investigate before considering deletion. On changing installation or restarting after failure, keep both work-state files and consult the dataset guide for reconciliation. An interrupted run is not evidence of completion.

## Optional readiness checks and startup

In the private config under `ragflow`, set `"readiness": {}` for a bounded preflight to the configured loopback RAGFlow health API. It checks the application and dependencies, not just an open port. A refused connection, unready dependency or incompatible response is not a successful archive. Scanning in `sync` happens before the readiness check, so local drafts stay available to the next run.

With separate authorization for unattended startup, add a `launcher_argv` array to that object (an absolute executable followed by individual arguments) and use `--allow-start` with `sync` or `publish`. For Windows PowerShell use the absolute `powershell.exe` followed by `-NoProfile -NonInteractive -File` and the absolute path to your existing, no-browser RAGFlow start script. Configure this only in a protected local file and never use code or commands pulled from conversations. Keep the ordinary scan command unchanged.

The launcher runs at most once per attempt, only for a confirmed refused loopback connection, not for rejected permissions or an incorrect service. It must not restart unrelated services; no Docker installation, machine wake-up, arbitrary shell command, model call or new schedule is performed by these scripts. Launch and readiness wait are bounded. The exit code is reported; startup stdout/stderr remain in a private `.ragflow-start-*.log` in the work directory (never put it in Git or in a prompt). Keep the same work directory and data binding on failure so the next run can continue.

