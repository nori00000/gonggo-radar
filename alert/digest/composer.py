"""협의회 주간 정책브리핑 다이제스트 생성기 (계약 v2.1).

v2.1의 뼈대 (2026-09-13 확정안, 판정 1~9):
- 섹션은 소스가 아니라 **독자의 행동**으로 나눈다: 신청하세요 / 알아두세요 / 협의회에서 / 회원사 소식.
- 협의회 소스 풀(COUNCIL_SOURCES) 밖의 소스는 **제목에 사회적경제 정체성 키워드가 있을 때만** 들어온다.
- 분류는 순수 함수다: classify_item(title, summary, source) -> Classification(판정·사유·근거·대상 태그).
- 기관은 게시자(author)가 아니라 **소스 표시명**이다 (author 필드는 표시하지 않는다).
- 마감·자격·금액은 DB(summary/raw_data)에서 **문자 그대로 인용**할 수 있을 때만 값을 갖는다.
  인용이 없으면 "원문 확인" — 어떤 레인도 이 필드를 창작하지 않는다.
- 보류 항목은 발송본에 싣지 않는다. 마크다운 맨 아래 HTML 주석으로만 남는다.
"""

import csv
import json
import re
import sqlite3
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Sequence, Set, Tuple

# 이 파일을 만든 레인 (계약 v1.1: 생성 파일 머리에 항상 표기)
LANE = "Claude opus executor"

# 협의회 의견/"이번 주 한 줄" 확정 마커. 발송 게이트(send_digest)가 이 문자열로 막는다.
MARKER = "<!-- 상민 확정 필요 -->"

# ─── 판정값 ──────────────────────────────────────────────────────────────
VERDICT_APPLY = "신청하세요"
VERDICT_NOTICE = "알아두세요"
VERDICT_HOLD = "보류"
VERDICT_EXCLUDE = "배제"

SECTION_COUNCIL = "협의회에서"
SECTION_MEMBER = "회원사 소식"

# 발송본 형식 v2.1의 섹션 머리글 (마크다운 `## ` 뒤에 오는 문자열 = 정본)
SECTION_HEADINGS = {
    VERDICT_APPLY: "✅ 신청하세요 (마감순)",
    VERDICT_NOTICE: "👀 알아두세요",
    SECTION_COUNCIL: "🤝 협의회에서",
    SECTION_MEMBER: "🏢 회원사 소식",
}
HEADING_TO_SECTION = {heading: name for name, heading in SECTION_HEADINGS.items()}

# 항목이 실리는 섹션과 그 상한 (판정 ⑩)
ITEM_SECTIONS = (VERDICT_APPLY, VERDICT_NOTICE)
SECTION_LIMITS = {VERDICT_APPLY: 5, VERDICT_NOTICE: 3}

# ─── 협의회 소스 풀 (판정 ②) ─────────────────────────────────────────────
# 회사용 소스(smartfarm·ipet·nongup_gg·rda·goyang·mafra 등 농업·지자체 계열)는
# 협의회 브리핑에서 제외한다. 예외는 아래 SSE_IDENTITY_KEYWORDS가 **제목에** 있을 때뿐.
COUNCIL_SOURCES = (
    "kofpi",
    "forest_service",
    "forest_press",
    "fowi",
    "lawmaking",
    "coop",
    "socialenterprise",
    "seis",
    "mois_sse",
    "bizinfo",
)

# 기관 = 소스 표시명 고정 매핑 (판정 ⑤). author(게시자 이름) 폐기.
SOURCE_DISPLAY_NAMES = {
    "kofpi": "한국임업진흥원",
    "forest_service": "산림청",
    "forest_press": "산림청",
    "fowi": "한국산림복지진흥원",
    "lawmaking": "국민참여입법센터(산림청 소관)",
    "coop": "협동조합포털(기재부)",
    "socialenterprise": "사회적기업진흥원",
    "seis": "사회적기업진흥원 통합정보",
    "mois_sse": "행정안전부 마을기업",
    "bizinfo": "기업마당",
}

# ─── 룰 v2.1 키워드 사전 (판정 ③) ────────────────────────────────────────
# 기회 키워드 = 신청하세요 후보
OPPORTUNITY_KEYWORDS = (
    "공고",
    "공모",
    "모집",
    "신청",
    "지원사업",
    "컨설팅",
    "입주",
    "입점",
    "조달",
    "설명회",
)

# 제도 키워드 = 알아두세요 후보
INSTITUTION_KEYWORDS = (
    "입법예고",
    "행정예고",
    "개정",
    "시행",
    "고시",
    "제도",
    "정책",
    "기본계획",
    "계획 발표",
)

# 산림 업종 키워드.
# 판정 ③ 단서: "치유"는 `산림치유`로만 인정(치유농업 배제), "조경"은 회사 축이므로 제외.
FOREST_INDUSTRY_KEYWORDS = (
    "산림",
    "임업",
    "임산물",
    "산촌",
    "목재",
    "숲",
    "산지",
    "임도",
    "산양삼",
    "산림복지",
    "산림치유",
)

# 사회적경제 정체성 키워드 (소스 풀 예외 + 관련성 축)
SSE_IDENTITY_KEYWORDS = (
    "사회적기업",
    "예비사회적기업",
    "사회적협동조합",
    "협동조합",
    "마을기업",
    "사회적경제",
    "사회연대경제",
    "소셜벤처",
    "자활기업",
)

# 노이즈 사전 (판정 ③).
# 계약에 적힌 항목 + 같은 부류의 한국어 표면형만 덧붙였다(추석=명절, 합격=합격자,
# 공모결과/선정 결과=행사 후기·수상 부류). 새 부류는 추가하지 않았다.
NOISE_KEYWORDS = (
    "채용",
    "합격",
    "인사발령",
    "시스템",
    "서버",
    "점검",
    "진로",
    "체험 부스",
    "행사 후기",
    "수상",
    "선물",
    "기획전",
    "카달로그",
    "카탈로그",
    "명절",
    "추석",
    "청탁금지",
    "공모결과",
    "선정 결과",
    "재난 대비 점검",
)

