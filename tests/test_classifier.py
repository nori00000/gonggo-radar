"""Tests for alert.classifier -- keyword matching and domain classification."""

import pytest
from alert.classifier import DomainClassifier, DOMAIN_KOREAN_MAP, DOMAIN_KEYWORDS


class TestMatchKeyword:
    """Tests for _match_keyword boundary matching."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.dc = DomainClassifier()

    def test_korean_compound_word(self):
        """Korean substring: '이끼' in '이끼재배' → True."""
        assert self.dc._match_keyword("이끼", "이끼재배") is True

    def test_korean_with_space(self):
        """Korean with space: '이끼' in '이끼 재배 사업' → True."""
        assert self.dc._match_keyword("이끼", "이끼 재배 사업") is True

    def test_korean_longer_keyword(self):
        """Longer Korean: '스마트팜' in '스마트팜 운영' → True."""
        assert self.dc._match_keyword("스마트팜", "스마트팜 운영") is True

    def test_korean_embedded(self):
        """Korean embedded: '조경' in '조경설계' → True."""
        assert self.dc._match_keyword("조경", "조경설계") is True

    def test_ascii_with_space(self):
        """ASCII with space: 'AI' in 'AI 기반 스마트팜' → True."""
        assert self.dc._match_keyword("AI", "AI 기반 스마트팜") is True

    def test_ascii_to_korean_transition(self):
        """ASCII→Korean boundary: 'AI' in 'AI융합 사업' → True."""
        assert self.dc._match_keyword("AI", "AI융합 사업") is True

    def test_ascii_inside_word_no_match(self):
        """ASCII inside word: 'AI' in 'FAIR 프로그램' → False."""
        assert self.dc._match_keyword("AI", "FAIR 프로그램") is False

    def test_ascii_prefix_no_match(self):
        """ASCII prefix: 'AI' in 'AIGC플랫폼' → False (no boundary)."""
        assert self.dc._match_keyword("AI", "AIGC플랫폼") is False

    def test_ict_with_space(self):
        """ICT with space: 'ICT' in 'ICT 농업' → True."""
        assert self.dc._match_keyword("ICT", "ICT 농업") is True

    def test_ict_to_korean(self):
        """ICT→Korean: 'ICT' in 'ICT활용' → True."""
        assert self.dc._match_keyword("ICT", "ICT활용") is True

    def test_keyword_not_in_text(self):
        """Keyword not present at all → False."""
        assert self.dc._match_keyword("블록체인", "AI 융합 사업") is False


class TestClassifyText:
    """Tests for classify_text domain classification."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.dc = DomainClassifier()

    @pytest.mark.parametrize("text,expected_domain", [
        ("이끼 스마트팜 시설원예 지원사업", "moss_agriculture"),
        ("조경공사 도시녹화 사업 공고", "landscape"),
        ("치유농업 산림치유 프로그램", "healing"),
        ("소공인 제조혁신 지원", "manufacturing"),
        ("나라장터 사회적기업 우선구매", "public_procurement"),
        ("AI융합 디지털전환 사업", "ai_digital"),
    ])
    def test_classify_6_domains(self, text, expected_domain):
        """Each domain should be correctly classified from representative text."""
        domain, confidence = self.dc.classify_text(text)
        assert domain == expected_domain
        assert confidence > 0.0

    def test_classify_empty_text(self):
        """Empty text should return no domain."""
        domain, confidence = self.dc.classify_text("")
        assert domain == ""
        assert confidence == 0.0

    def test_classify_unrelated_text(self):
        """Completely unrelated text → no match or very low score."""
        domain, confidence = self.dc.classify_text("오늘 날씨가 좋습니다")
        assert domain == "" or confidence < 0.1

    def test_confidence_capped_at_1(self):
        """Confidence should never exceed 1.0."""
        # Text with many matching keywords
        text = " ".join(DOMAIN_KEYWORDS["moss_agriculture"]["primary"] +
                       DOMAIN_KEYWORDS["moss_agriculture"]["secondary"])
        domain, confidence = self.dc.classify_text(text)
        assert confidence <= 1.0


class TestClassifyMulti:
    """Tests for multi-label classification."""

    def test_multi_returns_list(self):
        dc = DomainClassifier()
        results = dc.classify_multi(type("Obj", (), {
            "title": "스마트팜 조경 AI 사업",
            "summary": "스마트팜과 조경을 결합한 AI 사업",
            "category": "복합",
            "target": "농업인",
        })())
        assert isinstance(results, list)
        # Should match multiple domains
        assert len(results) >= 2

    def test_multi_sorted_by_confidence(self):
        dc = DomainClassifier()
        results = dc.classify_multi(type("Obj", (), {
            "title": "이끼 스마트팜 시설원예",
            "summary": "농업 지원",
            "category": "농업",
            "target": "농가",
        })())
        if len(results) >= 2:
            assert results[0][1] >= results[1][1]
