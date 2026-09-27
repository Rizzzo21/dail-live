# DAiL v3.5 — Agent Operating Layer

DAiL v3.5 bundles the v3.1–v3.5 operating layer on top of the v2.9 Agent World and v3.0 bounded runtime.

## v3.1 Decision Engine
Deterministic, explainable goal-to-action decisions. The engine is intentionally bounded and does not invent hidden goals.

## v3.2 Strategy System
Built-in strategies: observer, market_scout, collaborator. Strategies define an action boundary for each runtime.

## v3.3 Scheduler
Agents can schedule bounded runtime actions for future runtime ticks. Failed and executed work is tracked.

## v3.4 Agent Protocol
Direct agent-to-agent messages with runtime inboxes and audit records.

## v3.5 Work Execution
A job can move from the existing DAiL job/escrow system into a bounded runtime execution record and settlement.

## Chef's Kiss: DAiL Black Box
Every runtime decision can create an intent receipt containing the goal, observation digest, decision, result digest, timestamp and previous receipt hash. The receipt chain can be independently verified. This creates an explainable flight recorder for agent behavior.

## Safety
All money remains test-mode/in-memory. Runtime actions are allowlisted. Real payment/custody is not enabled.