# ─── 사업자 모집 vs 참가자 모집 (개정 v2.2 F1) ───────────────────────────
# 제목에 B2C 신호가 있고 사업자 신호가 없으면 **보류**다(배제 아님 — `핀 n` 복구 가능).
# W37 실측: "숲동행 건강출산 지원사업 수시모집", "나눔의 숲 캠프 모집 공고"가
# 신청하세요 5칸 중 2칸을 먹었다. 둘 다 회원사가 신청할 사업이 아니라 개인·가족 모집이다.
# 개정 v2.3 G1: `참여자`는 중립어라 목록에서 뺐다 — 임업 사업자 공고가 "참여자 모집"으로
# 쓰이는 사례가 실재한다(배출권거래제 외부사업, 시제품 개발 지원).
B2C_SIGNAL_KEYWORDS = (
    "참가자",
    "참가",
    "체험단",
    "캠프",
    "교실",
    "프로그램 참여",
    "건강",
    "출산",
    "가족",
    "어린이",
    "청소년",
)
B2B_SIGNAL_KEYWORDS = (
    "기업",
    "사업자",
    "단체",
    "법인",
    "협동조합",
    "사회적기업",
    "마을기업",
    "업체",
    "공급자",
    "입점",
    "조달",
    "위탁",
    # 개정 v2.3 G1: 사업자만 신청하는 사업의 표지
    "시제품",
    "배출권",
    "외부사업",
    "개발 지원",
    "인증 지원",
    "등록",
)

# 신청 섹션의 소스 다양성 상한 (개정 v2.2 F2). 초과분은 보류로 내려간다.
SOURCE_DIVERSITY_LIMIT = 2

# 보류 사유 (개정 v2.3 G2: 다양성 때문에 밀린 것과 상한 때문에 밀린 것을 구분한다)
HOLD_REASON_DIVERSITY = f"소스 다양성 상한(같은 소스 {SOURCE_DIVERSITY_LIMIT}건)"
HOLD_REASON_SECTION_CAP = "상한 초과"

# ─── 대상 태그 (판정 ①) ──────────────────────────────────────────────────
TAG_SOCIAL_COOP = "사협"          # 사회적협동조합
TAG_COOP = "협동조합"
TAG_SOCIAL_ENTERPRISE = "사회적기업"
TAG_VILLAGE = "마을기업"
TAG_FOREST_BIZ = "산림사업자"
TAG_ALL = "전체"

# 표시 순서 (좁은 태그부터)
TAG_ORDER = (
    TAG_SOCIAL_COOP,
    TAG_SOCIAL_ENTERPRISE,
    TAG_COOP,
    TAG_VILLAGE,
    TAG_FOREST_BIZ,
    TAG_ALL,
)

# 권역·광역 표기 (개정 v2.4 (b)). 센터명이 여러 시·도를 나열하면 권역명이 정답이다
# (예: `[세종대전충청센터]` → 충청).
REGION_BROAD_PATTERNS = (
    ("수도권", "수도권"),
    ("충청", "충청"),
    ("영남", "영남"),
    ("호남", "호남"),
)

# 지역 한정 시 붙일 도명. 긴 표기를 먼저 본다.
REGION_PATTERNS = (
    ("서울특별시", "서울"),
    ("부산광역시", "부산"),
    ("대구광역시", "대구"),
    ("인천광역시", "인천"),
    ("광주광역시", "광주"),
    ("대전광역시", "대전"),
    ("울산광역시", "울산"),
    ("세종특별자치시", "세종"),
    ("경기도", "경기"),
    ("강원특별자치도", "강원"),
    ("충청북도", "충북"),
    ("충청남도", "충남"),
    ("전라북도", "전북"),
    ("전북특별자치도", "전북"),
    ("전라남도", "전남"),
    ("경상북도", "경북"),
    ("경상남도", "경남"),
    ("제주특별자치도", "제주"),
    ("서울", "서울"),
    ("부산", "부산"),
    ("대구", "대구"),
    ("인천", "인천"),
    ("광주", "광주"),
    ("대전", "대전"),
    ("울산", "울산"),
    ("세종", "세종"),
    ("경기", "경기"),
    ("강원", "강원"),
    ("충북", "충북"),
    ("충남", "충남"),
    ("전북", "전북"),
    ("전남", "전남"),
    ("경북", "경북"),
    ("경남", "경남"),
    ("제주", "제주"),
)

# ─── 마감 라벨 (판정 ④) ──────────────────────────────────────────────────
LABEL_NEW = "새 소식"
LABEL_STANDING = "상시"
NEW_WINDOW_DAYS = 7
QUOTE_FALLBACK = "원문 확인"

# 중복 병합 임계값 (판정 ⑥): 같은 소스·같은 주 제목 3-gram Jaccard
DEDUP_JACCARD = 0.6
NGRAM_SIZE = 3

# 인용 span을 찾을 수 없는 raw_data 키 (링크는 사람이 읽는 문구가 아니다)
_RAW_SKIP_KEYS = ("link", "url", "href")

