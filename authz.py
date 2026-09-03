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


def resolve_audience(request, *, owner_key: str = "") -> str:
    """``anonymous`` / ``owner`` / ``staff`` for this request.

    The axis is ``stapel_attributes.visibility``'s, and the rule is
    ``stapel_listings.serializers.AudienceRedactionMixin.resolve_audience``'s
    — deliberately the same one, because a second "who is this?" predicate is
    a second place to get it wrong, and the thing being gated here (a
    seller's coordinates) is the thing that mixin was extended to gate.

    Two differences follow from the fact that a SERP is not a row:

    * There is no instance to own. ``owner`` is therefore resolved from the
      query's own ``owner`` scope — the seller reading their own list — and
      the answer applies to the whole page, which is sound because
      ``owner_key`` is the only predicate that can restrict a page to one
      seller. A signed-in reader browsing somebody ELSE's list is a stranger.
    * It fails closed. No request (a comm caller, a management command, a
      backend called directly) is ``anonymous``.
    """
    from stapel_attributes import visibility
    from stapel_core.django.api.permissions import IsServiceRequest

    if request is None:
        return visibility.ANONYMOUS
    if IsServiceRequest().has_permission(request, None):
        return visibility.AUDIENCE_STAFF
    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return visibility.ANONYMOUS
    if getattr(user, "is_staff", False) or getattr(user, "is_superuser", False):
        return visibility.AUDIENCE_STAFF
    if owner_key and str(getattr(user, "pk", "")) == str(owner_key):
        return visibility.AUDIENCE_OWNER
    return visibility.ANONYMOUS


def can_manage(request) -> bool:
    """Whether *request* may reindex or read operational health."""
    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    return bool(getattr(user, "is_staff", False) or getattr(user, "is_superuser", False))


__all__ = ["CAPABILITIES", "can_manage", "resolve_audience"]
