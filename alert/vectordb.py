"""벡터 임베딩 저장 및 유사도 검색 -- sqlite-vec + OpenAI."""

import json
import logging
import os
import struct
from datetime import datetime
from typing import Optional

__all__ = ["VectorStore"]

_BACKEND = "sqlite"
_DATABASE_URL = os.getenv("DATABASE_URL", "")
if _DATABASE_URL.startswith(("postgresql://", "postgres://")):
    _BACKEND = "postgresql"

logger = logging.getLogger(__name__)


def _floats_to_bytes(floats: list[float]) -> bytes:
    """Convert list of floats to little-endian float32 bytes for sqlite-vec."""
    return struct.pack(f"<{len(floats)}f", *floats)


def _bytes_to_floats(data: bytes) -> list[float]:
    """Convert little-endian float32 bytes back to list of floats."""
    n = len(data) // 4
    return list(struct.unpack(f"<{n}f", data))


class VectorStore:
    """sqlite-vec 기반 벡터 저장소 및 유사도 검색."""

    def __init__(self, db, config=None):
        """Initialize using db's existing connection (sqlite-vec already loaded).

        Args:
            db: Database instance (must have _vec_available=True and _conn)
            config: VectorConfig (optional)
        """
        self.db = db
        self._config = config
        self._backend = getattr(db, '_backend', 'sqlite')

        # OpenAI client (lazy init)
        self._client = None
        self._model = config.embedding_model if config else "text-embedding-3-small"
        self._dimensions = config.dimensions if config else 256
        self._top_k = config.top_k if config else 5

        if self._backend == "sqlite" and not db._vec_available:
            logger.warning("sqlite-vec not available, vector operations will fail")

    def _get_client(self):
        """Lazy-initialize OpenAI client."""
        if self._client is None:
            try:
                from openai import OpenAI
                # API key from config or environment
                api_key = getattr(self._config, 'openai_api_key', None) if self._config else None
                if api_key:
                    self._client = OpenAI(api_key=api_key)
                else:
                    self._client = OpenAI()  # Uses OPENAI_API_KEY env var
            except Exception as e:
                logger.error(f"Failed to initialize OpenAI client: {e}")
                raise
        return self._client

    def embed_text(self, text: str) -> list[float]:
        """Generate embedding via OpenAI API.

        Args:
            text: Text to embed

        Returns:
            List of floats (embedding vector)
        """
        client = self._get_client()
        response = client.embeddings.create(
            input=text,
            model=self._model,
            dimensions=self._dimensions,
        )
        return response.data[0].embedding

    def store_embedding(self, announcement_id: int, text: str) -> Optional[int]:
        """Embed text and store vector for an announcement.

        Args:
            announcement_id: DB row ID of the announcement
            text: Text to embed (typically title + summary)

        Returns:
            Embedding row ID, or None if embedding fails
        """
        try:
            vector = self.embed_text(text)

            if self._backend == "postgresql":
                # Store in embeddings table
                now = datetime.now().isoformat()
                cur = self.db.conn.cursor()
                cur.execute(
                    """INSERT INTO embeddings (announcement_id, model_name, embedding, dimensions, created_at)
                       VALUES (%s, %s, %s, %s, %s) RETURNING id""",
                    (announcement_id, self._model, _floats_to_bytes(vector), self._dimensions, now),
                )
                emb_id = cur.fetchone()["id"]
                # Update announcement reference
                cur.execute(
                    "UPDATE announcements SET embedding_id = %s, updated_at = %s WHERE id = %s",
                    (emb_id, now, announcement_id),
                )
                # Store in pgvector table
                try:
                    # Convert float list to PostgreSQL vector literal
                    vec_literal = "[" + ",".join(str(f) for f in vector) + "]"
                    cur.execute(
                        "INSERT INTO vec_announcements (id, embedding) VALUES (%s, %s::vector)",
                        (emb_id, vec_literal),
                    )
                except Exception as e:
                    logger.warning(f"vec_announcements insert failed (non-fatal): {e}")

                logger.debug(f"Stored embedding {emb_id} for announcement {announcement_id}")
                return emb_id
            else:
                # SQLite path - unchanged
                vector_bytes = _floats_to_bytes(vector)
                emb_id = self.db.insert_embedding(
                    announcement_id, vector_bytes, self._model, self._dimensions
                )
                if self.db._vec_available:
                    try:
                        self.db.conn.execute(
                            "INSERT INTO vec_announcements(rowid, embedding) VALUES (?, ?)",
                            (emb_id, vector_bytes),
                        )
                        self.db.conn.commit()
                    except Exception as e:
                        logger.warning(f"vec_announcements insert failed (non-fatal): {e}")

                logger.debug(f"Stored embedding {emb_id} for announcement {announcement_id}")
                return emb_id

        except Exception as e:
            logger.error(f"Failed to store embedding for announcement {announcement_id}: {e}")
            return None

    def find_similar(self, announcement_id: int, top_k: int = 0) -> list[tuple[int, float]]:
        """Find top-K similar announcements by cosine similarity.

        Args:
            announcement_id: DB row ID of the source announcement
            top_k: Number of results (0 = use config default)

        Returns:
            List of (announcement_id, distance) tuples, sorted by similarity
        """
        if top_k <= 0:
            top_k = self._top_k

        if self._backend == "postgresql":
            # Get embedding for this announcement
            cur = self.db.conn.cursor()
            cur.execute(
                "SELECT id, embedding FROM embeddings WHERE announcement_id = %s LIMIT 1",
                (announcement_id,),
            )
            row = cur.fetchone()
            if not row:
                return []

            emb_id = row["id"]
            # Get the vector from vec_announcements
            cur.execute("SELECT embedding FROM vec_announcements WHERE id = %s", (emb_id,))
            vec_row = cur.fetchone()
            if not vec_row:
                return []

            # Query pgvector for similar (excluding self)
            cur.execute(
                """SELECT id, embedding <=> %s::vector AS distance
                     FROM vec_announcements
                    WHERE id != %s
                    ORDER BY embedding <=> %s::vector
                    LIMIT %s""",
                (str(vec_row["embedding"]), emb_id, str(vec_row["embedding"]), top_k),
            )
            results = cur.fetchall()

            similar = []
            for r in results:
                cur.execute("SELECT announcement_id FROM embeddings WHERE id = %s", (r["id"],))
                ann_row = cur.fetchone()
                if ann_row:
                    similar.append((ann_row["announcement_id"], r["distance"]))
            return similar
        else:
            # SQLite path - unchanged
            row = self.db.conn.execute(
                "SELECT id, embedding FROM embeddings WHERE announcement_id = ? LIMIT 1",
                (announcement_id,),
            ).fetchone()
            if not row:
                return []

            emb_id = row["id"]
            vector_bytes = row["embedding"]

            results = self.db.conn.execute(
                """SELECT rowid, distance
                  FROM vec_announcements
                 WHERE embedding MATCH ?
                   AND k = ?""",
                (vector_bytes, top_k + 1),
            ).fetchall()

            similar = []
            for r in results:
                if r["rowid"] == emb_id:
                    continue
                ann_row = self.db.conn.execute(
                    "SELECT announcement_id FROM embeddings WHERE id = ?",
                    (r["rowid"],),
                ).fetchone()
                if ann_row:
                    similar.append((ann_row["announcement_id"], r["distance"]))
            return similar[:top_k]

    def find_similar_by_text(self, query: str, top_k: int = 0) -> list[tuple[int, float]]:
        """Ad-hoc text similarity search.

        Args:
            query: Text to search for
            top_k: Number of results

        Returns:
            List of (announcement_id, distance) tuples
        """
        if top_k <= 0:
            top_k = self._top_k

        try:
            vector = self.embed_text(query)

            if self._backend == "postgresql":
                vec_literal = "[" + ",".join(str(f) for f in vector) + "]"
                cur = self.db.conn.cursor()
                cur.execute(
                    """SELECT id, embedding <=> %s::vector AS distance
                         FROM vec_announcements
                        ORDER BY embedding <=> %s::vector
                        LIMIT %s""",
                    (vec_literal, vec_literal, top_k),
                )
                results = cur.fetchall()

                similar = []
                for r in results:
                    cur.execute("SELECT announcement_id FROM embeddings WHERE id = %s", (r["id"],))
                    ann_row = cur.fetchone()
                    if ann_row:
                        similar.append((ann_row["announcement_id"], r["distance"]))
                return similar
            else:
                # SQLite path - unchanged
                vector_bytes = _floats_to_bytes(vector)
                results = self.db.conn.execute(
                    """SELECT rowid, distance
                      FROM vec_announcements
                     WHERE embedding MATCH ?
                       AND k = ?""",
                    (vector_bytes, top_k),
                ).fetchall()

                similar = []
                for r in results:
                    ann_row = self.db.conn.execute(
                        "SELECT announcement_id FROM embeddings WHERE id = ?",
                        (r["rowid"],),
                    ).fetchone()
                    if ann_row:
                        similar.append((ann_row["announcement_id"], r["distance"]))
                return similar

        except Exception as e:
            logger.error(f"Text similarity search failed: {e}")
            return []

    def batch_embed(self, announcements: list) -> int:
        """Embed multiple announcements. Uses individual API calls to stay within limits.

        Args:
            announcements: List of AnalyzedAnnouncement with id set

        Returns:
            Number of successfully embedded announcements
        """
        count = 0
        for ann in announcements:
            if ann.id is None:
                continue
            text = f"{ann.title} {ann.summary} {ann.category} {ann.target}"
            result = self.store_embedding(ann.id, text)
            if result is not None:
                count += 1
        return count
