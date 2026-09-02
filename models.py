"""The materialized index table — owned by this module, in BOTH topologies.

Why not a :class:`stapel_core.comm.projections.Projection`. Its local mode
has, by definition, **no table** and a mandatory ``live_query``
(``comm/projections.py:289-299``); its only accessor is a keyed batch
``read(name, keys)`` (``:436-479``). Search answers "find the matching
ones", not "give me fields for these keys" — there is no tsvector, no GIN
and no facet aggregate to build on a keyed lookup. So the index is a real
table.

What *is* borrowed is all four of the Projection guarantees, by name and by
contract: idempotency on redelivery (``source_event_id``), ordering by
sequence (``source_seq``), ``rebuild`` from the owner's source of truth
through the snapshot contract ``{cursor, limit} -> {rows, cursor, total}``,
and ``drift_check``. What changes between a monolith and a split is the
transport that delivers the invalidation, never the code
(``comm/projections.py:33-37``).

Portability is deliberate: every column here works on SQLite, because the
naive backend exists so that a test or a zero-infra demo gets an honest
``capabilities.typo_tolerance = False`` instead of a silent ``icontains``
fallback hidden inside the Postgres backend. The Postgres-only index
structures (``tsvector``, two ``text[]`` companions, GIN) are added by
migration ``0002`` and maintained by the Postgres backend — a backend
maintaining its own engine's structures is exactly what the seam is for.
"""
from __future__ import annotations

from django.db import models

# Import from the declaration module, not the package root: models are
# imported before the auth machinery in some app orders (the precedent is
# stapel-core/django/outbox/models.py:12-15).
from stapel_core.access.declaration import access


