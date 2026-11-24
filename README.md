# AgentTeams Qoder Harness

Run the official Qoder Agent SDK as an independent AgentTeams runtime. Core owns Worker identity, public `task/v1` semantics, execution ownership, artifacts and delivery. This adapter owns the Qoder process, native tools/hooks, session mirror, Matrix transport and launch image.

**Status: experimental implementation, targeting Leader and Worker. Not admission-certified. Manager, Matrix E2EE, image/file message ingress, AgentSpec packages, nested agents and asynchronous background tools are not supported by this candidate.** Task files use the public Task tools. See [measured evidence and remaining gates](docs/acceptance.md).

The initial Core registration is on [`codex/qoder-harness-20261124`](https://github.com/agentscope-ai/AgentTeams/tree/codex/qoder-harness-20261124), commit `63eb100e2f553b4cd629288718c898a6d7b3a7ed`. Use a Core build containing that registration; do not configure `runtime: qoder` on an older Controller. The official Python SDK is pinned to `1.0.17`; its bundled native CLI was verified as `1.1.79`.

## Runtime integration

- Consume the Controller-owned `agents/{workerIdentity}/runtime/runtime.yaml` and credential bindings. Select Leader/Worker from the projected role, not a model argument.
- Register the public role schema through `task_native.py` in an SDK-owned in-process MCP server. The actual discovered tool name is `mcp__agentteams__task`. Framework context remains outside model parameters. Coordination rounds expose only that tool.
- Verify coordination notifications against Controller records. Use shared Task preparation, execution-generation fencing, tool-operation journaling and immutable terminal receipts. `Read`, `Write`, `Edit`, `Bash`, `Glob`, `Grep` are the exposed native tools in execution rounds. Shell calls cannot substitute for the registered Task mutation tool.
- Validate native file writes against the prepared Task output/scratch directories. Preserve native tool-use IDs. Read-only terminal errors can close with no effect; uncertain writes, interrupted calls and nonterminal Bash handles remain pending verification.
- Reuse Core's durable Matrix input ledger and per-device cursor. Mirror SDK-committed transcript rows transactionally to a private SQLite file and object storage before recording native input commitment. Reuse a session only when the public descriptor matches its actual digest and current authority; otherwise reconstruct from shared context.
- Discover recoverable Tasks at startup without a new Matrix message. Saved replies use the public delivery-only path and deterministic transaction identity. The candidate includes the integration paths; real restart/switch acceptance remains open.
- Export actual SDK assistant-response and native tool-hook spans over OTLP HTTP when enabled. Keep prompt and tool bodies out of exported spans. Token counts come only from SDK usage; missing counts are not manufactured. Collector verification remains open.
- Publish native thinking and tool progress as shared `org.agentteams.run` blocks. Tool completion edits preserve the original call ID, parameters and start time. Progress is marked `org.agentteams.progress_only` so it cannot reopen another Agent's work.

## Authentication and Higress routing

Qoder authentication is separate from your AT gateway consumer token. Inject `QODER_SERVICE_ACCOUNT_KEY` or `QODER_PERSONAL_ACCESS_TOKEN` through deployment secrets. If both exist, Service Account takes precedence. Do not put real values in a Worker YAML, image, source file or test output.

For a trusted workstation-only probe, explicitly select `QODER_AUTH_MODE=local` and provide the local Qoder profile at `QODER_CONFIG_DIR`. The container defaults that directory to the member's private `.qoder` directory. Authentication files are never included in the transcript mirror.

The deployment route is **Qoder → Higress → your model provider**, using the standard OpenAI Chat Completions protocol. The adapter automatically consumes the Controller-projected Base URL, model ID and Worker consumer key. Ordinary Model Studio Qwen models and their upstream credentials stay behind Higress. There is no provider-catalog selection, manual BYOK setup, upstream API key injection or Qoder-service fallback in the runtime.

The adapter configures the pinned CLI's native `settings.providers` with `type=openai-compatible`, `protocol=openai`, `baseUrl`, `apiKey` and `model`, then selects `agentteams-gateway/<model>`. Every model-policy callback, including auxiliary purposes, returns that identifier as a string; it never sends inline `custom_model` credentials through the server-side BYOK path. `QODER_BYOK_PROVIDER` is no longer used. The pinned CLI's SDK custom-endpoint feature switch is set by the adapter, not by the operator.

**Live gateway inference remains blocked with the tested SDK/account combination.** Qoder authentication and custom-endpoint permission checks still apply. A real SDK probe using the same direct settings did not expose the selected gateway model as enabled, failed its turn and sent zero requests to the local HTTP fixture. The runtime checks that the selected model is enabled before submitting input and fails explicitly otherwise. This is not a successful Higress inference claim; see the [evidence record](docs/acceptance.md). Changing an upstream Model Studio key cannot resolve a failure before the gateway is contacted.

Gateway MCP endpoints on the projected model gateway origin receive the Worker gateway Bearer token. Other origins receive no gateway credential. Unsupported or missing configuration fails explicitly.

## Build and tests

```sh
python3 -m venv .venv
.venv/bin/pip install -e .
make test PYTHON=.venv/bin/python AT_SOURCE=/path/to/the/Core/candidate

# CONTROLLER_IMAGE must contain agt and role assets from the same Core candidate.
make build AT_CONTEXT=/path/to/the/Core/candidate \
  CONTROLLER_IMAGE=your-built-core-image \
  IMAGE=agentteams/agentteams-harness-qoder:local
```

The default named BuildKit `at` input is pinned to the Core commit in `Makefile`. A local context must contain the matching Core change. Do not push or select an unbuilt release tag. Build publication and certification are separate steps.

Opt-in real SDK probes use tiny synthetic requests and may consume account quota. They stub Controller effects and do not count as AT Task certification:

```sh
AT_SOURCE=/path/to/the/Core/candidate \
  .venv/bin/python tests/live_sdk_smoke.py --local-login

# Turn-adapter probe: Controller effects and model routing are stubbed;
# uses a trusted workstation login and does not validate Higress.
AT_SOURCE=/path/to/the/Core/candidate PYTHONPATH=/path/to/the/Core/candidate/shared/lib \
  .venv/bin/python tests/live_runtime_smoke.py

# Same stubbed-authority boundary, with actual SDK supplement/interruption handling.
AT_SOURCE=/path/to/the/Core/candidate PYTHONPATH=/path/to/the/Core/candidate/shared/lib \
  .venv/bin/python tests/live_runtime_smoke.py --steer
AT_SOURCE=/path/to/the/Core/candidate PYTHONPATH=/path/to/the/Core/candidate/shared/lib \
  .venv/bin/python tests/live_runtime_smoke.py --stop

# Real SDK question and tool denial, synthetic Human/Matrix/Controller only.
AT_SOURCE=/path/to/the/Core/candidate \
  .venv/bin/python tests/live_interaction_smoke.py --local-login

# Direct endpoint diagnostic: real Qoder account checks, local HTTP fixture only.
# A nonzero exit preserves the observed permission/connectivity failure.
.venv/bin/python tests/live_direct_gateway_smoke.py
```

## Deployment boundary

Select `runtime: qoder` and the exact built `spec.image` on your Worker, or reference that Worker as a Team Leader. Core intentionally requires an explicit image. Provide standard Controller URL/token, process identity, Matrix credentials, storage bindings and an unencrypted transport projection. The shared entrypoint reads role assets, restores the native mirror and starts the SDK adapter. `/healthz` requires a recent successful Matrix sync and an enabled projected native gateway model; gateway checking does not issue an inference request. Saved reply recovery can still proceed without model availability.

Native permission requests use the shared `org.agentteams.approval` contract with `reply_format=com.agentteams.answer` and an opaque context ID. Dashboard buttons resume the exact request after room, sender, request and context checks. Approval, rejection, cancellation and expiry have separate receipts. The requesting Human or Team admin can answer; Agent senders cannot authorize side effects. The native `ask_human` MCP tool and external MCP form elicitation use shared question cards with validated, lossless answers. Coordination decision phases retain only their public Task tool.

Personal conversational turns advertise standard `com.agentteams.execution` states and consume `com.agentteams.control`. Stop persists its request before native interruption and reports `stopped` only after a terminal response with no unknown tool operation. Steer uses SDK `priority=next`, a stable Matrix-derived UUID, exact native input persistence and continued response reception. A queued native `attachment/queued_command` is verified by its source UUID and exact text. Accepted supplements retire only after persistence and consumption; uncertain interrupted operations remain fenced for verification. Late supplements remain ordinary follow-up input. Task cancellation remains owned by the public main Task. See [interaction fixes and measured boundaries](docs/interaction-alignment.md); full deployment/admission acceptance remains open.

Keep startup and runtime logs under the shared bounded runtime logger. Native/session/config changes belong here; public protocol changes belong in [Core](https://github.com/agentscope-ai/AgentTeams). The [Harness integration guide](https://github.com/agentscope-ai/AgentTeams/blob/main/docs/design/harness-integration-guide.md) defines the admission gates.

Licensed under Apache-2.0; see [LICENSE](LICENSE).
