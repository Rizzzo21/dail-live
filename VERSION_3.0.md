# DAiL v3.0 — Agent Runtime

Adds a bounded, policy-first runtime for agents operating inside the DAiL test world.

Core loop: Observe → Act → Remember.

Runtime actions are explicitly allowlisted: observe, remember, discover, purchase_service, message_lobby, join_room.

Controls include registration, pause/resume, per-agent state, runtime ticks, action logging, and audit events.

This remains test-only and in-memory. It does not provide real-money custody or unrestricted autonomous execution.
