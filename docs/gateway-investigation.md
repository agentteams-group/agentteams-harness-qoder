# Qoder gateway investigation — 2026-11-25

The current failure is before the model endpoint receives a request. It does not demonstrate an OpenAI protocol incompatibility in Higress or that Qoder has no direct-provider transport. There are separate SDK/runtime compatibility, provider-registration and account-access issues.

## SDK and runtime versions

The adapter uses the official local Agent SDK, not the Cloud Agents API client. Qoder documents Python, TypeScript and Java as clients of the local Qoder CLI runtime: [SDK overview](https://docs.qoder.com/cli/sdk/overview), [runtime architecture](https://docs.qoder.com/cli/sdk/how-it-works).

The package registries checked on this date expose Python `qoder-agent-sdk==1.0.17` with bundled CLI `1.1.79`, and TypeScript `@qoder-ai/qoder-agent-sdk@1.0.61` whose package metadata pins CLI `1.1.96`. The standalone CLI manifest offers `1.1.97`. The language packages do not share version numbers or bundled runtime versions. The official [CLI release notes](https://changelog.qoder.com/cli) record enterprise BYOK direct inference in `1.1.89`.

Pointing Python SDK `1.0.17` at CLI `1.1.97` fails with exit code `42`. The CLI log gives the actual cause:

```text
sdk_worker_removed: SDK worker sessions are no longer supported. Upgrade to the latest Qoder SDK version, or roll back the CLI to 1.1.80 or earlier.
```

This is a protocol-generation incompatibility, not an invalid gateway key. Do not update only `cli_path` in the existing adapter.

## Native provider configuration and account checks

The pinned CLI recognizes `providers.<id>` with `type: openai-compatible`, `protocol: openai`, `baseUrl`, `apiKey` and `model`. Changing the provider type to Alibaba Cloud Model Studio would not remove Qoder's access check. In CLI `1.1.79`, SDK direct providers require both the SDK feature switch and a successful account check equivalent to `featureSwitches.allow_byok >= 2`; these checks were inspected without changing them.

The newer CLI has a different provider manager. Injecting only flag-level `options.settings.providers` did not make the test provider selectable in the tested session. Returning its namespaced identifier through model policy produced `outerProvider is required for external provider model "agentteams-gateway/fixture"`. Adding `parameters.outerProvider` then produced `External provider model "agentteams-gateway/fixture" was not returned for provider "agentteams-gateway"`. An absent entry in `getAvailableModels()` alone therefore cannot diagnose account entitlement, and supplying `outerProvider` alone does not register a provider.

Loading the same synthetic provider through temporary project settings reached the provider access check. Both TypeScript SDK `1.0.61` + its paired CLI `1.1.96`, and the same SDK + CLI `1.1.97`, returned:

```text
Custom Provider access is not allowed for the current account.
```

These probes used real local Qoder authentication (`daemon.auth()` returned `true`), an isolated temporary workspace, no tool access, and a localhost OpenAI Chat Completions fixture with a synthetic key and model. The fixture received **zero requests**. Model policy always selected that test provider, so the failed dynamic probes did not fall back to an account model. No account flags, auth checks or CLI code were patched. The exact server feature-switch value and whether this account can be granted the required SDK access remain unverified.

A separate fixed-model diagnostic did fall back to `auto` when the provider was unavailable. Its account-model response is not gateway evidence. Production must retain fail-closed routing and must not adopt that fallback.

## Product documentation and next implementation step

The [desktop custom-model documentation](https://docs.qoder.com/qoder/custom-models) explicitly supports OpenAI-compatible custom endpoints. The [CLI documentation](https://docs.qoder.com/cli/custom-models) instead describes an account-dependent Custom wizard and instructs users not to configure BYOK manually in `settings.json`. Desktop support is not proof of unattended SDK access. The temporary project-settings probe above is a diagnostic of the native transport/access boundary, not a newly supported production setup procedure.

Before migrating the adapter, obtain a supported SDK custom-endpoint configuration contract and eligible authentication from Qoder, then test the same pinned SDK/CLI pair against the localhost fixture and real Higress. Endpoint, model and consumer-key projection can remain automatic inside the adapter; users should not need a separate manual provider-selection workflow. Changing Python to TypeScript can solve the runtime-generation mismatch, but it does not itself grant Custom Provider access.

Keep the current candidate experimental. A successful future gateway probe must verify the actual request path, model and authorization at the receiving endpoint, then repeat the existing approval, tool, stop, steer, Task and recovery admission checks after any SDK migration.
