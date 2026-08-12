"""Authorization checks.

Pure functions over a resolved `Principal`. They raise from the existing error
taxonomy so `app.api.exception_handlers` renders them into the standard
envelope with no special casing.

The status codes are a security decision, not an aesthetic one:

* **403** when the caller is a member of the tenant but lacks the permission.
  They already know the tenant exists, so nothing is leaked.
* **404** when the caller has no membership in the tenant at all. A 403 there
  would confirm the tenant exists to someone with no relationship to it, which
  turns tenant ids into an enumerable directory of customers.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from app.modules.identity.domain import Permission, Principal
from app.modules.identity.errors import PermissionDeniedError, TenantNotFoundError


def require_permission(principal: Principal, permission: Permission) -> None:
    """Raise `PermissionDeniedError` unless the principal holds `permission`."""
    if principal.has_permission(permission):
        return
    raise PermissionDeniedError(
        details={"required_permission": permission.value},
        internal_message=(
            f"principal kind={principal.kind.value} tenant={principal.tenant_id} "
            f"lacks {permission.value}"
        ),
    )


def require_all_permissions(principal: Principal, permissions: Iterable[Permission]) -> None:
    """Raise on the first permission the principal does not hold."""
    for permission in permissions:
        require_permission(principal, permission)


def require_tenant_match(principal: Principal, tenant_id: uuid.UUID) -> None:
    """Raise `TenantNotFoundError` when a request names another tenant.

    Reached only when a route accepts a tenant id in the path. The principal's
    tenant always comes from the session or API key, so a mismatch means the
    caller is reaching across the boundary and gets the same answer as if the
    tenant did not exist.
    """
    if principal.tenant_id == tenant_id:
        return
    raise TenantNotFoundError(
        internal_message=(
            f"cross-tenant access attempt: principal tenant={principal.tenant_id} "
            f"requested tenant={tenant_id}"
        ),
    )
