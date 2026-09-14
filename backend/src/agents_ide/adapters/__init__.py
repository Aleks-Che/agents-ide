"""Adapter interfaces for agent harnesses and LLM providers.

The interfaces are intentionally small: each adapter returns a single
:class:`AgentResult` or :class:`LLMResult` per external call. State, retries and
fallback logic live in the engine runner — adapters never decide policy.

Real Codex and OpenCode adapters are introduced at stage 6. The fake adapter
(stage 4) drives the engine through synthetic responses so the runner,
candidate selection and queue machinery can be exercised without a real harness.
"""
