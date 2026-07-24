"""Generate embeddings for Article rows that do not have one yet.

Runtime configuration lives in ``embedding-tool/.env``. The repository-level
``.env.vercel`` is loaded first so the tool can reference VERCEL_DATABASE_URL
without duplicating database credentials.
"""

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import RealDictCursor


BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parent
ENV_PATH = BASE_DIR / ".env"
VERCEL_ENV_PATH = REPO_ROOT / ".env.vercel"
TOOLS_DIR = REPO_ROOT / "tools"

load_dotenv(VERCEL_ENV_PATH, override=False)
load_dotenv(ENV_PATH, override=False)

if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from pipeline_lock import acquire  # noqa: E402


@dataclass(frozen=True)
class EmbeddingSettings:
    database_url: str
    provider: str
    model_name: str
    dimension: int
    device: Optional[str]
    normalize: bool
    connect_timeout: int
    ollama_host: str
    commit_every: int = 100
    batch_size: int = 16


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(
            f"Missing required environment variable {name}. "
            f"Configure it in {ENV_PATH}."
        )
    return value


def _positive_int_env(name: str, default: int) -> int:
    raw_value = os.getenv(name, str(default)).strip()
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw_value!r}.") from exc
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero.")
    return value


def _nonnegative_int_env(name: str, default: int) -> int:
    raw_value = os.getenv(name, str(default)).strip()
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw_value!r}.") from exc
    if value < 0:
        raise ValueError(f"{name} cannot be negative.")
    return value


def _bool_env(name: str, default: bool) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None or not raw_value.strip():
        return default
    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false, got {raw_value!r}.")


def load_settings(
    dimension_override: Optional[int] = None,
) -> EmbeddingSettings:
    """Load and validate embedding configuration from environment variables."""
    database_url = _required_env("SUPABASE_DB_URL")
    dimension = (
        dimension_override
        if dimension_override is not None
        else _positive_int_env("EMBEDDING_DIMENSION", 768)
    )
    if dimension <= 0:
        raise ValueError("Embedding dimension must be greater than zero.")

    provider = os.getenv(
        f"EMBEDDING_PROVIDER_{dimension}",
        os.getenv("EMBEDDING_PROVIDER", "local"),
    ).strip().lower()
    if provider not in {"local", "ollama"}:
        raise ValueError(
            f"Unsupported embedding provider {provider!r}; use local or ollama."
        )

    model_name = os.getenv(
        f"EMBEDDING_MODEL_NAME_{dimension}",
        os.getenv("EMBEDDING_MODEL_NAME", ""),
    ).strip()
    if not model_name:
        raise ValueError(
            f"Missing EMBEDDING_MODEL_NAME_{dimension} or EMBEDDING_MODEL_NAME."
        )

    device = os.getenv("EMBEDDING_DEVICE", "").strip() or None
    ollama_host = os.getenv(
        "EMBEDDING_OLLAMA_HOST",
        "http://localhost:11434",
    ).strip().rstrip("/")
    if provider == "ollama" and not ollama_host:
        raise ValueError("EMBEDDING_OLLAMA_HOST is required for Ollama.")

    return EmbeddingSettings(
        database_url=database_url,
        provider=provider,
        model_name=model_name,
        dimension=dimension,
        device=device,
        normalize=_bool_env("EMBEDDING_NORMALIZE", False),
        connect_timeout=_positive_int_env("EMBEDDING_DB_CONNECT_TIMEOUT", 20),
        ollama_host=ollama_host,
        commit_every=_positive_int_env("EMBEDDING_COMMIT_EVERY", 100),
        batch_size=_positive_int_env("EMBEDDING_BATCH_SIZE", 16),
    )


def _validate_vector(vector: List[float], expected_dimension: int) -> List[float]:
    if len(vector) != expected_dimension:
        raise ValueError(
            f"Model returned {len(vector)} dimensions; "
            f"expected {expected_dimension}."
        )
    return vector


