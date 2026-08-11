"""Business modules.

Each module owns its own API surface, services, domain rules, models and
repositories (ADR-0011). A module may depend on `app.core` and `app.platform`;
it must never reach into a sibling module's internals. Cross-module
communication goes through a published service interface or, from Phase 4, the
event backbone.
"""
