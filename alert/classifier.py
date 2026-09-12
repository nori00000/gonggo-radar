"""사업 도메인 자동 분류 -- 키워드 기반 7개 도메인 매핑."""

import logging
import re

__all__ = ["DomainClassifier", "DOMAIN_KOREAN_MAP", "DOMAIN_KEYWORDS"]

logger = logging.getLogger(__name__)

DOMAIN_KOREAN_MAP = {
    "moss_agriculture": "이끼농업/스마트팜",
    "landscape": "조경/정원",
    "healing": "치유산업",
    "manufacturing": "제조/굿즈",
    "public_procurement": "공공조달",
    "ai_digital": "AI/디지털",
    "forest_social_economy": "산림형 사회적경제",
}

# Keywords per domain: primary (strong signal, weight 2) and secondary (weak signal, weight 1)
DOMAIN_KEYWORDS = {
    "moss_agriculture": {
        "primary": ["이끼", "스마트팜", "시설원예", "시설농업", "스마트농업", "식물공장", "스마트온실"],
        "secondary": ["농업", "영농", "귀농", "청년농", "6차산업", "ICT", "환경제어", "특용작물", "관상식물", "농업기술"],
    },
    "landscape": {
        "primary": ["조경", "도시녹화", "학교숲", "탄소숲", "조경공사", "조경설계"],
        "secondary": ["정원", "인공지반녹화", "옥상녹화", "벽면녹화", "도시재생", "조경관리", "녹지", "공원"],
    },
    "healing": {
        "primary": ["치유농업", "산림치유", "원예치료", "치유정원"],
        "secondary": ["치유", "노인복지", "장기요양", "바우처", "산림복지", "힐링", "웰니스"],
    },
    "manufacturing": {
        "primary": ["제조혁신", "소공인", "반려동물", "굿즈"],
        "secondary": ["디자인", "제조", "생산", "특허", "브랜드", "패키지"],
    },
    "public_procurement": {
        "primary": ["나라장터", "벤처나라", "사회적기업", "우선구매", "공공조달"],
        "secondary": ["사회적경제", "소셜벤처", "협동조합", "마을기업", "입찰", "조달"],
    },
    "ai_digital": {
        "primary": ["AI융합", "디지털전환", "인공지능"],
        "secondary": ["콘텐츠", "ICT", "데이터", "자동화", "플랫폼", "디지털"],
    },
    "forest_social_economy": {
        "primary": [
            "산림형 사회적경제", "산림형 예비사회적기업", "산림사업법인",
            "산림복지", "산촌", "임업",
        ],
        "secondary": [
            "사회적협동조합", "마을기업", "목재", "사회적기업 인증",
            "협동조합", "사회연대경제",
        ],
    },
}


class DomainClassifier:
    """사업 도메인 자동 분류기."""

    def __init__(self, config=None):
        """Initialize. config is ClassifierConfig (optional)."""
        self.method = config.method if config else "keyword"
        self.threshold = config.claude_classify_threshold if config else 0.6

    def classify(self, ann) -> tuple[str, float]:
        """Classify an announcement into a business domain.

        Args:
            ann: AnalyzedAnnouncement or RawAnnouncement with title, summary, category, target

        Returns:
            (domain_key, confidence) tuple. domain_key is one of the 6 domains.
            If no domain matches, returns ("", 0.0).
        """
        text = f"{ann.title} {ann.summary} {ann.category} {ann.target}"
        return self.classify_text(text)

    @staticmethod
    def _match_keyword(keyword: str, text: str) -> bool:
        """Match keyword in text.

        Korean keywords always use substring matching (compound words like
        '이끼재배' naturally embed morphemes).
        ASCII-only short keywords (<=3 chars, e.g. 'AI', 'ICT') require
        boundary matching to avoid false positives like 'FAIR'.
        ASCII-to-Korean transitions count as valid boundaries (e.g. 'AI융합').
        """
        is_ascii = all(ord(c) < 128 for c in keyword)
        if is_ascii and len(keyword) <= 3:
            # Boundary = start/end, whitespace, punctuation, or non-ASCII char
            pattern = rf'(?:^|[\s,./·\-]|[^\x00-\x7f]){re.escape(keyword)}(?:[\s,./·\-]|[^\x00-\x7f]|$)'
            return bool(re.search(pattern, text))
        return keyword in text

    def classify_text(self, text: str) -> tuple[str, float]:
        """Classify raw text into a business domain.

        Returns:
            (domain_key, confidence) tuple.
        """
        scores = {}
        text_lower = text.lower()

        for domain, keywords in DOMAIN_KEYWORDS.items():
            score = 0.0
            primary_total = len(keywords["primary"])
            secondary_total = len(keywords["secondary"])
            total_possible = primary_total * 2 + secondary_total * 1

            for kw in keywords["primary"]:
                if self._match_keyword(kw.lower(), text_lower):
                    score += 2.0

            for kw in keywords["secondary"]:
                if self._match_keyword(kw.lower(), text_lower):
                    score += 1.0

            if total_possible > 0:
                scores[domain] = score / total_possible

        if not scores:
            return ("", 0.0)

        best_domain = max(scores, key=scores.get)
        best_score = scores[best_domain]

        if best_score <= 0.0:
            return ("", 0.0)

        return (best_domain, min(best_score, 1.0))

    def classify_multi(self, ann) -> list[tuple[str, float]]:
        """Classify into potentially multiple domains (multi-label).

        Returns list of (domain, confidence) tuples sorted by confidence descending.
        Only includes domains with score > 0.
        """
        text = f"{ann.title} {ann.summary} {ann.category} {ann.target}"
        text_lower = text.lower()

        results = []
        for domain, keywords in DOMAIN_KEYWORDS.items():
            score = 0.0
            total_possible = len(keywords["primary"]) * 2 + len(keywords["secondary"]) * 1

            for kw in keywords["primary"]:
                if self._match_keyword(kw.lower(), text_lower):
                    score += 2.0
            for kw in keywords["secondary"]:
                if self._match_keyword(kw.lower(), text_lower):
                    score += 1.0

            if total_possible > 0 and score > 0:
                results.append((domain, min(score / total_possible, 1.0)))

        results.sort(key=lambda x: x[1], reverse=True)
        return results
