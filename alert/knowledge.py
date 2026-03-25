"""AI 지식 레이어 오케스트레이터 -- 분류·임베딩·옵시디언·이력 통합 관리."""

import logging
from typing import Optional

__all__ = ["KnowledgeLayer"]

from .classifier import DomainClassifier
from .models import AnalyzedAnnouncement, ClassifiedAnnouncement, ApplicationRecord

logger = logging.getLogger(__name__)


class KnowledgeLayer:
    """Knowledge layer orchestrator -- post-pipeline hook.

    Coordinates: domain classification, vector embedding, Obsidian sync,
    application history, and n8n webhook.
    """

    def __init__(self, db, config):
        """Initialize knowledge layer components.

        Args:
            db: Database instance
            config: AppConfig instance
        """
        self.db = db
        self.config = config.knowledge

        # Initialize sub-components (graceful degradation)
        self.classifier = None
        if self.config.classifier.enabled:
            self.classifier = DomainClassifier(self.config.classifier)

        self.vectordb = None
        if self.config.vector.enabled and db._vec_available:
            try:
                from .vectordb import VectorStore
                self.vectordb = VectorStore(db, self.config.vector)
            except Exception as e:
                logger.info(f"Vector store init failed: {e} -- Set OPENAI_API_KEY in alert/.env to enable vector embeddings")

        self.obsidian = None
        if self.config.obsidian.enabled and self.config.obsidian.vault_path:
            try:
                from .obsidian import ObsidianSync
                self.obsidian = ObsidianSync(self.config.obsidian)
            except Exception as e:
                logger.info(f"Obsidian sync init failed: {e} -- Set OBSIDIAN_VAULT_PATH in alert/.env to enable Obsidian sync")

        self.n8n = None
        if self.config.n8n.enabled and self.config.n8n.webhook_url:
            try:
                from .n8n_hook import N8nHook
                self.n8n = N8nHook(self.config.n8n)
            except Exception as e:
                logger.warning(f"n8n hook init failed: {e}")

    def process_announcement(self, ann: AnalyzedAnnouncement) -> None:
        """Full knowledge pipeline for a single announcement.

        REQUIRES: ann.id must be set (from insert_announcement return value).
        """
        if ann.id is None:
            logger.error("ann.id is None -- cannot process without DB row ID")
            return

        domain = ""
        confidence = 0.0
        obsidian_path = ""

        # 1. Classify domain
        if self.classifier:
            try:
                domain, confidence = self.classifier.classify(ann)
                if domain:
                    self.db.update_announcement_domain(ann.id, domain, confidence)
                    logger.debug(f"Classified #{ann.id} as {domain} ({confidence:.0%})")
            except Exception as e:
                logger.error(f"Classification failed for #{ann.id}: {e}")

        # 2. Generate + store embedding
        if self.vectordb:
            try:
                text = f"{ann.title} {ann.summary} {ann.category} {ann.target}"
                self.vectordb.store_embedding(ann.id, text)
            except Exception as e:
                logger.error(f"Embedding failed for #{ann.id}: {e}")

        # 3. Find similar past announcements
        similar = []
        if self.vectordb:
            try:
                similar = self.vectordb.find_similar(ann.id, top_k=self.config.vector.top_k)
            except Exception as e:
                logger.debug(f"Similar search failed for #{ann.id}: {e}")

        # 4. Create application history record (if relevant enough)
        if (self.config.history.enabled and
            self.config.history.auto_create_on_relevant and
            ann.relevance_score >= self.config.obsidian.min_relevance_for_sync):
            try:
                record = ApplicationRecord(
                    announcement_id=ann.id,
                    status="discovered",
                    assigned_domain=domain,
                    priority=self.config.history.default_priority,
                )
                self.db.insert_application_record(record)
                logger.debug(f"Created application record for #{ann.id}")
            except Exception as e:
                logger.error(f"Application record creation failed for #{ann.id}: {e}")

        # 5. Sync to Obsidian (if meets threshold)
        if (self.obsidian and self.obsidian.is_available and
            ann.relevance_score >= self.config.obsidian.min_relevance_for_sync):
            try:
                # Build ClassifiedAnnouncement from AnalyzedAnnouncement
                classified = ClassifiedAnnouncement(
                    source=ann.source,
                    source_id=ann.source_id,
                    title=ann.title,
                    url=ann.url,
                    summary=ann.summary,
                    author=ann.author,
                    category=ann.category,
                    target=ann.target,
                    period_start=ann.period_start,
                    period_end=ann.period_end,
                    raw_data=ann.raw_data,
                    fetched_at=ann.fetched_at,
                    relevance_score=ann.relevance_score,
                    relevance_reason=ann.relevance_reason,
                    matched_keywords=ann.matched_keywords,
                    id=ann.id,
                    business_domain=domain,
                    domain_confidence=confidence,
                    similar_announcements=similar,
                )
                obsidian_path = self.obsidian.generate_note(classified)
                if obsidian_path:
                    self.db.update_announcement_obsidian_path(ann.id, obsidian_path)
                    logger.debug(f"Obsidian note created for #{ann.id}: {obsidian_path}")
            except Exception as e:
                logger.error(f"Obsidian sync failed for #{ann.id}: {e}")

        # 6. Send n8n webhook (only for relevant announcements)
        if self.n8n and ann.relevance_score >= self.config.obsidian.min_relevance_for_sync:
            try:
                self.n8n.send_event("new_relevant_announcement", ann, obsidian_path)
            except Exception as e:
                logger.debug(f"n8n webhook failed for #{ann.id}: {e}")

    def process_batch(self, announcements: list) -> int:
        """Process multiple announcements. Each must have ann.id set.

        Returns:
            Number of successfully processed announcements
        """
        processed = 0
        for ann in announcements:
            try:
                self.process_announcement(ann)
                processed += 1
            except Exception as e:
                logger.error(f"Knowledge processing failed for {ann.title}: {e}", exc_info=True)

        logger.info(f"Knowledge layer processed {processed}/{len(announcements)} announcements")
        return processed

    def generate_research(self, period_start: str, period_end: str) -> Optional[int]:
        """Generate research document for a period.

        Returns:
            Research document ID, or None if generation failed
        """
        try:
            from .research_generator import ResearchGenerator
            generator = ResearchGenerator(self.db, self.config, self.obsidian)
            return generator.generate(period_start, period_end)
        except Exception as e:
            logger.error(f"Research generation failed: {e}", exc_info=True)
            return None

    def close(self) -> None:
        """Cleanup resources."""
        if self.n8n:
            self.n8n.close()
