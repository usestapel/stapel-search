"""Read-only admin over the index.

Nothing here is editable, and that is the point: every row is derived from
a source through a mapper, so a human correcting an index entry by hand
produces a value the next re-index silently reverts. The models carry
``@access.ops`` for the same reason — the fix belongs at the source or in
the mapper, never here.
"""
from django.contrib import admin

from .models import SearchDocument, SearchNumber, SearchSignal


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(SearchDocument)
class SearchDocumentAdmin(ReadOnlyAdmin):
    list_display = ("doc_type", "doc_key", "title", "visible", "promoted", "indexed_at")
    list_filter = ("doc_type", "visible", "promoted", "language")
    search_fields = ("doc_key", "title")
    ordering = ("-indexed_at",)


@admin.register(SearchNumber)
class SearchNumberAdmin(ReadOnlyAdmin):
    list_display = ("document", "slug", "value")
    list_filter = ("slug",)


@admin.register(SearchSignal)
class SearchSignalAdmin(ReadOnlyAdmin):
    """Kept visible: this is the answer to "why is this listing at the top?"."""

    list_display = ("doc_type", "doc_key", "kind", "value", "expires_at", "received_at")
    list_filter = ("doc_type", "kind")
    search_fields = ("doc_key",)
    ordering = ("-received_at",)
