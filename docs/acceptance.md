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
6. The initial inline `custom_model` localhost probe entered Qoder's server-side BYOK path and failed with a custom-model service error before any HTTP request reached the fixture. The account's catalog was restricted; this does not establish the capabilities of the separate direct-provider path. A subsequent probe configured `settings.providers` with `type=openai-compatible`, `protocol=openai`, a localhost `/v1` URL, a synthetic key and model, then selected `agentteams-gateway/fixture`. Both with and without the pinned CLI's `QODER_SDK_CUSTOM_BASE_URL_BYOK=1` feature switch, Qoder returned `You do not have access to this model service. Please check your account permissions.` and the fixture observed zero requests. Real Qoder authentication and account checks were retained. These failures remain historical evidence.
7. The adapter now uses the native OpenAI-compatible direct-provider settings, with no catalog provider or inline `custom_model` payload. A new `QoderSDKClient` probe uses the actual `gateway_options` function and a string-only model-policy callback. `get_available_models()` did not report the selected model as enabled. The turn returned `Sorry, something went wrong. Please try again. Report Issue (input /feedback)` and the fixture again observed zero requests. The reproducible diagnostic is `tests/live_direct_gateway_smoke.py`; its nonzero exit is intentional on failure. Static inspection of the pinned CLI found that SDK custom endpoints require its feature switch and a server account status satisfying `featureSwitches.allow_byok >= 2`. The adapter enables the SDK switch but preserves account authentication and entitlement checks. The tested account's actual server feature value has not been retrieved, so the precise account/configuration cause remains unconfirmed. The runtime now rejects an unavailable gateway model before submitting input instead of returning the native generic error or falling back. No successful Higress inference is established.
8. All 32 local contract tests passed after the direct-provider change. They cover the actual SDK role schema, SDK/Core MCP namespace separation, tool IDs, read-only errors, unknown effects, file-path containment, coordination-phase tool restriction, transcript durability, approval/run binding, delivery-only recovery and native telemetry content exclusion. New checks exercise `Runtime.execute` options and the installed SDK's model-policy wire response: main and auxiliary purposes select the gateway identifier without a `custom_model` payload. Incomplete gateway configuration fails, obsolete model/provider environment settings cannot select a fallback, and an unavailable gateway model prevents input submission. [Hosted CI passed on the earlier implementation commit](https://github.com/agentteams-group/agentteams-harness-qoder/actions/runs/45218569520); that run predates this direct-provider change. CI runs the offline contracts without credentials.
9. Core backend and controller package tests passed, including Qoder registration/projection and avoiding the QwenPaw console path. The narrower server/observability packages compiled and the selected runtime checks passed.
10. Earlier local Linux arm64 packaging passed with SDK `1.0.17`, bundled CLI `1.1.79`, the public `agt task` entrypoint and 30/30 offline tests inside the image with networking disabled. That image is `agentteams/agentteams-harness-qoder:local-2cf28a8`, ID `sha256:a14cf5b8f906ccb18fb13ca1ad841f6dd5fde3fd7163d690ca056e963a4ac3b7`. All nine installed adapter Python files matched the source digests at `2cf28a82647fca1166b95203cc7486bb146c4997`. This image predates the direct-provider change and must not be used as its packaging evidence. It was not published or deployed to Matrix.
11. The earlier local build first installed the pinned SDK/Core dependencies using the repository Dockerfile, then refreshed the application package without changing those dependency inputs. Source/CLI/image checks were performed on the refreshed image. Build recipes and per-file digests are retained in the local task evidence directory.

## Still required

- A real supported deployment from the formal launcher, including private auth injection, native profile, Matrix sender/room policy and storage restoration.
- Model/gateway compatibility for the actual account, provider, region and installation. A Qoder-service probe does not validate Higress routing.
- The guide's simple 10/10 and complex 10/10 business/protocol cases, measured complete artifact delivery and original-conversation follow-up.
- Exact submission review, role-mixed combinations, duplicate/concurrent inputs, rejection/rework, cancellations, approval rejection, upload/notification failure and unknown tool outcomes.
- Real Worker restart, missing/expired session, both switch directions and a same-task round trip, preserving old native caches/cursors.
- Collector-backed trace correlation and model/tool token completeness checks. Local exporter tests do not prove ingestion.
- Linux amd64 packaging, formal publication and live deployment evidence. The local arm64 image checks above do not establish these layers.

Until those gates pass, describe this repository as an experimental adapter and Core registration candidate. Do not claim full Harness admission, production readiness or an independently published image release.