# 메타데이터 괄호: 센터명 접두사·날짜·연장 표기가 들어간다 → **내용까지** 지운다.
# 「」『』는 한국 공문에서 사업·법령 **이름을 인용**하는 부호이므로 내용은 남기고
# 부호만 지운다(_SYMBOL_RE가 처리). 내용까지 지우면 제목의 알맹이가 사라져서
# "…참여자 추가모집 공고"끼리 엉뚱하게 병합된다.
# 한 단계 중첩까지 소비한다 — "('26.09.30.(수) 14:00, 대전)" 처럼 괄호 안에 괄호가
# 들어간 공공기관 제목이 흔하고, 바깥 괄호만 지우면 " 14:00, 대전)"이 본문에 남아
# 지역 태그가 엉뚱하게 붙는다.
_BRACKET_RE = re.compile(
    r"[(\[{（【<](?:[^()\[\]{}（）【】<>]|\([^()]*\))*[)\]}）】>]"
)
# 괄호 내용을 지우고 남은 알맹이가 이보다 짧으면 부호만 지우는 쪽으로 되돌린다.
_MIN_DEDUP_KEY_CHARS = 8
_DATE_TOKEN_RE = re.compile(
    r"\d{1,4}\s*[.\-/]\s*\d{1,2}(?:\s*[.\-/]\s*\d{1,2})?\.?"
    r"|\d{1,4}\s*년|\d{1,2}\s*월|\d{1,2}\s*일|\d{1,2}\s*차|\d{1,2}\s*분기"
)
_SYMBOL_RE = re.compile(r"[^0-9A-Za-z가-힣ㄱ-ㅎㅏ-ㅣ\s]")

# 인용 가능한 마감 표기 (특수한 것부터). 반환값은 원문에서 잘라낸 **문자 그대로**다.
_QUOTE_DEADLINE_PATTERNS = (
    r"\d{4}\s*\.\s*\d{1,2}\s*\.\s*\d{1,2}\s*\.?\s*~\s*\d{4}\s*\.\s*\d{1,2}\s*\.\s*\d{1,2}\s*\.?",
    r"~\s*\d{4}\s*[.\-/]\s*\d{1,2}\s*[.\-/]\s*\d{1,2}\s*\.?",
    r"\d{1,2}\s*[./]\s*\d{1,2}\s*~\s*\d{1,2}\s*[./]\s*\d{1,2}",
    r"~\s*\d{1,2}\s*[./]\s*\d{1,2}\s*\.?",
    r"\d{4}\s*[.\-/]\s*\d{1,2}\s*[.\-/]\s*\d{1,2}\s*까지",
    r"\d{1,2}\s*월\s*\d{1,2}\s*일\s*까지",
    r"연중\s*상시\s*모집",
    r"상시\s*모집",
)

_QUOTE_ELIGIBILITY_PATTERNS = (
    r"(?:참여|신청|지원|모집)\s*(?:자격|대상)\s*[:：]\s*[^\n]{2,60}",
    r"(?:참여|신청|지원|모집)\s*(?:자격|대상)\s*은?\s+[^\n]{2,60}",
    r"대상\s*[:：]\s*[^\n]{2,60}",
)

_QUOTE_AMOUNT_PATTERNS = (
    r"최대\s*\d[\d,]*\s*(?:억|천만|백만|만)?\s*원",
    r"\d[\d,]*\s*(?:억|천만|백만|만)\s*원",
    r"\d[\d,]*\s*원\s*(?:이내|한도|지원)",
)


class Classification(NamedTuple):
    """분류 결과 (순수 함수 classify_item의 반환값).

    verdict: VERDICT_APPLY | VERDICT_NOTICE | VERDICT_HOLD | VERDICT_EXCLUDE
    reason: 판정 사유 (배제·보류 목록에 그대로 쓴다)
    matched: 매칭 근거 키워드
    tags: 대상 태그 (없으면 빈 튜플 → 보류)
    region: 지역 한정 도명 (없으면 None)
    """

    verdict: str
    reason: str
    matched: Tuple[str, ...]
    tags: Tuple[str, ...]
    region: Optional[str]


def get_week_date_range(week_str: str) -> Tuple[str, str]:
    """ISO 주 표기(YYYY-Www)에서 시작/종료 날짜를 반환.

    Args:
        week_str: ISO 주 표기 (예: "2026-W13")

    Returns:
        (시작일, 종료일) 튜플 (YYYY-MM-DD 형식)
    """
    parts = week_str.split("-W")
    year = int(parts[0])
    week = int(parts[1])

    # ISO 8601: 주는 월요일부터 시작
    jan4 = datetime(year, 1, 4)
    week_one_monday = jan4 - timedelta(days=jan4.weekday())
    week_start = week_one_monday + timedelta(weeks=week - 1)
    week_end = week_start + timedelta(days=6)

    return week_start.strftime("%Y-%m-%d"), week_end.strftime("%Y-%m-%d")


def normalize_title(title: str) -> str:
    """제목 정규화: 공백 정규화 (개행·탭 제거)."""
    return " ".join((title or "").split())


def _strip_tokens(text: str) -> str:
    text = _DATE_TOKEN_RE.sub(" ", text)
    text = _SYMBOL_RE.sub(" ", text)
    return " ".join(text.split())


def dedup_key(title: str) -> str:
    """중복 판정용 강한 정규화 (판정 ⑥: 괄호·기호·날짜·「」 제거)."""
    text = normalize_title(title)
    stripped = _strip_tokens(_BRACKET_RE.sub(" ", text))
    if len(stripped.replace(" ", "")) >= _MIN_DEDUP_KEY_CHARS:
        return stripped
    # 제목이 사실상 괄호 안에만 있는 경우 — 부호만 지운다
    return _strip_tokens(text)


def title_ngrams(title: str, n: int = NGRAM_SIZE) -> frozenset:
    """정규화된 제목의 문자 n-gram 집합."""
    compact = dedup_key(title).replace(" ", "")
    if not compact:
        return frozenset()
    if len(compact) <= n:
        return frozenset([compact])
    return frozenset(compact[i:i + n] for i in range(len(compact) - n + 1))


def jaccard(left: frozenset, right: frozenset) -> float:
    """두 n-gram 집합의 Jaccard 유사도."""
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _hits(text: str, keywords: Sequence[str]) -> Tuple[str, ...]:
    """text에 포함된 키워드를 등장 순서가 아니라 사전 순서대로 반환."""
    return tuple(kw for kw in keywords if kw in text)


def strip_brackets(text: str) -> str:
    """대괄호·괄호 안 내용 제거 (센터명 접두사가 지역으로 오인되는 것을 막는다)."""
    return " ".join(_BRACKET_RE.sub(" ", normalize_title(text)).split())


