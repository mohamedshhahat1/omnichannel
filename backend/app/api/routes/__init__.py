"""HTTP routers.

`health` is mounted at the application root and is deliberately unversioned:
probes are an operational contract with a load balancer, not part of the public
API, and an orchestrator should not have to follow an API version bump.

`v1` is the versioned public API. It is empty in Phase 1 and is where business
routers are mounted from Phase 3 onward.
"""

__all__: list[str] = []
