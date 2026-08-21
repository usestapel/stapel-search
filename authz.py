"""Who may do what — declared on day 1, so a host's role overlay never migrates.

The public verbs (``query`` / ``suggest`` / ``ranking``) are ``AllowAny``
by design: a marketplace people cannot search without an account is not a
marketplace. They are bounded by their own throttle scopes instead, from
this module's namespace — a library does not own the project's
``DEFAULT_THROTTLE_RATES``.

``health`` and ``reindex`` are operator surface. **The index is a global
corpus with no workspace axis**: one ``search_document`` table serves every
tenant of a deployment, and there is no workspace id on a document to
resolve a membership against. So the shipped gate is the staff mandate,
and ``search.manage`` is declared for hosts whose role model does grade it
— stated here rather than pretending a workspace capability call would mean
something on a table that has no workspace column.
"""
from __future__ import annotations

#: Declared in full on day one.
CAPABILITIES = ("search.query", "search.manage")


def can_manage(request) -> bool:
    """Whether *request* may reindex or read operational health."""
    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    return bool(getattr(user, "is_staff", False) or getattr(user, "is_superuser", False))


__all__ = ["CAPABILITIES", "can_manage"]