def _scan_regions(text: str) -> List[str]:
    """텍스트에 등장하는 시·도명 (표시명 기준 중복 제거)."""
    found: List[str] = []
    for pattern, display in REGION_PATTERNS:
        if pattern in text and display not in found:
            found.append(display)
    return found


def _scan_broad_region(text: str) -> Optional[str]:
    for pattern, display in REGION_BROAD_PATTERNS:
        if pattern in text:
            return display
    return None


def _region_from(text: str) -> Optional[str]:
    """시·도가 하나면 그것, 여럿이거나 없으면 권역명, 그래도 없으면 None."""
    singles = _scan_regions(text)
    if len(singles) == 1:
        return singles[0]
    broad = _scan_broad_region(text)
    if broad:
        return broad
    return None


def infer_region(title: str) -> Optional[str]:
    """제목에서 지역 한정 도·광역·권역명을 추론 (개정 v2.4 (b)).

    ① 먼저 괄호 밖 본문을 본다 (`경기도 …`, `강원(춘천 권역) …`).
    ② 본문에서 못 찾으면 `[세종대전충청센터]` 같은 접두 괄호까지 포함해서 본다.
    여러 시·도가 섞여 있으면 권역명을 쓰고, 권역명도 없으면 붙이지 않는다(여러 지역 = 판정 불가).

    주의: announcements 스키마에는 소스별 지역 필드가 없다(스키마 변경 금지). 그래서
    판정 근거는 제목뿐이다 — 소스 지역 필드가 생기면 여기에 합친다.
    """
    body = strip_brackets(title)
    region = _region_from(body)
    if region:
        return region
    return _region_from(normalize_title(title))


def infer_target_tags(text: str) -> Tuple[str, ...]:
    """대상 태그 추론 (판정 ①). 못 붙이면 빈 튜플 → 호출자가 보류로 내린다."""
    tags: Set[str] = set()

    if "사회적협동조합" in text:
        tags.add(TAG_SOCIAL_COOP)
    # `사회적협동조합`을 지운 뒤에도 `협동조합`이 남으면 일반 협동조합도 대상이다.
    if "협동조합" in text.replace("사회적협동조합", ""):
        tags.add(TAG_COOP)
    if "사회적기업" in text or "소셜벤처" in text:
        tags.add(TAG_SOCIAL_ENTERPRISE)
    if "마을기업" in text:
        tags.add(TAG_VILLAGE)
    if _hits(text, FOREST_INDUSTRY_KEYWORDS):
        tags.add(TAG_FOREST_BIZ)

    if not tags and ("사회적경제" in text or "사회연대경제" in text):
        tags.add(TAG_ALL)

    return tuple(tag for tag in TAG_ORDER if tag in tags)


def classify_item(title: str, summary: str, source: str) -> Classification:
    """제목·요약·소스만 보고 섹션/보류/배제를 결정하는 순수 함수 (룰 v2.1).

    Args:
        title: 공고 제목
        summary: DB summary (없으면 빈 문자열)
        source: 크롤러 소스 키

    Returns:
        Classification. 마감 경과 판정은 날짜가 필요하므로 여기서 하지 않는다
        (compose 단계에서 VERDICT_EXCLUDE "마감 경과"로 강등된다).
    """
    title = normalize_title(title)
    summary = normalize_title(summary or "")
    text = f"{title} {summary}".strip()

    # ② 소스 풀. 협의회 소스가 아니면 제목의 사회적경제 정체성 키워드만이 통행권이다.
    if source not in COUNCIL_SOURCES:
        identity_in_title = _hits(title, SSE_IDENTITY_KEYWORDS)
        if not identity_in_title:
            return Classification(
                VERDICT_EXCLUDE, "협의회 소스 풀 외", (), (), None
            )

    # ③ 노이즈 사전
    noise = _hits(text, NOISE_KEYWORDS)
    if noise:
        return Classification(
            VERDICT_EXCLUDE, f"노이즈: {noise[0]}", noise, (), None
        )

    # ③ 관련성 = 산림 업종 ∨ 사회적경제 정체성
    forest = _hits(text, FOREST_INDUSTRY_KEYWORDS)
    identity = _hits(text, SSE_IDENTITY_KEYWORDS)
    if not forest and not identity:
        return Classification(VERDICT_EXCLUDE, "관련성 없음", (), (), None)

    relevance = forest + identity

    # ① 대상 태그를 못 붙이면 발송본에 싣지 않는다
    tags = infer_target_tags(text)
    if not tags:
        return Classification(VERDICT_HOLD, "대상 태그 없음", relevance, (), None)

    region = infer_region(title)

    # 기회·제도 신호는 **제목에서만** 인정한다 (룰 v2.1의 "제목/본문"을 제목으로 좁힘).
    # 근거(W37 실측): forest_press 보도자료 요약에는 "모집"·"정책"·"시행" 같은 상용구가
    # 늘 들어 있어서 본문까지 보면 관리소 활동 기사가 신청/제도 섹션으로 올라온다
    # ("산림청, 국익 중심 실용외교…" 가 신청하세요에 실렸다). 제목에 없는 기회는
    # 배제가 아니라 **보류**로 내려가므로 편집자가 `핀 n`으로 되살릴 수 있다(판정 ⑦).
    opportunity = _hits(title, OPPORTUNITY_KEYWORDS)
    if opportunity:
        # 개정 v2.2 F1: 참가자 모집(B2C)은 보류. 사업자 신호가 하나라도 있으면 통과.
        b2c = _hits(title, B2C_SIGNAL_KEYWORDS)
        b2b = _hits(title, B2B_SIGNAL_KEYWORDS)
        if b2c and not b2b:
            return Classification(
                VERDICT_HOLD,
                f"참가자 모집(B2C): {b2c[0]}",
                relevance + opportunity + b2c,
                tags,
                region,
            )
        return Classification(
            VERDICT_APPLY,
            f"기회: {opportunity[0]}",
            relevance + opportunity,
            tags,
            region,
        )

    institution = _hits(title, INSTITUTION_KEYWORDS)
    if institution:
        return Classification(
            VERDICT_NOTICE,
            f"제도: {institution[0]}",
            relevance + institution,
            tags,
            region,
        )

    return Classification(VERDICT_HOLD, "섹션 판정 불명", relevance, tags, region)


