# Interaction and Task integration fixes

Recorded 2026-11-24 against SDK `1.0.17` / native CLI `1.1.79`. These fixes preserve Core-owned Task state and reuse the existing Dashboard wire contracts. No Qoder-specific Dashboard command or parser exception was added.

## Corrected paths

| Path | Result |
| --- | --- |
| Saved replies | The Matrix client implements `send_content`; the real shared delivery helper binds the sending device before HTTP submission. Conversational replies link to the original source event so confirmed history can reconstruct both sides. |
| Approvals | Generic structured replies, opaque context IDs, request/room/sender/context binding, mention-prefix handling, and distinct approved/rejected/cancelled/expired receipts replace the incompatible command-only payload. |
| Host permission denial | A real SDK probe showed that this version does not emit `PermissionDenied` after a host callback rejects a tool. The adapter saves its own pre-dispatch denial receipt before returning `PermissionResultDeny`. The exact call carries a registered pre-effect error code; unknown writes are not relabelled as harmless failures. A later duplicate denial hook is idempotent. |
| Stop | Standard execution/control envelopes, persisted control intent, native interruption, terminal acknowledgement and truthful unavailable state for unknown tool effects. An interruption RPC acknowledgement alone never produces a stopped receipt. |
| Steer | Safe-boundary native input (`priority=next`), deterministic source UUID, per-source native commitment, continued reception across native responses, and no retirement before commitment and consumption. Completion races preserve late input as a follow-up. |
| Native supplement storage | The real SDK persists an injected supplement as `attachment/queued_command`, not an ordinary user row. Its `source_uuid` and exact prompt are verified before the Controller receives a native commitment. Model-input trace attribution includes committed supplements. |
| Thinking/tools | Shared v1 blocks, stable native tool IDs, terminal edits retaining parameters and timestamps, bounded content, tool filtering and coordination-phase thinking suppression. Every progress message carries `org.agentteams.progress_only`. |
| Questions | Native in-process `ask_human` plus MCP form elicitation emit shared question cards. IDs/options, scalar types, multi-selection and schema constraints are validated before native resumption. URL authorization and referenced schemas are not treated as answered forms. |
| Startup and policy | Existing room membership is refreshed on startup. Authorized operator controls remain subject to explicit channel denial. Readiness requires Matrix sync and native gateway-model availability; saved delivery does not require inference. Malformed and stale controls cannot grant a different request. |
| Task and tracing | Public Leader/Worker schemas and coordination restrictions remain in Core. The turn trace is attached for native callbacks/shared CLI propagation and restored on exit. |

## Measured evidence

- Local contracts exercise the actual SDK schema/wire bindings, Controller/Core helpers, real localhost Matrix HTTP delivery, control ownership, native transcript storage, tool denial receipts and unknown outcomes. The test count and packaging evidence for each frozen commit are recorded in the acceptance record.
- The Dashboard consumer check feeds actual adapter-emitted payloads into the real pinned Dashboard parsers (`96881f2a2a69a06f616c758b04bbddbec08111b8`). Approval, question/receipt, tool, thinking and execution-control shapes pass. CI runs this check without installing the full Dashboard application.
- A real SDK tool probe returned `SDK_TOOLS_OK` after these changes, with original native hook IDs and exact initial-input commitment. Its Task API is stubbed.
- `live_interaction_smoke.py --local-login` returned `INTERACTION_OK`: the real native question received a synthetic Human answer, the real Bash request was rejected, no rejected output file existed, and no native operation remained unconfirmed. The first attempt exposed the missing host-denial hook; the corrected pre-dispatch receipt path passed.
- `live_runtime_smoke.py --steer` returned `STEER_LIVE_OK` through `Runtime.execute`, with separate commitments for the original and supplemental Matrix sources. The initial probe fixture lacked a control claim response; after fixing that fixture, the probe exposed the native queued-attachment format and passed after the adapter handled that exact format.
- `live_runtime_smoke.py --stop` produced `running → stopping → stopped` after native interruption. This probe interrupts inference without a side-effecting tool; unknown in-flight tool effects are separately tested to remain unavailable for verification.

The live SDK probes deliberately use an existing workstation login and an account model, with Controller authority and Matrix transport replaced by synthetic fixtures. They do not prove Higress inference, live Dashboard rendering, real Task mutation, business acceptance, restart or cross-Harness switching. Production has no account-model fallback. The custom-endpoint SDK/account compatibility gate and the full admission requirements remain in [acceptance.md](acceptance.md).