@access.ops  # derived rows: an admin hand-editing the index is a bug, never an operation
class SearchDocument(models.Model):
    """One indexable document, engine-neutral.

    ``(doc_type, doc_key)`` is the identity — ``doc_key`` is the source's
    own opaque key, so an index row and its source row can always be
    reconciled without a translation table.
    """

    doc_type = models.CharField(max_length=32, db_index=True)
    doc_key = models.CharField(max_length=64)

    # --- membership / scoping ---------------------------------------------
    #: The in-index predicate. Set from the PULLED document's ``status``,
    #: never from the event name: republishing a live listing emits
    #: ``listing.updated`` carrying ``status: pending`` and emits no
    #: ``listing.removed`` at all (spec §19.7).
    visible = models.BooleanField(default=True, db_index=True)
    language = models.CharField(max_length=10, blank=True, default="", db_index=True)
    owner_key = models.CharField(max_length=64, blank=True, default="", db_index=True)
    #: root->leaf, built by this module from stapel-categories: the listings
    #: source serves ``category_id`` only and has no tree (spec §19.1).
    category_path = models.JSONField(default=list, blank=True)

    # --- text ---------------------------------------------------------------
    title = models.CharField(max_length=512, blank=True, default="")
    body = models.TextField(blank=True, default="")
    #: Title/badge attribute values, joined — weight B.
    text_extra = models.TextField(blank=True, default="")
    #: Case-folded, diacritic-folded concatenation. Folded in Python
    #: (``text.fold``) rather than by the Postgres ``unaccent`` extension so
    #: that every backend's trigram/substring arm sees the same bytes.
    text_plain = models.TextField(blank=True, default="")

    # --- facets ---------------------------------------------------------------
    #: ``{slug: [values]}`` — the authoritative filter structure. On Postgres
    #: this is jsonb and carries a ``jsonb_path_ops`` GIN: the predicate is
    #: always containment, and other operators are not needed.
    facets = models.JSONField(default=dict, blank=True)
    #: ``["slug=value", ...]``, path slugs expanded to EVERY prefix — the
    #: counting structure. Rollup keys on the path prefix rather than on the
    #: bare value because hierarchical_select only guarantees uniqueness
    #: among siblings (stapel-attributes/type.py:35-71).
    facet_terms = models.JSONField(default=list, blank=True)

    # --- sort keys ---------------------------------------------------------
    price_base = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True, db_index=True)
    #: Fed only by ``search.signal``; 0 until an emitter exists.
    popularity = models.IntegerField(default=0)

    # --- geo -----------------------------------------------------------------
    lat = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    lon = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    geohash = models.CharField(max_length=12, blank=True, default="", db_index=True)

    # --- ranking / promotion --------------------------------------------------
    boost = models.FloatField(default=0.0)
    #: DSA Art. 26 marker. Serialized on EVERY item, including ``false``.
    promoted = models.BooleanField(default=False)
    promotion_expires_at = models.DateTimeField(null=True, blank=True, db_index=True)

    # --- hydration -------------------------------------------------------------
    #: Stored result-row fields (verdict §18.4): one query per result page,
    #: no N+1 hydration hop in a split topology.
    card = models.JSONField(default=dict, blank=True)

    # --- bookkeeping: the Projection guarantees, borrowed by name --------------
    source_seq = models.BigIntegerField(default=0)
    source_event_id = models.CharField(max_length=64, blank=True, default="")
    indexed_at = models.DateTimeField(auto_now=True, db_index=True)
    source_updated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "search_document"
        constraints = [
            models.UniqueConstraint(fields=["doc_type", "doc_key"], name="search_doc_identity"),
        ]
        indexes = [
            models.Index(fields=["doc_type", "visible", "-published_at"], name="search_doc_recent_idx"),
            models.Index(fields=["doc_type", "visible", "price_base"], name="search_doc_price_idx"),
            models.Index(fields=["doc_type", "owner_key"], name="search_doc_owner_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.doc_type}:{self.doc_key}"


@access.ops  # derived rows, same reasoning as SearchDocument
class SearchNumber(models.Model):
    """Numeric facet values, one row per (document, slug).

    A btree index on a JSONB path would need a migration per slug — flatly
    impossible against an open category schema — and ``jsonb_path_ops`` GIN
    answers containment, not ``<``/``>``. A narrow side table with
    ``INDEX(slug, value)`` turns every range predicate into an indexed
    semi-join instead. The price is one join per predicate, which is why
    ``MAX_RANGE_FILTERS`` is a closed switch.

    Meilisearch has no equivalent table: there, ``numeric.<slug>`` is a
    native filterable attribute. That difference is real, and the contract's
    job is not to hide it but to stop it leaking: from outside, both engines
    answer ``r.year=2015..``.
    """

    document = models.ForeignKey(
        SearchDocument, on_delete=models.CASCADE, related_name="numbers"
    )
    slug = models.CharField(max_length=64)
    value = models.DecimalField(max_digits=18, decimal_places=6)

    class Meta:
        db_table = "search_number"
        constraints = [
            models.UniqueConstraint(fields=["document", "slug"], name="search_number_identity"),
        ]
        indexes = [
            models.Index(fields=["slug", "value"], name="search_number_range_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.slug}={self.value}"


@access.standard  # read by humans asking "why is this listing at the top?"
class SearchSignal(models.Model):
    """Audit of the one inbound door for promotion and popularity.

    v1 has no ``PromotedListing``, no tiers and no promo-slot marketplace
    (spec §14): the index carries ``boost`` / ``promoted`` /
    ``promotion_expires_at`` and this table records who moved them. Adding
    a paid-promotion product later is a new emitter, not a redesign of the
    index.
    """

    KIND_BOOST = "boost"
    KIND_POPULARITY = "popularity"
    KIND_PROMOTED = "promoted"
    KINDS = (
        (KIND_BOOST, "boost"),
        (KIND_POPULARITY, "popularity"),
        (KIND_PROMOTED, "promoted"),
    )

    doc_type = models.CharField(max_length=32)
    doc_key = models.CharField(max_length=64)
    kind = models.CharField(max_length=16, choices=KINDS)
    value = models.FloatField(default=0.0)
    expires_at = models.DateTimeField(null=True, blank=True)
    #: Idempotency on redelivery, same guarantee as the index itself.
    event_id = models.CharField(max_length=64, blank=True, default="")
    received_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "search_signal"
        indexes = [
            models.Index(fields=["doc_type", "doc_key"], name="search_signal_target_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.doc_type}:{self.doc_key} {self.kind}={self.value}"




@access.ops  # derived rows: rebuilt from the corpora, never hand-edited
class VectorEmbedding(models.Model):
    """Metadata half of the vector store — one row per embedded string.

    The ``embedding`` column itself is NOT here: it is an extension type
    (pgvector), added by migration ``0003`` only where the extension
    exists and touched only by ``vector/store.py``'s raw SQL. This model
    carries what every deployment can carry — which string, of which
    corpus, embedded under which model tag — so admin, counts and pruning
    work even where the vectors cannot.
    """

    #: Corpus name from ``VECTOR_CORPORA`` («category», «vocab_label», ...).
    kind = models.CharField(max_length=32)
    #: Stable id within the kind — a pk, or a hash of the folded text.
    key = models.CharField(max_length=160)
    #: The exact string that was embedded.
    text = models.TextField()
    #: What a consumer needs to render a row from a bare hit.
    payload = models.JSONField(default=dict, blank=True)
    #: ``<model>@<dims>`` — the embedding SPACE this row lives in. A tag
    #: mismatch is how a needed re-embed is detected, never searched across.
    model_tag = models.CharField(max_length=64)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "search_vector_embedding"
        constraints = [
            models.UniqueConstraint(
                fields=["kind", "key"], name="search_vector_kind_key_uniq"
            ),
        ]
        indexes = [
            models.Index(
                fields=["kind", "model_tag"], name="search_vector_kind_tag_idx"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.kind}:{self.key} ({self.model_tag})"


__all__ = ["SearchDocument", "SearchNumber", "SearchSignal", "VectorEmbedding"]
