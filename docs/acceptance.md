# Candidate evidence and remaining admission gates

Recorded 2026-11-24. These results are separate evidence layers, not a production acceptance claim.

## Frozen inputs

- Core registration: `63eb100e2f553b4cd629288718c898a6d7b3a7ed`.
- Python SDK: `qoder-agent-sdk==1.0.17` from PyPI.
- Bundled native CLI: `1.1.79` on macOS arm64.
- Native probes: explicit local Qoder login, `auto` account model, temporary working directories and synthetic input. Credentials and private session contents are not included in this repository.
- Harness commit and actual built image identity must be recorded by each deployment; do not infer them from this document.

## Observed results

1. Real SDK inference returned exactly `QODER_SDK_OK`.
2. Real SDK discovery reported the in-process `agentteams` MCP server connected and the tool named `mcp__agentteams__task`.
3. A real native turn read a fixture file, invoked the public role-schema Task binding, persisted the exact native user-message UUID through SessionStore, and returned `SDK_TOOLS_OK`. Original tool-use IDs matched the pre/post native hooks. The Task API was stubbed; no real Task lifecycle was executed.
4. The full `Runtime.execute` adapter, including its options, native MCP discovery, Hook bindings and eager transcript mirror, returned `NATIVE_RUNTIME_OK`. Controller authority, Task API effects and account-model selection were stubbed in this probe. It validates the native turn bindings, not Higress routing. A missing client in the initial probe fixture was corrected before rerunning successfully. The runtime subsequently retained only the required Higress model route; Qoder-service inference is confined to opt-in SDK probes.
5. A real native Bash probe returned structured `kind=completed`, exit code 0 and `interrupted=false`. The adapter requires this terminal shape; a running handle cannot close its operation.
6. A custom-model localhost gateway probe failed with a Qoder custom-model service error, and the local HTTP server observed no request. The tested account's catalog was restricted. This is failed gateway compatibility evidence, not proof that every Qoder account or endpoint is unsupported.
7. The local contract suite covers the actual SDK role schema, SDK/Core MCP namespace separation, tool IDs, read-only errors, unknown effects, file-path containment, coordination-phase tool restriction, transcript durability, model-source selection and native telemetry content exclusion. CI runs these offline contracts without credentials.
8. Core backend and controller package tests passed, including Qoder registration/projection and avoiding the QwenPaw console path. The narrower server/observability packages compiled and the selected runtime checks passed.

## Still required

- A real supported deployment from the formal launcher, including private auth injection, native profile, Matrix sender/room policy and storage restoration.
- Model/gateway compatibility for the actual account, provider, region and installation. A Qoder-service probe does not validate Higress routing.
- The guide's simple 10/10 and complex 10/10 business/protocol cases, measured complete artifact delivery and original-conversation follow-up.
- Exact submission review, role-mixed combinations, duplicate/concurrent inputs, rejection/rework, cancellations, approval rejection, upload/notification failure and unknown tool outcomes.
- Real Worker restart, missing/expired session, both switch directions and a same-task round trip, preserving old native caches/cursors.
- Collector-backed trace correlation and model/tool token completeness checks. Local exporter tests do not prove ingestion.
- Linux amd64/arm64 packaging and formal image/deployment evidence. Record build outcomes separately from local native inference.

Until those gates pass, describe this repository as an experimental adapter and Core registration candidate. Do not claim full Harness admission, production readiness or an independently published image release.