def _create_batch_embedder(
    settings: EmbeddingSettings,
) -> Callable[[List[str]], List[List[float]]]:
    """Initialize the provider and return a batch embedder."""
    if settings.provider == "ollama":
        try:
            import ollama
        except ImportError as exc:
            raise RuntimeError(
                "Ollama provider requires: "
                "pip install -r requirements-ollama.txt"
            ) from exc

        client = ollama.Client(host=settings.ollama_host)

        def embed_with_ollama(texts: List[str]) -> List[List[float]]:
            response = client.embed(
                model=settings.model_name,
                input=texts,
                dimensions=settings.dimension,
                truncate=True,
            )
            embeddings = (
                response.embeddings
                if hasattr(response, "embeddings")
                else response["embeddings"]
            )
            if len(embeddings) != len(texts):
                raise ValueError(
                    f"Model returned {len(embeddings)} vectors for "
                    f"{len(texts)} inputs."
                )
            return [
                _validate_vector(
                    [float(value) for value in embedding],
                    settings.dimension,
                )
                for embedding in embeddings
            ]

        return embed_with_ollama

    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise RuntimeError(
            "Local provider requires: "
            "pip install -r requirements-local.txt"
        ) from exc

    model_kwargs = {}
    if settings.device:
        model_kwargs["device"] = settings.device
    model = SentenceTransformer(settings.model_name, **model_kwargs)

    def embed_locally(texts: List[str]) -> List[List[float]]:
        encoded = model.encode(
            texts,
            normalize_embeddings=settings.normalize,
            convert_to_numpy=True,
        )
        values = encoded.tolist() if hasattr(encoded, "tolist") else list(encoded)
        return [
            _validate_vector(
                [float(value) for value in vector],
                settings.dimension,
            )
            for vector in values
        ]

    return embed_locally


def _create_embedder(
    settings: EmbeddingSettings,
) -> Callable[[str], List[float]]:
    """Backward-compatible single-text wrapper used by callers and tests."""
    embed_batch = _create_batch_embedder(settings)

    def embed_one(text: str) -> List[float]:
        return embed_batch([text])[0]

    return embed_one


def _replace_incompatible_embeddings(
    cursor,
    requested_dimension: int,
    confirmation: Optional[str],
) -> int:
    expected_confirmation = f"REPLACE-EMBEDDINGS-{requested_dimension}"
    if confirmation != expected_confirmation:
        raise ValueError(
            "--confirm-replace must equal "
            f"{expected_confirmation!r} when --replace-incompatible is used."
        )
    cursor.execute(
        """
        UPDATE "Article"
        SET "embedding" = NULL
        WHERE "embedding" IS NOT NULL
          AND vector_dims("embedding") <> %s
        """,
        (requested_dimension,),
    )
    return cursor.rowcount


def _validate_existing_dimensions(cursor, requested_dimension: int) -> None:
    cursor.execute(
        """
        SELECT DISTINCT vector_dims("embedding") AS dimension
        FROM "Article"
        WHERE "embedding" IS NOT NULL
        """
    )
    dimensions = sorted(
        {
            int(row["dimension"])
            for row in cursor.fetchall()
            if row["dimension"] is not None
        }
    )
    incompatible = [
        dimension
        for dimension in dimensions
        if dimension != requested_dimension
    ]
    if incompatible:
        raise ValueError(
            "Existing Article embeddings use dimensions "
            f"{incompatible}; refusing to mix them with "
            f"{requested_dimension}-dimension vectors."
        )


def _article_text(article) -> str:
    title = (article.get("title") or "").strip()
    abstract = (article.get("abstract") or "").strip()
    return f"{title}. {abstract}".strip(". ")


def _print_settings(
    settings: EmbeddingSettings,
    limit: int,
    dry_run: bool,
) -> None:
    print(f"Provider: {settings.provider}")
    print(f"Model: {settings.model_name}")
    print(f"Dimensions: {settings.dimension}")
    if settings.provider == "ollama":
        print(f"Ollama host: {settings.ollama_host}")
    print(f"Database URL: configured (hidden)")
    print(f"Limit: {'all pending rows' if limit == 0 else limit}")
    print(f"Dry-run: {'yes' if dry_run else 'no'}")