# ─── 마감 처리 (판정 ④) ──────────────────────────────────────────────────
_DEADLINE_FORMATS = ("%Y-%m-%d", "%Y.%m.%d", "%Y/%m/%d")


def parse_deadline(value: Optional[str]) -> Optional[date]:
    """구조화된 마감일 문자열을 date로. 파싱 불가는 None."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None

    for fmt in _DEADLINE_FORMATS:
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue

    matched = re.match(r"^(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일", text)
    if matched:
        try:
            return date(*(int(g) for g in matched.groups()))
        except ValueError:
            return None
    return None


def effective_deadline(
    period_start: Optional[str], period_end: Optional[str]
) -> Optional[date]:
    """마감일로 인정할 수 있는 date.

    크롤러 다수가 **게시일을 period_start/period_end에 같은 값으로** 넣는다
    (forest_service·fowi·socialenterprise·ipet 실측). 그것을 마감으로 믿으면
    당일 게시된 유효 공고가 '마감 경과'로 조용히 사라진다 — 같으면 마감 없음으로 본다.
    (구조화된 마감 추출 강화는 V2 크롤러 레인의 몫이다.)
    """
    end = parse_deadline(period_end)
    if end is None:
        return None
    start = parse_deadline(period_start)
    if start is not None and start == end:
        return None
    return end


def format_month_day(value: date) -> str:
    """9/22 형태."""
    return f"{value.month}/{value.day}"


def _quote_source_text(summary: Optional[str], raw_data: Optional[str]) -> str:
    """인용 span을 찾을 대상 텍스트 (사람이 읽는 필드만)."""
    parts: List[str] = []
    if summary:
        parts.append(summary)
    if raw_data:
        try:
            parsed = json.loads(raw_data)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            for key, value in parsed.items():
                if not isinstance(value, str):
                    continue
                if str(key).lower() in _RAW_SKIP_KEYS:
                    continue
                parts.append(value)
        elif isinstance(parsed, str):
            parts.append(parsed)
    return "\n".join(parts)


def _find_quote(text: str, patterns: Sequence[str]) -> Optional[str]:
    """패턴 순서대로 처음 맞는 구간을 **문자 그대로** 잘라낸다."""
    if not text:
        return None
    for pattern in patterns:
        matched = re.search(pattern, text)
        if matched:
            return " ".join(matched.group(0).split())
    return None


def extract_quotes(
    summary: Optional[str], raw_data: Optional[str]
) -> Dict[str, str]:
    """마감·자격·금액의 인용 span (판정 ⑦).

    DB에서 문자 그대로 찾을 수 있을 때만 값이 들어간다. 없으면 "원문 확인".
    필드 이름은 GLM 야간 레인(V3)이 나중에 채울 자리로 예약돼 있다.
    """
    text = _quote_source_text(summary, raw_data)
    return {
        "quote_deadline": _find_quote(text, _QUOTE_DEADLINE_PATTERNS)
        or QUOTE_FALLBACK,
        "quote_eligibility": _find_quote(text, _QUOTE_ELIGIBILITY_PATTERNS)
        or QUOTE_FALLBACK,
        "quote_amount": _find_quote(text, _QUOTE_AMOUNT_PATTERNS)
        or QUOTE_FALLBACK,
    }


def source_display_name(source: str) -> str:
    """기관 표시명 (판정 ⑤). 미등록 소스는 소스 키를 그대로 보여준다(조용히 비우지 않는다)."""
    return SOURCE_DISPLAY_NAMES.get(source, source or "기관 미상")


def target_display(tags: Sequence[str], region: Optional[str]) -> str:
    """`사협·사회적기업` / `사회적기업(경기)` 형태."""
    if not tags:
        return ""
    joined = "·".join(tags)
    return f"{joined}({region})" if region else joined


def load_form_responses(
    forms_csv_path: Optional[Path] = None,
) -> Tuple[Dict[str, List[str]], List[str], List[str]]:
    """forms/responses.csv에서 회원사 소식과 의견 정보 로드.

    Returns:
        ({"회원사명": ["동정내용", ...]}, ["의견1", ...], ["경고문", ...]) 튜플.
        세 번째 원소는 로드 실패 경고 목록(stderr에도 출력됨).
    """
    if forms_csv_path is None:
        forms_csv_path = Path("forms/responses.csv")

    responses: Dict[str, List[str]] = {}
    opinions: List[str] = []
    warnings: List[str] = []

    if not forms_csv_path.exists():
        return responses, opinions, warnings

    try:
        with open(forms_csv_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if not row:
                    continue

                item_type = row.get("유형", "").strip()
                content = row.get("내용", "").strip()
                company = row.get("회원사", "").strip()

                if not content:
                    continue

                if item_type == "동정" and company:
                    responses.setdefault(company, []).append(content)
                elif item_type == "의견":
                    opinions.append(content)
    except Exception as e:
        warning = f"폼 로드 실패: {e}"
        warnings.append(warning)
        print(f"Warning: {warning}", file=sys.stderr)

    return responses, opinions, warnings


def _parse_created_date(created_at: Optional[str]) -> Optional[date]:
    """created_at 문자열 → date."""
    if not created_at:
        return None
    return parse_deadline(str(created_at)[:10])


def _build_item(row: Sequence, classification: Classification, today: date) -> Dict:
    """DB 행 + 분류 결과 → 렌더에 필요한 항목 딕셔너리."""
    (
        item_id,
        source,
        title,
        summary,
        url,
        period_start,
        period_end,
        created_at,
        raw_data,
    ) = row

    deadline = effective_deadline(period_start, period_end)
    # 게시일 정본은 period_start다. created_at은 **크롤 시각**이므로 정렬 보조로만 쓰고
    # "새 소식" 판정에는 쓰지 않는다 — 크롤 날짜를 게시일로 믿으면 게시일이 없는 소스
    # (seis·coop 실측: period_start NULL)가 전부 "새 소식"이 되어 최신 슬롯을 독식한다.
    posted_known = _parse_created_date(period_start)
    posted = posted_known or _parse_created_date(created_at)
    quotes = extract_quotes(summary, raw_data)

    # 개정 v2.4 (a): 마감 표기 3단계. "마감 원문 확인" 단독 표기는 폐지.
    quoted = quotes["quote_deadline"]
    has_quote = quoted != QUOTE_FALLBACK
    if deadline is not None:
        days_left = (deadline - today).days
        label = f"D-{days_left}"
        deadline_short = format_month_day(deadline)
        deadline_display = f"마감 {deadline_short}"
        sort_bucket = 0
    else:
        days_left = None
        deadline_short = ""
        # 판정 ④: "새 소식"은 게시 7일 이내임을 **알 때만** 붙인다. 모르면 "상시"(보수).
        fresh = (
            posted_known is not None
            and (today - posted_known).days <= NEW_WINDOW_DAYS
        )
        label = LABEL_NEW if fresh else LABEL_STANDING
        # 인용 span이 있으면 "원문 확인"/"미정" 자리를 **원문 문구 그대로** 채운다
        # (판정 ⑦ — 창작이 아니라 인용이므로 정보를 버리지 않는다).
        tail = quoted if has_quote else None
        if posted_known is not None:
            deadline_display = "접수 {}부터, 마감 {}".format(
                format_month_day(posted_known), tail or "원문 확인"
            )
        else:
            deadline_display = f"마감 {tail or '미정'}"
        sort_bucket = 1 if fresh else 2

    item = {
        "id": item_id,
        "source": source,
        "org": source_display_name(source),
        "title": normalize_title(title),
        "summary": normalize_title(summary or ""),
        "url": url,
        "verdict": classification.verdict,
        "reason": classification.reason,
        "matched": list(classification.matched),
        "tags": list(classification.tags),
        "region": classification.region,
        "target": target_display(classification.tags, classification.region),
        "period_end": period_end or "",
        "deadline": deadline.isoformat() if deadline else "",
        "deadline_short": deadline_short,
        "deadline_display": deadline_display,
        "days_left": days_left,
        "label": label,
        "posted": posted.isoformat() if posted else "",
        "sort_bucket": sort_bucket,
        # 개정 v2.2 F2: 마감 없음 버킷에서 사회적경제 정체성 공고를 앞세우는 키
        "identity_priority": 0 if _hits(
            normalize_title(title), SSE_IDENTITY_KEYWORDS
        ) else 1,
        "similar_count": 0,
    }
    item.update(quotes)
    return item


def _dedup_same_source(items: List[Dict]) -> List[Dict]:
    """같은 소스·같은 주 안에서 제목 유사도 ≥ DEDUP_JACCARD면 대표 1건으로 병합 (판정 ⑥).

    개정 v2.2 F4: 제목이 같아도 **인정된 마감이 다르면 다른 회차**이므로 병합하지 않는다
    (W37 실측: `산림재난방지법 시행령 일부개정령안 입법예고`가 의견 9/16·10/19 두 건).
    병합할 때 대표는 **마감이 늦은(아직 열린) 쪽**이다. 마감 비교는 우리가 인정한
    effective deadline(`item["deadline"]`)으로 한다 — 게시일을 마감 칸에 넣은
    크롤러 산출물이 "다른 회차"로 위장하는 것을 막는다.
    """
    kept: List[Dict] = []
    kept_grams: List[frozenset] = []
    for item in items:
        grams = title_ngrams(item["title"])
        merged = False
        for index, (other, other_grams) in enumerate(zip(kept, kept_grams)):
            if other["source"] != item["source"]:
                continue
            if jaccard(grams, other_grams) < DEDUP_JACCARD:
                continue
            if item["deadline"] and other["deadline"] and (
                item["deadline"] != other["deadline"]
            ):
                # 다른 회차 — 병합하지 않는다
                continue
            merged = True
            if item["deadline"] > other["deadline"]:
                kept[index] = item
                kept_grams[index] = grams
            break
        if merged:
            continue
        kept.append(item)
        kept_grams.append(grams)
    return kept


def _mark_cross_source_similar(items: List[Dict]) -> None:
    """다른 소스 간 의미 중복은 자동 병합하지 않고 "유사 항목 n"으로만 표기 (판정 ⑥③)."""
    grams = [title_ngrams(item["title"]) for item in items]
    for i, item in enumerate(items):
        count = 0
        for j, other in enumerate(items):
            if i == j or other["source"] == item["source"]:
                continue
            if jaccard(grams[i], grams[j]) >= DEDUP_JACCARD:
                count += 1
        item["similar_count"] = count


def _posted_ordinal(item: Dict) -> int:
    """게시일의 서수 (없으면 0 = 가장 오래된 것으로 취급)."""
    posted = parse_deadline(item.get("posted") or "")
    return posted.toordinal() if posted else 0


def _sort_apply(items: List[Dict]) -> List[Dict]:
    """신청하세요 정렬 (판정 ④ + 개정 v2.2 F2).

    ① 마감 있음 → D-day 오름차순 ② 새 소식 ③ 상시.
    마감 없음 버킷 안에서는 **제목에 사회적경제 정체성 키워드가 있는 항목이 먼저**,
    그다음 게시일 내림차순 — 협의회 정체성 공고(예비사회적기업 지정 계획 등)가
    게시일 며칠 차이로 상한에서 밀려나던 것을 막는다.
    """
    return sorted(
        items,
        key=lambda item: (
            item["sort_bucket"],
            item["deadline"] or "9999-12-31",
            item["identity_priority"],
            -_posted_ordinal(item),
        ),
    )


def _sort_notice(items: List[Dict]) -> List[Dict]:
    """알아두세요: 게시일 내림차순."""
    return sorted(items, key=lambda item: -_posted_ordinal(item))


def compose_digest_data(
    db_path: str,
    week_str: Optional[str] = None,
    forms_csv_path: Optional[Path] = None,
    warnings_out: Optional[List[str]] = None,
    exclude_urls: Optional[Set[str]] = None,
    today: Optional[date] = None,
) -> Dict:
    """다이제스트 구조 데이터 생성 (렌더 전 단계).

    Returns:
        {"week", "week_start", "week_end", "period_label", "sections",
         "council_notes", "member_news", "holds", "excluded", "opinions"}
    """
    if week_str is None:
        iso = datetime.now().isocalendar()
        week_str = f"{iso[0]}-W{iso[1]:02d}"
    if today is None:
        today = date.today()

    week_start, week_end = get_week_date_range(week_str)

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    exclude_list = sorted(exclude_urls) if exclude_urls else []
    exclude_sql = ""
    if exclude_list:
        exclude_sql = " AND url NOT IN (%s)" % ", ".join("?" * len(exclude_list))

    # 판정 ④의 정렬 정본. 빈 문자열도 "마감 없음"으로 취급하려고 NULLIF를 쓴다.
    cursor.execute(
        f"""
        SELECT id, source, title, summary, url, period_start, period_end,
               created_at, raw_data
        FROM announcements
        WHERE DATE(created_at) BETWEEN ? AND ?{exclude_sql}
        ORDER BY (NULLIF(period_end, '') IS NULL),
                 NULLIF(period_end, ''),
                 created_at DESC
        """,
        (week_start, week_end, *exclude_list),
    )
    rows = cursor.fetchall()
    conn.close()

    survivors: List[Dict] = []
    excluded: List[Dict] = []

    for row in rows:
        source, title, summary, url = row[1], row[2], row[3], row[4]
        if exclude_urls and url in exclude_urls:
            continue

        classification = classify_item(title, summary, source)
        item = _build_item(row, classification, today)

        if classification.verdict == VERDICT_EXCLUDE:
            excluded.append(item)
            continue

        # 마감 경과는 신청 섹션에서만 배제 사유다 (판정 ④).
        if (
            classification.verdict == VERDICT_APPLY
            and item["deadline"]
            and item["days_left"] is not None
            and item["days_left"] < 0
        ):
            item["verdict"] = VERDICT_EXCLUDE
            item["reason"] = "마감 경과"
            excluded.append(item)
            continue

        survivors.append(item)

    survivors = _dedup_same_source(survivors)

    sections: Dict[str, List[Dict]] = {VERDICT_APPLY: [], VERDICT_NOTICE: []}
    holds: List[Dict] = []
    apply_candidates: List[Dict] = []
    for item in survivors:
        if item["verdict"] == VERDICT_APPLY:
            apply_candidates.append(item)
        elif item["verdict"] == VERDICT_NOTICE:
            sections[VERDICT_NOTICE].append(item)
        else:
            holds.append(item)

    # 개정 v2.2 F2 + v2.3 G2: 정렬 상위부터 상한(5)을 채우며 같은 소스 3번째부터 건너뛴다.
    # 다양성 사유는 **실제로 다양성 때문에 밀린 항목에만** 붙고, 상한이 이미 찬 뒤에 남은
    # 항목은 `상한 초과`로만 기록한다(편집자에게 사유가 과장돼 보이지 않게).
    selected: List[Dict] = []
    diversity_skipped: List[Dict] = []
    cap_overflow: List[Dict] = []
    per_source: Counter = Counter()
    for item in _sort_apply(apply_candidates):
        if len(selected) >= SECTION_LIMITS[VERDICT_APPLY]:
            cap_overflow.append(item)
            continue
        if per_source[item["source"]] >= SOURCE_DIVERSITY_LIMIT:
            diversity_skipped.append(item)
            continue
        per_source[item["source"]] += 1
        selected.append(item)

    for item, reason in (
        [(item, HOLD_REASON_DIVERSITY) for item in diversity_skipped]
        + [(item, HOLD_REASON_SECTION_CAP) for item in cap_overflow]
    ):
        item["verdict"] = VERDICT_HOLD
        item["reason"] = reason
        holds.append(item)

    sections[VERDICT_APPLY] = selected
    sections[VERDICT_NOTICE] = _sort_notice(sections[VERDICT_NOTICE])[
        : SECTION_LIMITS[VERDICT_NOTICE]
    ]

    published = sections[VERDICT_APPLY] + sections[VERDICT_NOTICE]
    _mark_cross_source_similar(published)

    for number, item in enumerate(holds, start=1):
        item["number"] = number

    form_responses, opinions, form_warnings = load_form_responses(forms_csv_path)
    if warnings_out is not None:
        warnings_out.extend(form_warnings)

    start = parse_deadline(week_start)
    end = parse_deadline(week_end)
    period_label = (
        f"{format_month_day(start)}~{format_month_day(end)}"
        if start and end
        else f"{week_start} ~ {week_end}"
    )

    return {
        "week": week_str,
        "week_start": week_start,
        "week_end": week_end,
        "period_label": period_label,
        "sections": sections,
        "council_notes": [],
        "member_news": form_responses,
        "holds": holds,
        "excluded": excluded,
        "opinions": opinions,
    }


def item_line(item: Dict) -> str:
    """발송본 형식 v2.1의 항목 1행 (링크는 다음 줄)."""
    parts = [f"{item['org']}"]
    if item["target"]:
        parts.append(f"대상: {item['target']}")

    if item["verdict"] == VERDICT_NOTICE:
        if item["deadline_short"]:
            parts.append(f"의견 {item['deadline_short']}까지")
    else:
        parts.append(item["deadline_display"])

    if item["similar_count"]:
        parts.append(f"유사 항목 {item['similar_count']}")

    head = item["title"]
    if item["verdict"] == VERDICT_APPLY:
        head = f"[{item['label']}] {head}"

    return f"{head} — " + " · ".join(parts)


def _member_news_lines(member_news: Dict[str, List[str]]) -> List[str]:
    lines: List[str] = []
    for company, contents in sorted(member_news.items()):
        lines.append(f"**{company}**")
        for content in contents:
            lines.append(f"- {content}")
        lines.append("")
    return lines


def render_markdown(data: Dict) -> str:
    """발송본 마크다운 (형식 v2.1).

    보류 목록은 맨 아래 HTML 주석으로만 남는다 — send_digest의 마크다운→HTML
    변환이 `<!--` 로 시작하는 줄을 건너뛰므로 발송 HTML에는 실리지 않는다.
    """
    lines = [
        f"<!-- lane: {LANE} -->",
        "",
        f"# 📋 협의회 주간 정책브리핑 {data['week']} ({data['period_label']})",
        "",
        f"이번 주 한 줄: {MARKER}",
        "",
    ]

    for section in ITEM_SECTIONS:
        lines.append(f"## {SECTION_HEADINGS[section]}")
        lines.append("")
        items = data["sections"].get(section) or []
        if not items:
            lines.append("*(항목 없음)*")
            lines.append("")
            continue
        for item in items:
            lines.append(item_line(item))
            lines.append(f"  [원문]({item['url']})")
            lines.append("")

    # 개정 v2.4 (c): 협의회에서·회원사 소식은 내용이 없으면 섹션 자체를 생략한다
    # (빈 자리 표시 금지 — 발송본에 남은 플레이스홀더는 브리핑의 신뢰를 깎는다).
    if data.get("council_notes"):
        lines.append(f"## {SECTION_HEADINGS[SECTION_COUNCIL]}")
        lines.append("")
        for note in data["council_notes"]:
            lines.append(f"· {note}")
        lines.append("")

    # 판정 ⑩: 회원사 소식은 없으면 섹션을 생략한다.
    if data.get("member_news"):
        lines.append(f"## {SECTION_HEADINGS[SECTION_MEMBER]}")
        lines.append("")
        lines.extend(_member_news_lines(data["member_news"]))

    if data.get("holds"):
        for item in data["holds"]:
            title = item["title"].replace("--", "—").replace(">", "＞")
            lines.append(f"<!-- 보류: {item['number']}. {title} | {item['reason']} -->")
        lines.append("")

    return "\n".join(lines)


def render_kakao(data: Dict, headline: Optional[str] = None) -> str:
    """카톡 평문 렌더 (같은 틀, 마크다운 장식·보류 주석 없음)."""
    lines = [
        f"📋 협의회 주간 정책브리핑 {data['week']} ({data['period_label']})",
        f"이번 주 한 줄: {headline.strip() if headline else '(확정 필요)'}",
        "",
    ]

    for section in ITEM_SECTIONS:
        lines.append(SECTION_HEADINGS[section])
        items = data["sections"].get(section) or []
        if not items:
            lines.append("  (항목 없음)")
        for item in items:
            lines.append(item_line(item))
            lines.append(f"  {item['url']}")
        lines.append("")

    if data.get("council_notes"):
        lines.append(SECTION_HEADINGS[SECTION_COUNCIL])
        for note in data["council_notes"]:
            lines.append(f"· {note}")
        lines.append("")

    if data.get("member_news"):
        lines.append(SECTION_HEADINGS[SECTION_MEMBER])
        for company, contents in sorted(data["member_news"].items()):
            for content in contents:
                lines.append(f"· {company}: {content}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def compose_digest(
    db_path: str,
    week_str: Optional[str] = None,
    output_path: Optional[Path] = None,
    forms_csv_path: Optional[Path] = None,
    warnings_out: Optional[List[str]] = None,
    exclude_urls: Optional[Set[str]] = None,
    today: Optional[date] = None,
) -> str:
    """주간 정책브리핑 다이제스트 마크다운 생성 (계약 v2.1).

    섹션 상한은 고정값(SECTION_LIMITS)이며 호출자가 조정할 수 없다.

    Args:
        db_path: announcements.db 경로
        week_str: ISO 주 표기 (기본: 현재 주, 예: "2026-W37")
        output_path: 출력 파일 경로. 주어지면 같은 이름의 `.kakao.txt`도 함께 쓴다
        forms_csv_path: 폼 CSV 경로
        warnings_out: 경고 수집용 리스트 (폼 로드 실패 등)
        exclude_urls: 제외할 원문 URL 집합 (계약 v1.2: url_alive=false 자동 제외)
        today: 기준 날짜 (기본: 오늘). D-day·새 소식/상시 판정에 쓰인다

    Returns:
        생성된 마크다운 텍스트
    """
    data = compose_digest_data(
        db_path=db_path,
        week_str=week_str,
        forms_csv_path=forms_csv_path,
        warnings_out=warnings_out,
        exclude_urls=exclude_urls,
        today=today,
    )

    markdown = render_markdown(data)

    if output_path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(markdown, encoding="utf-8")
        output_path.with_name(
            output_path.name.replace(".md", "") + ".kakao.txt"
        ).write_text(render_kakao(data), encoding="utf-8")

    if data["opinions"] and output_path:
        opinions_path = output_path.with_name(
            output_path.name.replace(".md", ".opinions.md")
        )
        opinions_lines = [
            f"<!-- lane: {LANE} -->",
            "",
            f"# 협의회 의견 {data['week']}",
            "",
        ]
        for opinion in data["opinions"]:
            opinions_lines.append(f"- {opinion}")
            opinions_lines.append("")

        opinions_path.write_text("\n".join(opinions_lines), encoding="utf-8")

    return markdown
