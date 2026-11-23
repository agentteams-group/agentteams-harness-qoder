# Qoder Harness implementation

Implement Qoder-native behavior here. AgentTeams Core owns `task/v1`, roles, Task state, artifact delivery and recovery. Read the Core Harness integration guide before changing those boundaries.

- Keep model arguments separate from framework role, event sources, Task scope and execution generation.
- Register the exact public role schema in the official SDK. Journal non-Task tools before their effects, retaining native call IDs and terminal receipts.
- Reuse the Core Matrix input ledger, Task recovery and reply identity. Native sessions are optional caches, not execution authority.
- Do not enable nested agents or asynchronous background tools until their child operations have full admission and terminal evidence.
- Keep unsupported capabilities explicit. Local tests, real SDK transport, real inference, image builds and admission certification are separate evidence.
- Pin the SDK and Core build input. Never commit credentials or session contents.
- Write runtime prompts and skills directly to the agent as "you".