def embed_database(
    dimension: Optional[int] = None,
    limit: Optional[int] = None,
    dry_run: bool = False,
    replace_incompatible: bool = False,
    replace_confirmation: Optional[str] = None,
) -> int:
    settings = load_settings(dimension_override=dimension)
    effective_limit = (
        _nonnegative_int_env("EMBEDDING_LIMIT", 0)
        if limit is None
        else limit
    )
    if effective_limit < 0:
        raise ValueError("Embedding limit cannot be negative.")

    _print_settings(settings, effective_limit, dry_run)
    if dry_run:
        print("[DRY-RUN] Configuration is valid; model and database were not used.")
        return 0

    print(
        f"[0] Initializing {settings.provider} provider, "
        f"model {settings.model_name} ({settings.dimension} dimensions)..."
    )
    embed_texts = _create_batch_embedder(settings)
    connection = psycopg2.connect(
        settings.database_url,
        connect_timeout=settings.connect_timeout,
        application_name="embed_database",
    )

    processed = 0
    skipped = 0
    try:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            if replace_incompatible:
                replaced = _replace_incompatible_embeddings(
                    cursor,
                    settings.dimension,
                    replace_confirmation,
                )
                connection.commit()
                print(
                    f"[reset] Cleared {replaced:,} incompatible embeddings.",
                    flush=True,
                )
            _validate_existing_dimensions(cursor, settings.dimension)

            query = """
                SELECT "article_id", "title", "abstract"
                FROM "Article"
                WHERE "embedding" IS NULL
                  AND COALESCE("is_deleted", FALSE) = FALSE
                ORDER BY "article_id"
            """
            parameters = ()
            if effective_limit:
                query += " LIMIT %s"
                parameters = (effective_limit,)
            cursor.execute(query, parameters)
            articles = cursor.fetchall()
            total = len(articles)
            print(f"[1] Found {total:,} Article rows without embeddings.")

            pending_articles = []
            for article in articles:
                text = _article_text(article)
                if not text:
                    skipped += 1
                    print(
                        f"[SKIP] Article {article['article_id']} has no text."
                    )
                    continue
                pending_articles.append((article, text))

            since_commit = 0
            for offset in range(0, len(pending_articles), settings.batch_size):
                batch = pending_articles[offset : offset + settings.batch_size]
                vectors = embed_texts([text for _article, text in batch])
                cursor.executemany(
                    """
                    UPDATE "Article"
                    SET "embedding" = %s
                    WHERE "article_id" = %s
                    """,
                    [
                        (vector, article["article_id"])
                        for (article, _text), vector in zip(batch, vectors)
                    ],
                )
                processed += len(batch)
                since_commit += len(batch)

                if since_commit >= settings.commit_every:
                    connection.commit()
                    since_commit = 0
                current = min(offset + len(batch) + skipped, total)
                if current == total or processed % 100 < settings.batch_size:
                    print(
                        f"[2] Progress {current:,}/{total:,}; "
                        f"updated {processed:,}, skipped {skipped:,}.",
                        flush=True,
                    )

        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    print(
        f"[3] Completed: updated {processed:,}, skipped {skipped:,}."
    )
    return processed


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Embed pending Article rows using env-driven configuration."
    )
    parser.add_argument(
        "--dimension",
        type=int,
        default=None,
        help="Override EMBEDDING_DIMENSION and select dimension-specific config.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum rows to process; 0 means all pending rows.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate configuration without loading a model or connecting to DB.",
    )
    parser.add_argument(
        "--replace-incompatible",
        action="store_true",
        help="Clear existing embeddings whose dimensions differ from --dimension.",
    )
    parser.add_argument(
        "--confirm-replace",
        default=None,
        help="Required confirmation: REPLACE-EMBEDDINGS-<dimension>.",
    )
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    try:
        if not args.dry_run:
            acquire("embed_database")
        embed_database(
            dimension=args.dimension,
            limit=args.limit,
            dry_run=args.dry_run,
            replace_incompatible=args.replace_incompatible,
            replace_confirmation=args.confirm_replace,
        )
    except Exception as exc:
        print(f"[ERROR] Embedding failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
