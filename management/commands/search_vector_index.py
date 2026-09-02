"""Build (or size) the vector index from the registered corpora.

``--estimate`` prints the corpus and its token/cost arithmetic and STOPS —
the embedding spend is an owner-visible number before it is a bill. The
estimate is heuristic (≈1 token / 2.5 chars, the cl100k ballpark for the
mixed RU/EN short strings this index holds); the build prints the
provider-reported usage afterwards, which is the real number.
"""
from __future__ import annotations

import time

from django.core.management.base import BaseCommand, CommandError

#: text-embedding-3-small, USD per 1M tokens (2026-02 price card).
_PRICE_PER_MTOKEN = 0.02
_CHARS_PER_TOKEN = 2.5


class Command(BaseCommand):
    help = "Embed the registered corpora into the pgvector store."

    def add_arguments(self, parser):
        parser.add_argument("--kind", help="one corpus kind (default: all registered)")
        parser.add_argument(
            "--estimate",
            action="store_true",
            help="print corpus sizes and the cost arithmetic; embed nothing",
        )
        parser.add_argument(
            "--batch",
            type=int,
            default=512,
            help="texts per embedding request (default 512)",
        )

    def handle(self, *args, **options):
        from django.utils import timezone

        from stapel_search.vector import corpus, service, store

        registry = corpus.providers()
        kinds = [options["kind"]] if options.get("kind") else sorted(registry)
        if not kinds:
            raise CommandError(
                "no corpora registered — STAPEL_SEARCH['VECTOR_CORPORA'] is empty"
            )
        for kind in kinds:
            if kind not in registry:
                raise CommandError(f"no corpus provider for kind {kind!r}")

        if options["estimate"]:
            total_texts = total_chars = 0
            for kind in kinds:
                texts = chars = 0
                for entry in corpus.entries(kind):
                    texts += 1
                    chars += len(entry["text"])
                total_texts += texts
                total_chars += chars
                self.stdout.write(f"{kind}: {texts} texts, {chars} chars")
            tokens = int(total_chars / _CHARS_PER_TOKEN)
            cost = tokens / 1_000_000 * _PRICE_PER_MTOKEN
            self.stdout.write(
                f"TOTAL: {total_texts} texts, ~{tokens} tokens, "
                f"~${cost:.4f} at ${_PRICE_PER_MTOKEN}/1M "
                f"({service.model_tag()})"
            )
            return

        if not store.ensure_schema():
            raise CommandError(
                "vector store unavailable — Postgres with the pgvector "
                "extension is required (CREATE EXTENSION vector)"
            )

        tag = service.model_tag()
        batch_size = max(1, int(options["batch"]))
        for kind in kinds:
            started = timezone.now()
            clock = time.monotonic()
            written = 0
            batch: list[dict] = []

            def flush(batch):
                nonlocal written
                if not batch:
                    return
                vectors = service.embed_texts([entry["text"] for entry in batch])
                if vectors is None:
                    raise CommandError(
                        f"embedding batch failed at {written} rows of {kind!r} — "
                        "the store keeps the finished rows; rerun to resume"
                    )
                written += store.upsert_many(
                    kind,
                    [
                        (entry["key"], entry["text"], entry.get("payload") or {}, vector)
                        for entry, vector in zip(batch, vectors)
                    ],
                    model_tag=tag,
                )

            for entry in corpus.entries(kind):
                batch.append(entry)
                if len(batch) >= batch_size:
                    flush(batch)
                    batch = []
            flush(batch)
            pruned = store.prune(kind, model_tag=tag, before=started)
            self.stdout.write(
                f"{kind}: {written} embedded ({tag}), {pruned} pruned, "
                f"{time.monotonic() - clock:.1f}s"
            )
