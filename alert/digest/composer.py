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
import hashlib
import json
import re
import sqlite3
import sys
import unicodedata
from collections import Counter
from datetime import date, datetime, timedelta, timezone
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
    "계획",
    "발표",
)

# 개정 v2.5 (#10): 이 키워드가 제목에 있으면 `공고`가 같이 있어도 알아두세요가 우선이다
# (`산지관리법 시행령 개정 입법예고 공고`가 신청 섹션으로 갔고, 의견 마감이 지나면
# 제도 변경 자체가 배제됐다).
INSTITUTION_PRIORITY_KEYWORDS = ("입법예고", "행정예고", "고시")

# 단독으로는 너무 넓은 제도 키워드 — 산림 업종 키워드를 동반할 때만 인정한다.
BARE_INSTITUTION_KEYWORDS = ("계획", "발표")

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
HOLD_REASON_URL_TOO_LONG = "URL 길이 초과"

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

# ─── 항목 줄 형식 (형식 v2.1) — 블록 파서의 정본 (사이클 6 #1) ───────────
#   신청하세요: `[라벨] 제목 — 기관 · 대상: … · 마감 …`
#   알아두세요: `제목 — 기관 · 대상: … · 의견 …까지`
# 라벨은 _deadline_fields 가 만드는 것만 인정한다 — 제목에 원래 있던 임의의
# 대괄호(`[모집]`·`[공고]`)는 제목의 일부이며 항목 판정 근거가 아니다.
# 이 정규식을 쓰는 곳은 alert.digest.blocks 하나이며, 항목 판정은 여기에
# **다음 줄이 `  [원문](URL)`** 이라는 조건을 더해서만 성립한다.
ITEM_LABELS = (LABEL_NEW, LABEL_STANDING, "마감 미정")
ITEM_LABEL_PATTERN = "(?:D-\\d+|{})".format(
    "|".join(re.escape(label) for label in ITEM_LABELS)
)
ITEM_LINE_RE = re.compile(
    r"^(?:\[(?P<label>" + ITEM_LABEL_PATTERN + r")\]\s+)?"
    r"(?P<title>\S.*?)\s+—\s+(?P<rest>\S.*)$"
)

# 항목 블록의 **구조 마커** (Codex 3차 HIGH #2). 항목 줄 바로 앞에 놓인다.
# 정규식만으로 항목을 판정하면 양방향 오류가 난다 — 산문이 항목으로 세어지고
# (`자료를 참고해 주세요 — 자세한 내용은 …` + 원문 줄 → 공고 0건인데 pass),
# `*산림 제도 개정 — 산림청` 같은 위조 항목이 상한을 우회한다.
# 이 마커는 composer 만 쓴다 — 발송 HTML·카톡 렌더는 주석 줄을 버리므로 실리지 않는다.
ITEM_MARKER_TEMPLATE = "<!-- item id={} -->"
ITEM_MARKER_RE = re.compile(r"^<!--\s*item\s+id=(\S+)\s*-->$")

# 항목이 하나도 없는 항목 섹션에 남기는 표시 (블록 파서·prune 이 같은 문자열을 쓴다)
EMPTY_SECTION_LINE = "*(항목 없음)*"
# 카톡 평문에서의 같은 표시
KAKAO_EMPTY_SECTION_LINE = "  (항목 없음)"
_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")

# "유사 항목 n" **표시** 임계값 (판정 ⑥③). 병합에는 쓰지 않는다 — 사이클 10 #1에서
# 유사도 병합을 폐기했다. 유사도는 편집자에게 알리는 신호일 뿐이고, 무엇을 지울지는
# 사람이 `제외` 로 판단한다.
SIMILAR_JACCARD = 0.6
NGRAM_SIZE = 3

# 개정 v2.6 (8): created_at에 오프셋이 있으면 KST로 환산해 주간 창을 판정한다.
KST = timezone(timedelta(hours=9))
# SQL 문자열 범위는 인덱스용 1차 필터라 앞뒤로 하루 넓힌다(오프셋 최대 ±14h 흡수).
WINDOW_PREFILTER_SLACK_DAYS = 1

# 연장·재공고 표지 (개정 v2.6 (1)). 마감이 비어 있는데 이 말이 붙어 있으면
# 앞 회차의 만료된 마감을 상속시키지 않는다.
RENEWAL_KEYWORDS = ("연장", "재공고", "추가모집", "재모집", "2차")

# 카톡 평문 분할 (개정 v2.5 #12). 카카오톡 한 메시지 한도와 같은 4096자.
KAKAO_CHUNK_LIMIT = 4096
KAKAO_CHUNK_SEPARATOR = "---8<---"
KAKAO_HEADLINE_PREFIX = "이번 주 한 줄: "
KAKAO_HEADLINE_PLACEHOLDER = "(확정 필요)"

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
    r"[(\[{（【［<]"
    r"(?:[^()\[\]{}（）【】［］<>]|\([^()]*\)|\[[^\[\]]*\]|［[^［］]*］)*"
    r"[)\]}）】］>]"
)
# 접두 괄호 — 자격 지역은 여기서 확정한다 (개정 v2.6 (2)). 전각 대괄호도 인식.
_PREFIX_BRACKET_RE = re.compile(
    r"^\s*[(\[（【［]([^()\[\]（）【】［］]*)[)\]）】］]"
)
# 행사 장소 문맥 — 자격 지역이 아니다 (개정 v2.6 (2)).
# 사이클 13 #4: `에서` 는 **장소 동사가 따를 때만** 장소 신호다.
# `[경기에서 사업하는 기업]` 은 자격 조건이지 행사 장소가 아니다 —
# 토큰 존재만으로 괄호를 버리면 자격 지역이 사라진다(대상이 넓어져 오배포).
_VENUE_VERBS = r"개최|진행|열린|열립|열리|실시"
_VENUE_CONTEXT_RE = re.compile(
    r"(?:개최\s*)?장소\s*[:：][^)\]]*"
    r"|[가-힣]{2,5}에서\s*[^)\]]{0,10}?(?:" + _VENUE_VERBS + r")"
    r"|개최지\s*[:：][^)\]]*"
)
# 콜론 없는 장소 문맥 (사이클 11 #5). `[설명회 장소 서울]`·`[서울 개최]` 처럼
# 구분 기호가 없어도 행사 장소는 자격 지역이 아니다. **괄호 그룹 안에서만** 본다.
#
# 사이클 12 #6: 단독 `장` 을 뺐다. `[경기 사업장 보유 기업]` 처럼 **자격 조건**에
# 들어간 `장` 을 장소 신호로 읽어 지역 제한이 사라졌다 — 자격 지역을 잃는 것은
# 행사 장소를 지역으로 오인하는 것보다 나쁘다(대상이 넓어져 오배포가 된다).
_VENUE_TOKEN_RE = re.compile(
    r"장소|개최|회장|행사장|에서\s*[^)\]]{0,10}?(?:" + _VENUE_VERBS + r")"
)
# 괄호 내용을 지우고 남은 알맹이가 이보다 짧으면 부호만 지우는 쪽으로 되돌린다.
_MIN_DEDUP_KEY_CHARS = 8
_DATE_TOKEN_RE = re.compile(
    r"\d{1,4}\s*[.\-/]\s*\d{1,2}(?:\s*[.\-/]\s*\d{1,2})?\.?"
    r"|\d{1,4}\s*년|\d{1,2}\s*월|\d{1,2}\s*일|\d{1,2}\s*차|\d{1,2}\s*분기"
)
_SYMBOL_RE = re.compile(r"[^0-9A-Za-z가-힣ㄱ-ㅎㅏ-ㅣ\s]")


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


# 개정 v2.5 (#2): 제목은 링크·HTML을 렌더하지 않는다. 링크는 "원문" 필드로만 나간다.
# 개정 v2.6 (3): 괄호 내용이 **URL일 때만** 링크로 본다. `[모집](~9.30)`,
# `[모집](산림사업자)` 같은 정상 표기는 원문 그대로 보존한다.
_MD_LINK_IN_TITLE_RE = re.compile(
    r"\[([^\]]*)\]\(\s*(?:https?://|www\.)(?:[^()\s]|\([^()]*\))*\)"
)
_AUTOLINK_RE = re.compile(r"<(\s*(?:https?://|www\.)[^>]*)>")


# 제목 선두에서 **문서 구조를 바꿀 수 있는** 마크다운 구문 → 전각 치환 (사이클 8 #2).
# DB 제목이 `## 산림 제도 개정` 이면 발송본에서 섹션 헤딩이 되어 뒤 항목이 전부 산문
# 섹션으로 밀려났고, `*산림 제도 개정` 이면 정상 항목이 산문으로 판정돼 0건·fail 이었다.
# 제목은 사람이 읽는 문자열이고, 구조는 composer 만 만든다 — 그 경계를 문자로 못박는다.
_LEADING_FULLWIDTH = {
    "#": "＃", "*": "＊", "-": "－", "+": "＋", ">": "＞", "|": "｜",
    "~": "～", "=": "＝", "·": "・", "•": "・",
}
_LEADING_ORDERED_RE = re.compile(r"^(\d+)\.")
# 제목의 모든 괄호 그룹 (선두·중간·후미) — 병합 서명과 지역 탐색이 함께 쓴다.
_ANY_BRACKET_GROUP_RE = re.compile(
    r"[(\[（【［]([^()\[\]（）【】［］]*)[)\]）】］]"
)
_TRAILING_BRACKET_RE = re.compile(
    r"[(\[（【［]([^()\[\]（）【】［］]*)[)\]）】］]\s*$"
)
# 항목 줄의 구분자와 겹치는 em dash — 제목 안에 있으면 제목/꼬리 경계가 흔들린다.
_EM_DASH = "—"
_EN_DASH = "–"


def neutralize_title_structure(text: str) -> str:
    """제목 선두의 마크다운 구문을 중화한다 (사이클 8 #2).

    ①선두 기호 런(`#`·`*`·`-`·`>` …) → 전각
    ②선두 번호 목록 `1.` → `1．`
    ③제목 안의 em dash → en dash (항목 줄 구분자 ` — ` 와 겹치지 않게)

    선두 대괄호(`[모집]`)는 **중화하지 않는다** (사이클 9 #6). 링크 판정은 괄호 내용이
    URL 일 때만이고, 항목 줄 판정은 라벨 목록과 정확히 일치할 때만이므로 무해하다 —
    `[D-9]` 흉내는 items.json 제목 대조가 잡는다(사이클 8 #1).
    """
    if not text:
        return text
    index = 0
    while index < len(text) and text[index] in _LEADING_FULLWIDTH:
        index += 1
    if index:
        text = "".join(_LEADING_FULLWIDTH[c] for c in text[:index]) + text[index:]
    matched = _LEADING_ORDERED_RE.match(text)
    if matched:
        text = matched.group(1) + "．" + text[matched.end():]
    return text.replace(_EM_DASH, _EN_DASH)


def sanitize_title(title: str) -> str:
    """제목에서 링크 문법·HTML 태그·**문서 구조 구문**을 제거한다.

    - `[텍스트](http…)` → `텍스트` (URL은 버린다 — 링크는 "원문" 필드의 몫)
    - `<https://…>` → `https://…` (꺾쇠 제거)
    - 남은 `<`·`>`는 전각으로 바꿔 태그가 만들어지지 못하게 한다
    - 괄호 내용이 URL이 아니면 표기를 그대로 둔다 (`[모집]`·`(~9.30)` 보존)
    - **선두 마크다운 구문은 전각으로 중화한다** (사이클 8 #2 — 구조 변형 차단)
    """
    text = normalize_title(title)
    text = _MD_LINK_IN_TITLE_RE.sub(lambda m: m.group(1), text)
    text = _AUTOLINK_RE.sub(lambda m: m.group(1).strip(), text)
    text = text.replace("<", "＜").replace(">", "＞")
    text = neutralize_title_structure(" ".join(text.split()))
    return " ".join(text.split())


def _strip_tokens(text: str) -> str:
    text = _DATE_TOKEN_RE.sub(" ", text)
    text = _SYMBOL_RE.sub(" ", text)
    return " ".join(text.split())


def merge_title_key(title: str) -> str:
    """병합 판정용 **보수** 정규화 — 공백(Z*)과 구두점(P*)만 지운다 (사이클 11 #1).

    지우는 것은 **공백(Z*)뿐**이다. 구두점·숫자·기호·문자는 전부 남는다 —
    구두점 하나가 금액·회차를 가르기 때문이다(사이클 12 #1):
      `최대 1.5억원 지원사업` / `최대 15억원 지원사업`  → 구두점을 지우면 같은 키
      `(1·3차)` / `(13차)`                              → 같음
    구두점만 다른 제목은 병합하지 않고 "유사 항목" 으로 표시한다. 판단은 편집자 몫이다.

    공백은 지운다 — `사업개발비 지원사업` 과 `사업개발비지원사업` 은 같은 공고이고,
    갈라놓으면 중복이 슬롯을 두 개 차지한다(Codex 7차 LOW #6).
    병합은 이 키가 **완전히 같을 때만** 일어난다.
    """
    return "".join(
        char for char in normalize_title(title)
        if not (char.isspace() or unicodedata.category(char)[0] == "Z")
    )


def dedup_key(title: str) -> str:
    """중복 판정용 정규화 — **괄호 내용은 남긴다** (사이클 9 #2).

    예전에는 괄호 그룹의 내용까지 지웠다(`_BRACKET_RE`). 그 결과
    `… 지원사업 모집 [경기]` 와 `… 지원사업 모집 [강원]` 의 정규화 결과가 같아져,
    지역이 다른 유효 공고가 하나로 병합되며 한 건이 sections·holds 양쪽에서
    사라졌다(Codex 5차 HIGH #3). 지역·회차·센터명은 괄호 안에 들어오는 일이 많으므로
    괄호 내용은 판정의 **근거**다. 지우는 것은 부호·날짜 토큰·공백뿐이다.
    """
    return _strip_tokens(normalize_title(title))


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


_REGION_SUFFIXES = ("센터", "권역", "광역", "특별", "지역", "도", "시", "권")
# 처소격 조사 — `경기에서 사업하는 기업`의 `경기`는 지역이다 (사이클 13 #4).
# `에`는 뒤가 음절로 이어지면(`에너지`) 낱말의 일부이므로 경계로 치지 않는다.
_REGION_PARTICLES = ("에서", "에는", "에도", "에만")


def _is_hangul_syllable(char: str) -> bool:
    return bool(char) and "가" <= char <= "힣"


def _region_boundary_ok(text: str, pattern: str, pos: int) -> bool:
    """지역 토큰 경계 규칙 (개정 v2.6 (4) + 사이클 13 #4).

    `경기침체`·`대구목재`처럼 다른 낱말에 묻힌 토큰은 지역으로 인정하지 않는다.
    허용: 문자열 끝 / 공백 / 구두점 / {센터·도·시·권·권역·지역·광역·특별} /
    처소격 조사(`에서`·`에`) / 바로 다음에 또 다른 지역·권역 토큰
    (센터명 연쇄: `세종대전충청센터`).
    """
    rest = text[pos + len(pattern):]
    if not rest:
        return True
    head = rest[0]
    if head.isspace():
        return True
    if not (head.isalnum() or "가" <= head <= "힣"):
        return True
    if any(rest.startswith(suffix) for suffix in _REGION_SUFFIXES):
        return True
    if any(rest.startswith(particle) for particle in _REGION_PARTICLES):
        return True
    if rest.startswith("에") and not _is_hangul_syllable(rest[1:2]):
        return True
    if any(rest.startswith(other) for other, _ in REGION_PATTERNS):
        return True
    if any(rest.startswith(other) for other, _ in REGION_BROAD_PATTERNS):
        return True
    return False


def _scan_regions(text: str) -> List[str]:
    """텍스트에 등장하는 시·도명 (표시명 기준 중복 제거, 경계 규칙 적용)."""
    found: List[str] = []
    for pattern, display in REGION_PATTERNS:
        if display in found:
            continue
        start = 0
        while True:
            pos = text.find(pattern, start)
            if pos < 0:
                break
            if _region_boundary_ok(text, pattern, pos):
                found.append(display)
                break
            start = pos + 1
    return found


def _scan_broad_region(text: str) -> Optional[str]:
    for pattern, display in REGION_BROAD_PATTERNS:
        pos = text.find(pattern)
        if pos >= 0 and _region_boundary_ok(text, pattern, pos):
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


def prefix_brackets(title: str) -> List[str]:
    """제목 맨 앞에 연달아 붙은 괄호 그룹의 내용 (등장 순서)."""
    groups: List[str] = []
    rest = normalize_title(title)
    while True:
        matched = _PREFIX_BRACKET_RE.match(rest)
        if not matched:
            break
        groups.append(matched.group(1).strip())
        rest = rest[matched.end():]
    return groups


def prefix_bracket(title: str) -> str:
    """제목 맨 앞 괄호의 내용 (없으면 빈 문자열)."""
    groups = prefix_brackets(title)
    return groups[0] if groups else ""


def prefix_signature(title: str) -> str:
    """제목 **선두** 괄호 그룹의 전체 문자열 (지역 탐색 진단용)."""
    return "".join(f"[{group}]" for group in prefix_brackets(title))


def bracket_regions(title: str) -> Tuple[str, ...]:
    """제목의 **모든** 괄호 그룹에서 찾은 지역 (선두·중간·후미, 등장 순서).

    선두만 보면 후미 괄호(`… 모집 [경기]`)를 놓친다 — 지역이 양쪽 None 이 되어
    병합 키가 같아졌다(Codex 5차 HIGH #3). 진단·테스트용이며, 판정 정본은
    infer_region 이다(이 함수와 같은 그룹 집합을 본다).
    """
    found = []
    for group in _ANY_BRACKET_GROUP_RE.findall(normalize_title(title)):
        if is_venue_group(group):
            continue
        region = _region_from(_drop_venue_context(group))
        if region and region not in found:
            found.append(region)
    return tuple(found)


def trailing_brackets(title: str) -> List[str]:
    """제목 **후미**에 붙은 괄호 그룹의 내용 (뒤에서부터 순서대로)."""
    groups: List[str] = []
    rest = normalize_title(title).rstrip()
    while True:
        matched = _TRAILING_BRACKET_RE.search(rest)
        if not matched or matched.end() != len(rest):
            break
        groups.append(matched.group(1).strip())
        rest = rest[: matched.start()].rstrip()
    return groups


def _drop_venue_context(text: str) -> str:
    """행사 장소 문맥을 지운다 — 자격 지역이 아니다."""
    return " ".join(_VENUE_CONTEXT_RE.sub(" ", text).split())


def is_venue_group(group: str) -> bool:
    """괄호 그룹이 **행사 장소** 문맥인가 (사이클 11 #5).

    콜론이 있든 없든(`장소: 서울`·`장소 서울`·`서울 개최`) 장소 토큰이 보이면
    그 괄호의 지역은 신청 자격이 아니다.
    """
    return bool(_VENUE_TOKEN_RE.search(group or ""))


def infer_region(title: str) -> Optional[str]:
    """제목에서 **신청 자격 지역**을 추론 (개정 v2.4 (b) + v2.6 (2)(4)).

    ① 접두 괄호 그룹을 **순서대로 전부** 훑는다 (`[모집][경기]`). 지역을 만나면
       거기서 확정하고 끝낸다 — 2차 탐색을 하면 행사 장소(`(설명회 장소: 서울)`)가
       섞여 자격 지역이 사라진다. 인식된 비지역 태그(`[모집]`)는 건너뛰고,
       알 수 없는 접두사(기관명)에서는 멈춘다.
    ② 접두 괄호에 지역이 없으면 괄호 밖 본문을 본다(장소 문맥 제거 후).
    여러 시·도가 섞여 있으면 권역명을 쓰고, 권역명도 없으면 붙이지 않는다.

    주의: announcements 스키마에는 소스별 지역 필드가 없다(스키마 변경 금지). 그래서
    판정 근거는 제목뿐이다 — 소스 지역 필드가 생기면 여기에 합친다.
    """
    # 사이클 9 #2: 접두 괄호 그룹을 **전부** 훑는다. 예전에는 알 수 없는 태그
    # (`[모집공고]`)에서 멈춰서 그 뒤의 `[경기]` 를 놓쳤다 — 지역이 None 이 되어
    # 경기/강원 공고가 하나로 병합됐다(Codex 5차 HIGH #3·HIGH #1 잔여).
    for group in prefix_brackets(title):
        # 사이클 10 #6 + 11 #5: 선두 괄호에도 장소 문맥을 걷어낸다 —
        # `[설명회 장소: 서울]`·`[설명회 장소 서울]` 은 자격 지역이 아니다.
        if is_venue_group(group):
            continue
        region = _region_from(_drop_venue_context(group))
        if region:
            # 괄호에서 확정하고 끝낸다 — 괄호 밖 2차 탐색을 하면 행사 장소
            # (`(설명회 장소: 서울)`)가 섞여 자격 지역이 사라진다.
            return region

    # **후미** 괄호도 본다 (`… 지원사업 모집 [경기]`·`…(충청 권역)`).
    for group in trailing_brackets(title):
        if is_venue_group(group):
            continue
        region = _region_from(_drop_venue_context(group))
        if region:
            return region

    body = _drop_venue_context(strip_brackets(title))
    return _region_from(body)


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


def _institution_hits(
    title: str, relevance: Tuple[str, ...]
) -> Tuple[str, ...]:
    """제목의 제도 키워드.

    개정 v2.6 (6): 단독 `계획`·`발표`는 **산림 업종 또는 사회적경제 정체성** 키워드를
    동반할 때 인정한다(`사회적기업 성장 추진계획` → 알아두세요). 계약에 없던 산림
    한정 조건을 걷어냈다.
    """
    hits = _hits(title, INSTITUTION_KEYWORDS)
    if not hits:
        return ()
    if relevance:
        return hits
    return tuple(kw for kw in hits if kw not in BARE_INSTITUTION_KEYWORDS)


def classify_item(title: str, summary: str, source: str) -> Classification:
    """제목·요약·소스만 보고 섹션/보류/배제를 결정하는 순수 함수 (룰 v2.1 + 개정 v2.5).

    Args:
        title: 공고 제목
        summary: DB summary (없으면 빈 문자열)
        source: 크롤러 소스 키

    Returns:
        Classification. 마감 경과 판정은 날짜가 필요하므로 여기서 하지 않는다
        (compose 단계에서 중복 병합 **뒤에** VERDICT_EXCLUDE "마감 경과"로 강등된다).
    """
    title = normalize_title(title)
    summary = normalize_title(summary or "")
    text = f"{title} {summary}".strip()

    # ② 소스 풀. 협의회 소스가 아니면 제목의 사회적경제 정체성 키워드만이 통행권이다.
    if source not in COUNCIL_SOURCES:
        if not _hits(title, SSE_IDENTITY_KEYWORDS):
            return Classification(
                VERDICT_EXCLUDE, "협의회 소스 풀 외", (), (), None
            )

    # ③ 관련성 = 산림 업종 ∨ 사회적경제 정체성
    forest = _hits(text, FOREST_INDUSTRY_KEYWORDS)
    identity = _hits(text, SSE_IDENTITY_KEYWORDS)
    relevance = forest + identity
    tags = infer_target_tags(text)
    region = infer_region(title)

    # 기회·제도 신호는 **제목에서만** 인정한다 (룰 v2.1의 "제목/본문"을 제목으로 좁힘).
    # 근거(W37 실측): forest_press 보도자료 요약에는 "모집"·"정책"·"시행" 같은 상용구가
    # 늘 들어 있어서 본문까지 보면 관리소 활동 기사가 신청/제도 섹션으로 올라온다.
    opportunity = _hits(title, OPPORTUNITY_KEYWORDS)
    institution = _institution_hits(title, relevance)

    # ③ 노이즈 사전 — 개정 v2.5 (#6): **제목에만** 적용한다. 요약의 "통합정보시스템에서
    # 접수" 같은 접수 안내 상용구로 유효 공고를 영구 배제하던 결함을 막는다.
    # 제목에 기회·제도 신호가 같이 있으면 배제가 아니라 **보류**(편집자 복구 가능).
    noise = _hits(title, NOISE_KEYWORDS)
    if noise:
        if relevance and (opportunity or institution):
            return Classification(
                VERDICT_HOLD,
                f"노이즈 의심: {noise[0]}",
                relevance + noise,
                tags,
                region,
            )
        return Classification(
            VERDICT_EXCLUDE, f"노이즈: {noise[0]}", noise, (), None
        )

    if not relevance:
        return Classification(VERDICT_EXCLUDE, "관련성 없음", (), (), None)

    # ① 대상 태그를 못 붙이면 발송본에 싣지 않는다
    if not tags:
        return Classification(VERDICT_HOLD, "대상 태그 없음", relevance, (), None)

    # 개정 v2.5 (#10): 입법예고·행정예고·고시는 `공고`보다 우선한다.
    priority = _hits(title, INSTITUTION_PRIORITY_KEYWORDS)
    if priority:
        return Classification(
            VERDICT_NOTICE,
            f"제도: {priority[0]}",
            relevance + institution,
            tags,
            region,
        )

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
# 개정 v2.5 (#8): `2026. 9. 12.`, `2026-9-12 18:00`, `2026년 9월 7일`, `26.09.30.(수)`,
# `2026/09/30` 를 모두 받는다. 고정폭 슬라이싱(text[:10])이 이들을 조용히 None으로
# 떨어뜨려 마감 지난 공고가 살아남고, 게시일이 [상시]로 오판됐다.
_DATE_RE = re.compile(
    r"^\s*(\d{2,4})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})"
)


def parse_deadline(value: Optional[str]) -> Optional[date]:
    """날짜 문자열을 date로. 파싱 불가는 None (문자열 앞쪽의 날짜만 본다)."""
    if not value or not isinstance(value, str):
        return None
    matched = _DATE_RE.match(value)
    if not matched:
        return None
    year, month, day = (int(g) for g in matched.groups())
    if year < 100:
        year += 2000
    try:
        return date(year, month, day)
    except ValueError:
        return None


def date_parse_failed(value: Optional[str]) -> bool:
    """값이 있는데 날짜로 못 읽었는가 (파싱 실패 카운트용)."""
    if value is None:
        return False
    text = str(value).strip()
    if not text:
        return False
    return parse_deadline(text) is None


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
    """자격·금액의 인용 span (판정 ⑦).

    DB에서 문자 그대로 찾을 수 있을 때만 값이 들어간다. 없으면 "원문 확인".
    필드 이름은 GLM 야간 레인(V3)이 나중에 채울 자리로 예약돼 있다.

    **마감은 여기서 뽑지 않는다** (사이클 12 #3). 마감의 근거는 DB `period_end` 뿐이고,
    텍스트에서 유도한 날짜는 정본 대조가 검증할 수 없다(DB 가 바뀌어도 못 잡는다).
    """
    text = _quote_source_text(summary, raw_data)
    return {
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
    """날짜 문자열 → date. 고정폭 슬라이싱 없이 parse_deadline에 맡긴다 (#8)."""
    if not created_at:
        return None
    return parse_deadline(str(created_at))


def _deadline_fields(
    deadline: Optional[date],
    posted_known: Optional[date],
    today: date,
) -> Dict:
    """마감 표기 (사이클 12 #3: 근거는 **DB period_end 뿐**).

    summary·raw_data·제목 텍스트에서 날짜를 유도하지 않는다. 인용 마감을 렌더하면
    ①DB 가 바뀌어도 정본 대조가 잡지 못하고(근거가 정본 밖에 있다)
    ②크롤러가 못 읽은 날짜를 다이제스트가 창작한 셈이 된다.
    마감을 모르면 "미정" 이라고 쓰고, 사람이 원문을 본다.
    """
    if deadline is not None:
        short = format_month_day(deadline)
        return {
            "days_left": (deadline - today).days,
            "label": f"D-{(deadline - today).days}",
            "deadline_short": short,
            "deadline_display": f"마감 {short}",
            "sort_bucket": 0,
        }

    # 판정 ④: "새 소식"은 게시 7일 이내임을 **알 때만** 붙인다. 모르면 "상시"(보수).
    fresh = (
        posted_known is not None
        and (today - posted_known).days <= NEW_WINDOW_DAYS
    )
    if posted_known is not None:
        display = "접수 {}부터, 마감 미정".format(format_month_day(posted_known))
    else:
        display = "마감 미정"
    return {
        "days_left": None,
        "label": LABEL_NEW if fresh else LABEL_STANDING,
        "deadline_short": "",
        "deadline_display": display,
        "sort_bucket": 1 if fresh else 2,
    }


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
    posted = posted_known or kst_date(created_at)
    quotes = extract_quotes(summary, raw_data)
    clean_title = normalize_title(title)

    item = {
        "id": item_id,
        "source": source,
        "org": source_display_name(source),
        "title": clean_title,
        "summary": normalize_title(summary or ""),
        "url": url,
        "verdict": classification.verdict,
        "reason": classification.reason,
        "matched": list(classification.matched),
        "tags": list(classification.tags),
        "region": classification.region,
        "prefix_signature": prefix_signature(title),
        "target": target_display(classification.tags, classification.region),
        "period_start": period_start or "",
        "period_end": period_end or "",
        "deadline": deadline.isoformat() if deadline else "",
        "posted": posted.isoformat() if posted else "",
        "posted_known": posted_known.isoformat() if posted_known else "",
        # 개정 v2.2 F2: 마감 없음 버킷에서 사회적경제 정체성 공고를 앞세우는 키
        "identity_priority": 0 if _hits(
            clean_title, SSE_IDENTITY_KEYWORDS
        ) else 1,
        # 개정 v2.5 (#5): 병합 대표는 사업자 신호가 있는 쪽이 먼저다
        "has_b2b": bool(_hits(clean_title, B2B_SIGNAL_KEYWORDS)),
        # 개정 v2.6 (1): 마감이 비어 있는데 연장·재공고 표지나 새 마감 인용이 있으면
        # 앞 회차와 병합하지 않는다 — 만료된 마감을 상속받아 함께 배제됐다.
        "is_renewal": bool(
            deadline is None
            and _hits(clean_title, RENEWAL_KEYWORDS)
        ),
        "merged_ids": [],
        "similar_count": 0,
    }
    item.update(_deadline_fields(deadline, posted_known, today))
    item.update(quotes)
    return item


def _refresh_deadline(item: Dict, deadline: Optional[date], today: date) -> None:
    """병합 그룹의 유효 마감으로 마감 필드를 다시 계산 (#3)."""
    item["deadline"] = deadline.isoformat() if deadline else ""
    item.update(
        _deadline_fields(
            deadline,
            parse_deadline(item.get("posted_known") or ""),
            today,
        )
    )


def _is_new_round(item: Dict) -> bool:
    """마감 없는 연장·재공고 — 새 회차로 취급해 병합하지 않는다 (개정 v2.6 (1))."""
    return bool(item.get("is_renewal"))


def _mergeable(left: Dict, right: Dict) -> bool:
    """병합해도 되는 짝인가 (개정 v2.5 #5·#11).

    판정(신청/보류/알아두세요)이 다르거나 대상 태그·지역이 다르면 다른 공고다.
    W37 실측 결함: 같은 소스·같은 마감의 `…참가자 모집`(보류)과 `…참가기업 모집`(신청)이
    Jaccard 0.67로 병합돼, 회원사가 신청할 수 있는 공고가 사라졌다.
    """
    if left["verdict"] != right["verdict"]:
        return False
    if tuple(left["tags"]) != tuple(right["tags"]):
        return False
    if (left["region"] or "") != (right["region"] or ""):
        return False
    # 사이클 8 #3: 지역 미확정(None)끼리도 접두 괄호가 다르면 별개 공고다.
    # 후미 지역 괄호(`… 모집 [경기]`)는 사이클 9 #2 에서 infer_region 이 잡으므로
    # 위 region 검사로 이미 갈라진다 — 모든 괄호 내용을 키에 넣으면
    # `…안내` / `…안내(참여기업)` 같은 **정상 중복**이 병합되지 않는다.
    if left.get("prefix_signature", "") != right.get("prefix_signature", ""):
        return False
    if _is_new_round(left) or _is_new_round(right):
        return False
    return True


def _merge_key(item: Dict) -> tuple:
    """병합 키 (사이클 10 #1): 같은 소스 · 같은 기관 · 같은 유효 마감 · 같은 제목.

    제목은 보수 정규화(공백·구두점만 제거)로 비교하며, **완전히 같아야** 한다.
    """
    return (
        item["source"],
        item["org"],
        item["deadline"],
        merge_title_key(item["title"]),
    )


def _dedup_same_source(items: List[Dict], today: date) -> List[Dict]:
    """**같은 공고가 두 번 들어온 것만** 합친다 (사이클 10 #1 — 유사도 병합 폐기).

    조건: `_merge_key` 가 완전히 같고 `_mergeable`(판정·대상·지역·접두)도 통과할 때.
    근사 중복은 합치지 않는다 — 유사도(3-gram Jaccard) 병합은 서로 다른 공고를
    지웠다(`[경기]`/`[강원]` 0.6 · `(3차)`/`(4차)` 1.0). 대신 "유사 항목 n" 으로
    표시해 편집자가 `제외` 로 판단한다.

    대표는 ①사업자 신호가 있는 쪽 ②마감이 늦은 쪽 순으로 고른다. 마감이 같아야
    병합되므로 그룹 마감 재계산(개정 v2.5 #3)은 필요 없다.
    """
    kept: List[Dict] = []
    index_by_key: Dict[tuple, int] = {}
    for item in items:
        key = _merge_key(item)
        index = index_by_key.get(key)
        if index is None or not _mergeable(kept[index], item):
            index_by_key.setdefault(key, len(kept))
            kept.append(item)
            continue
        other = kept[index]
        # 대표: 사업자 신호 → 마감 늦은 쪽
        if (item["has_b2b"], item["deadline"]) > (
            other["has_b2b"], other["deadline"]
        ):
            item["merged_ids"] = list(other["merged_ids"]) + [other["id"]]
            kept[index] = item
        else:
            other["merged_ids"].append(item["id"])
    return kept


def _mark_similar(items: List[Dict]) -> None:
    """자동 병합하지 않은 근사 중복을 "유사 항목 n" 으로만 표기 (판정 ⑥③ + 사이클 10 #1).

    같은 소스든 다른 소스든 가리지 않는다 — 유사도 병합을 폐기했으므로 같은 소스의
    근사 중복도 발송본에 나란히 실리고, 편집자는 이 표시를 보고 판단한다.
    """
    grams = [title_ngrams(item["title"]) for item in items]
    for i, item in enumerate(items):
        item["similar_count"] = sum(
            1 for j in range(len(items))
            if j != i and jaccard(grams[i], grams[j]) >= SIMILAR_JACCARD
        )


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


def kst_date(value: Optional[str]) -> Optional[date]:
    """타임스탬프 문자열을 **KST 날짜**로 (개정 v2.6 (8)).

    오프셋이 붙어 있으면 KST로 환산한다(`2026-09-06T15:30:00+00:00` → 9/7).
    naive 값은 KST로 저장된 것으로 본다(실측: `2026-09-12T09:46:29.074620`).
    ISO로 못 읽으면 앞쪽 날짜만이라도 건진다.
    """
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return parse_deadline(text)
    if parsed.tzinfo is None:
        return parsed.date()
    return parsed.astimezone(KST).date()


def window_prefilter_bounds(week_str: str) -> Tuple[str, str]:
    """SQL 1차 필터용 문자열 범위 — 인덱스를 쓰되 오프셋 경계를 놓치지 않게 넓힌다."""
    week_start, end_exclusive = week_bounds(week_str)
    slack = timedelta(days=WINDOW_PREFILTER_SLACK_DAYS)
    start = (datetime.strptime(week_start, "%Y-%m-%d") - slack).strftime(
        "%Y-%m-%d"
    )
    end = (datetime.strptime(end_exclusive, "%Y-%m-%d") + slack).strftime(
        "%Y-%m-%d"
    )
    return start, end


def week_bounds(week_str: str) -> Tuple[str, str]:
    """주간 창을 KST 날짜 문자열 범위로 (개정 v2.5 #9).

    created_at은 **KST 기준 naive ISO 문자열**로 저장된다(실측: `2026-09-12T09:46:29.074620`).
    `DATE(created_at)`는 오프셋이 붙은 값(`2026-09-07T00:30:00+09:00`)을 UTC로 해석해
    하루를 밀어버리므로, 문자열 범위 비교로 바꾼다. 인덱스(idx_ann_created)도 함께 산다.

    Returns:
        (창 시작 문자열, 창 끝 배타 문자열) — `created_at >= a AND created_at < b`
    """
    week_start, week_end = get_week_date_range(week_str)
    end_exclusive = (
        datetime.strptime(week_end, "%Y-%m-%d") + timedelta(days=1)
    ).strftime("%Y-%m-%d")
    return week_start, end_exclusive


def compose_digest_data(
    db_path: str,
    week_str: Optional[str] = None,
    forms_csv_path: Optional[Path] = None,
    warnings_out: Optional[List[str]] = None,
    exclude_urls: Optional[Set[str]] = None,
    today: Optional[date] = None,
    stats_out: Optional[Dict] = None,
) -> Dict:
    """다이제스트 구조 데이터 생성 (렌더 전 단계).

    Returns:
        {"week", "week_start", "week_end", "period_label", "sections",
         "council_notes", "member_news", "holds", "excluded", "opinions",
         "merged_ids", "date_parse_failures", "candidate_ids"}
    """
    if week_str is None:
        iso = datetime.now().isocalendar()
        week_str = f"{iso[0]}-W{iso[1]:02d}"
    if today is None:
        today = date.today()

    week_start, week_end = get_week_date_range(week_str)
    range_start, range_end = window_prefilter_bounds(week_str)
    window_first = parse_deadline(week_start)
    window_last = parse_deadline(week_end)

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
        WHERE created_at >= ? AND created_at < ?{exclude_sql}
        ORDER BY (NULLIF(period_end, '') IS NULL),
                 NULLIF(period_end, ''),
                 created_at DESC
        """,
        (range_start, range_end, *exclude_list),
    )
    rows = cursor.fetchall()
    conn.close()

    survivors: List[Dict] = []
    excluded: List[Dict] = []
    date_parse_failures = 0
    candidate_ids: List[int] = []

    for row in rows:
        source, title, summary, url = row[1], row[2], row[3], row[4]
        if exclude_urls and url in exclude_urls:
            continue
        # 개정 v2.6 (8): 정확한 창 판정은 KST 날짜로 한다 (SQL 범위는 1차 필터).
        created_kst = kst_date(row[7])
        if created_kst is None:
            continue
        if window_first and created_kst < window_first:
            continue
        if window_last and created_kst > window_last:
            continue
        candidate_ids.append(row[0])
        # 개정 v2.5 (#8): 값이 있는데 못 읽은 날짜를 센다(조용한 실패 가시화)
        for value in (row[5], row[6]):
            if date_parse_failed(value):
                date_parse_failures += 1

        classification = classify_item(title, summary, source)
        item = _build_item(row, classification, today)

        if classification.verdict == VERDICT_EXCLUDE:
            excluded.append(item)
            continue

        survivors.append(item)

    # 개정 v2.5 (#3): **중복 병합을 먼저** 하고, 병합 그룹의 유효 마감으로 경과를 판정한다.
    # 병합 전에 경과 행을 버리면 마감 없는 복제본이 [새 소식]으로 되살아난다.
    survivors = _dedup_same_source(survivors, today)

    kept: List[Dict] = []
    for item in survivors:
        if (
            item["verdict"] == VERDICT_APPLY
            and item["days_left"] is not None
            and item["days_left"] < 0
        ):
            item["verdict"] = VERDICT_EXCLUDE
            item["reason"] = "마감 경과"
            excluded.append(item)
            continue
        kept.append(item)

    # 사이클 7 (#9): 카톡 한 조각에 들어갈 수 없는 URL 은 발송본에서 내린다.
    # 오버사이즈 조각을 만드는 대신 보류로 남겨 사람이 판단하게 한다.
    fitting: List[Dict] = []
    url_too_long: List[Dict] = []
    for item in kept:
        (fitting if kakao_item_fits(item) else url_too_long).append(item)
    kept = fitting

    sections: Dict[str, List[Dict]] = {VERDICT_APPLY: [], VERDICT_NOTICE: []}
    holds: List[Dict] = []
    for item in url_too_long:
        item["verdict"] = VERDICT_HOLD
        item["reason"] = HOLD_REASON_URL_TOO_LONG
        holds.append(item)
    apply_candidates: List[Dict] = []
    notice_candidates: List[Dict] = []
    for item in kept:
        if item["verdict"] == VERDICT_APPLY:
            apply_candidates.append(item)
        elif item["verdict"] == VERDICT_NOTICE:
            notice_candidates.append(item)
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

    sections[VERDICT_APPLY] = selected

    # 개정 v2.5 (#4): 알아두세요 상한 초과분도 무기록 삭제하지 않는다.
    sorted_notice = _sort_notice(notice_candidates)
    sections[VERDICT_NOTICE] = sorted_notice[: SECTION_LIMITS[VERDICT_NOTICE]]
    cap_overflow.extend(sorted_notice[SECTION_LIMITS[VERDICT_NOTICE]:])

    for item, reason in (
        [(item, HOLD_REASON_DIVERSITY) for item in diversity_skipped]
        + [(item, HOLD_REASON_SECTION_CAP) for item in cap_overflow]
    ):
        item["verdict"] = VERDICT_HOLD
        item["reason"] = reason
        holds.append(item)

    published = sections[VERDICT_APPLY] + sections[VERDICT_NOTICE]
    _mark_similar(published)

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

    merged_ids = [
        merged_id
        for item in survivors
        for merged_id in item["merged_ids"]
    ]

    if stats_out is not None:
        stats_out["date_parse_failures"] = date_parse_failures
        stats_out["candidates"] = len(candidate_ids)

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
        "merged_ids": merged_ids,
        "candidate_ids": candidate_ids,
        "date_parse_failures": date_parse_failures,
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

    # 개정 v2.5 (#2): 제목은 링크·HTML을 렌더하지 않는다 (링크는 "원문" 필드로만).
    head = sanitize_title(item["title"])
    if item["verdict"] == VERDICT_APPLY:
        head = f"[{item['label']}] {head}"

    return f"{head} — " + " · ".join(parts)


ITEMS_JSON_SUFFIX = ".items.json"

# 항목 정본의 **렌더 규칙 버전** (사이클 13 #1).
#
# 규칙: 항목 줄·원문 줄의 렌더 방식이나 정본이 담는 근거가 바뀌면 **반드시 +1** 한다.
# checker 는 이 상수와 다른 정본을 `정본 구버전 — 재조립 필요` 로 막는다.
# 필드가 다 있어도 **옛 규칙으로 렌더된** 본문은 통과시키면 안 되기 때문이다 —
# 사이클 12 이전 정본은 summary·raw_data 에서 유도한 마감을 담고 있었고, 그 근거는
# 정본 밖에 있어 DB 대조로 잡을 수 없다(Codex 9차 HIGH #1).
#
#   1 = 사이클 13 현재 규칙
#       · 마감 표기는 DB period_end 에서만 (사이클 12 #3)
#       · 항목 줄·원문 줄을 정본에 그대로 담고 문자열 동일성 검사 (사이클 10 #2)
#       · 대상·지역·기관을 DB 에서 재계산해 대조 (사이클 13 #2)
ITEMS_SCHEMA_VERSION = 1


def items_json_path(markdown_path) -> Path:
    """발송본 마크다운 경로 → 항목 정본 파일 경로."""
    markdown_path = Path(markdown_path)
    name = markdown_path.name
    stem = name[:-3] if name.endswith(".md") else name
    return markdown_path.with_name(stem + ITEMS_JSON_SUFFIX)


def items_manifest(data: Dict, markdown_bytes: bytes = b"") -> Dict:
    """항목 **정본**(사이클 8 #1). 검증은 이 데이터에 결속된다.

    md 는 사람이 고칠 수 있는 텍스트다 — 마커 id 만으로는 "누가 이 항목을 만들었나"를
    증명하지 못했다(`<!-- item id=999 -->` 를 손으로 붙이면 빈 DB에서도 통과했다).
    그래서 compose 가 만든 항목 목록을 파일로 남기고, checker 가 md 의 블록을
    ①이 목록 ②DB 의 해당 id 와 1:1로 대조한다.
    """
    return {
        "week": data["week"],
        "schema_version": ITEMS_SCHEMA_VERSION,
        "markdown_sha256": hashlib.sha256(markdown_bytes).hexdigest(),
        "items": [
            {
                "id": item["id"],
                "url": item["url"],
                "title": sanitize_title(item["title"]),
                "section": section,
                "deadline_label": item["label"],
                # 사이클 10 #2: 렌더된 **줄 그대로**를 담는다. checker 가 md 의 해당
                # 줄과 문자열 동일성을 검사하므로, 마감·대상·기관·라벨을 손으로
                # 고치면 즉시 "재조립 필요" 가 된다(항목 줄은 편집 불가 영역).
                "line": item_line(item),
                "origin_line": f"  [원문]({item['url']})",
                # DB 기간 원문 — DB 쪽이 바뀌면 정본이 낡았다는 뜻이다.
                # period_start 도 담는다: 게시일이 바뀌면 유효 마감(마감 경과 판정)이
                # 달라지는데, period_end 만 보면 그것을 놓친다 (사이클 11 #2).
                "period_start": item.get("period_start") or "",
                "period_end": item.get("period_end") or "",
                # 사이클 13 #2: 표시 근거도 담는다. checker 가 DB 행(제목·summary·
                # source)에서 **재계산한 값**과 대조하므로, DB 가 바뀌면 정본이 낡았음이
                # 드러난다 — summary 만 고쳐 대상 태그를 바꾸던 경로가 닫힌다.
                "org": item.get("org") or "",
                "target": item.get("target") or "",
                "region": item.get("region") or "",
            }
            for section in ITEM_SECTIONS
            for item in (data["sections"].get(section) or [])
        ],
    }


def load_items_manifest(markdown_path) -> Optional[Dict]:
    """항목 정본 파일을 읽는다. 없거나 깨졌으면 None (호출자가 fail-closed)."""
    path = items_json_path(markdown_path)
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(loaded, dict) or not isinstance(loaded.get("items"), list):
        return None
    return loaded


def write_items_manifest(markdown_path, manifest: Dict) -> None:
    """항목 정본 파일을 쓴다."""
    items_json_path(markdown_path).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def refresh_manifest_binding(markdown_path) -> Optional[Dict]:
    """md 를 고친 뒤 정본 파일의 `markdown_sha256` 만 갱신한다.

    항목 목록(id·url·제목)은 **손대지 않는다** — 그것을 md 에서 다시 뽑으면
    손으로 고친 제목이 스스로 정당화되어 게이트가 비어버린다.
    """
    manifest = load_items_manifest(markdown_path)
    if manifest is None:
        return None
    try:
        manifest["markdown_sha256"] = hashlib.sha256(
            Path(markdown_path).read_bytes()
        ).hexdigest()
    except OSError:
        return manifest
    write_items_manifest(markdown_path, manifest)
    return manifest


def item_marker(item: Dict) -> str:
    """항목 블록의 구조 마커 (`<!-- item id=123 -->`)."""
    return ITEM_MARKER_TEMPLATE.format(item["id"])


def hold_comment(item: Dict) -> str:
    """보류 한 줄 (개정 v2.5 #13).

    제목의 `|`는 전각 `｜`로 바꿔 사유 칸이 밀리지 않게 하고, `--`는 주석을 닫아버리므로
    `—`로 바꾼다. 다른 도구가 원항목을 정확히 복원하도록 `id=<announcement id>`를 붙인다.
    """
    title = (
        sanitize_title(item["title"])
        .replace("|", "｜")
        .replace("--", "—")
    )
    return "<!-- 보류: {}. {} | {} | id={} -->".format(
        item["number"], title, item["reason"], item["id"]
    )


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
            lines.append(EMPTY_SECTION_LINE)
            lines.append("")
            continue
        for item in items:
            lines.append(item_marker(item))
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
            lines.append(hold_comment(item))
        lines.append("")

    return "\n".join(lines)


def chunk_plaintext(text: str, limit: int = KAKAO_CHUNK_LIMIT) -> List[str]:
    """평문을 한도 이하 조각으로 분할. 줄 경계를 지키고, URL 줄은 쪼개지 않는다."""
    chunks: List[str] = []
    current: List[str] = []
    size = 0
    for line in text.split("\n"):
        pieces = _split_line(line, limit)
        for piece in pieces:
            extra = len(piece) + (1 if current else 0)
            if size + extra > limit and current:
                chunks.append("\n".join(current))
                current = [piece]
                size = len(piece)
            else:
                current.append(piece)
                size += extra
    if current:
        chunks.append("\n".join(current))
    return chunks or [""]


def _split_line(line: str, limit: int) -> List[str]:
    """한 줄을 한도로 자른다. **URL 줄은 자르지 않는다** (개정 v2.6 (5))."""
    if len(line) <= limit:
        return [line]
    if _is_url_line(line):
        # 쪼개면 링크가 죽는다. 한도를 넘겨도 통째로 보낸다.
        return [line]
    return [line[i:i + limit] for i in range(0, len(line), limit)]


def _is_url_line(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("http://") or stripped.startswith("https://")


def kakao_item_block(item: Dict) -> str:
    """카톡 항목 덩어리 (제목 줄 + URL 줄) — 적합성 판정과 렌더가 같은 문자열을 본다."""
    return f"{item_line(item)}\n  {item['url']}"


def kakao_item_fits(item: Dict, limit: int = KAKAO_CHUNK_LIMIT) -> bool:
    """제목을 줄인 뒤의 **렌더된 조각 전체**가 한도 안에 들어가는가 (사이클 8 #4).

    예전에는 URL 길이만 봤다 — `len(url_line) + 2 <= limit` 는 경계에서
    `_shrink_item_block` 의 판정(`room <= 1`)과 어긋나, URL 4,092자 항목이
    "게시 가능"으로 통과한 뒤 카톡에서는 안내 문구로 대체됐다(원문 유실).
    판정과 렌더가 같은 함수를 보게 만들어 경계가 갈라질 수 없게 한다.
    """
    block = kakao_item_block(item)
    return len(_shrink_item_block(block, limit)) <= limit


def fit_prose_urls(text: str, limit: int = KAKAO_CHUNK_LIMIT) -> Tuple[str, List[str]]:
    """한도를 넘기는 URL 을 안내 문구로 치환 (사이클 9 #5 — 모든 렌더러가 이 함수만 쓴다).

    판정 기준은 URL 길이가 아니라 **그 줄이 한도를 넘는가**다. 예전에는
    `len(url) + 4 > limit` 로만 봐서 `[자료이름](4,092자 URL)` 처럼 링크 문구가 붙은
    줄이 한도를 넘겨도 치환되지 않았고, 조각 분할이 URL 을 잘라 링크가 죽었다
    (Codex 5차 MEDIUM #4·#6).

    **URL 은 절대 분절하지 않는다** — 줄이 한도를 넘으면 긴 링크부터 안내 문구로
    바꾸고, 그래도 남으면 그 줄은 순수 텍스트이므로 조각 분할에 맡긴다.
    (치환한 URL 목록은 check.json 경고로 남는다.)
    """
    from alert.digest import blocks as blocks_mod

    replaced: List[str] = []
    lines = []
    for line in (text or "").split("\n"):
        # 사이클 10 #5: **항목의 원문 줄은 건드리지 않는다.** 항목 URL 이 한도에 맞지
        # 않으면 compose 가 보류로 내리므로(kakao_item_fits), 여기서 치환하면 채널마다
        # 다른 URL 이 나가고(md·미리보기엔 원문, HTML·재생성 카톡엔 안내 문구) 정본
        # 대조도 깨진다.
        if blocks_mod.origin_url(line) is not None:
            lines.append(line)
            continue

        # ① 마크다운 링크부터 (긴 것 먼저). **매번 다시 스캔**한다 — 한 번 치환하면
        # 나머지 링크의 좌표가 밀려서, 옛 좌표로 자른 두 번째 링크는 실제로 남는데
        # "치환 완료" 로 기록됐다(사이클 10 #4).
        # 기록은 **출현마다** 남긴다 (사이클 13 #3: 같은 URL 이 여러 번 나오면
        # 그중 몇 번을 치환했는지가 판정에 필요하다).
        while len(line) > limit:
            links = blocks_mod.find_links(line)
            if not links:
                break
            link = max(links, key=lambda found: len(found.url))
            replaced.append(link.url)
            line = line[:link.start] + URL_TOO_LONG_NOTICE + line[link.end:]

        # ② 맨몸 URL 토큰 (`· https://…`) — 글머리·들여쓰기는 보존한다
        while len(line) > limit:
            tokens = line.split(" ")
            candidates = [
                index for index, token in enumerate(tokens)
                if _is_url_line(token)
            ]
            if not candidates:
                break
            index = max(candidates, key=lambda i: len(tokens[i]))
            # 사이클 13 #3: 기록은 **본문 URL 추출기와 같은 형태**로 남긴다.
            # 토큰에는 끝 구두점(`…/x.`)이 붙어 있을 수 있는데, 그대로 기록하면
            # 출현 대조에서 원본 URL 과 다른 문자열이 되어 정상 치환이 유실로 잡힌다.
            canonical = blocks_mod.bare_urls(tokens[index])
            replaced.append(canonical[0] if canonical else tokens[index])
            tokens[index] = URL_TOO_LONG_NOTICE
            line = " ".join(tokens)
        lines.append(line)
    return "\n".join(lines), replaced


def markdown_kakao_problems(markdown_text: str) -> List[str]:
    """카톡 렌더의 **생성 후 확인** (사이클 9 #5). 비어 있어야 정상이다.

    두 가지를 본다:
      ① 한도를 넘긴 조각 — 분할이 링크를 자르게 되는 상태
      ② **URL 분절/유실** — 본문의 링크가 어느 조각에도 온전히 실리지 않았고
         안내 문구로 치환된 것도 아닌 상태

    ②가 핵심이다. 조각 길이만 보면 "URL 을 잘라서 한도를 맞춘" 출력이 통과한다 —
    Codex 5차가 측정한 것이 정확히 그 상태였다(4,092자 산문 URL 분절 · pass).
    치환 규칙이 완전하다면 둘 다 비어 있다. 비어 있지 않으면 게이트가 막는다:
    조용히 잘린 링크를 보내는 것보다 발송을 멈추는 편이 낫다.
    """
    from alert.digest import blocks as blocks_mod

    chunks = _pack_blocks(
        kakao_blocks_from_markdown(markdown_text), KAKAO_CHUNK_LIMIT
    )
    problems = [
        f"조각 #{index} {len(chunk)}자 (한도 {KAKAO_CHUNK_LIMIT})"
        for index, chunk in enumerate(chunks)
        if len(chunk) > KAKAO_CHUNK_LIMIT
    ]
    joined = "\n".join(chunks)
    # 사이클 12 #5 + 13 #3: 판정은 **토큰 경계 + 출현 수**다 (부분 문자열 비교 금지).
    #   ① 결과물의 URL 토큰이 원본에 없으면 그것이 **분절 조각**이다
    #   ② 각 URL 의 `결과물 출현 수 + 치환 수` 가 원본 출현 수와 같아야 한다
    # 집합 비교만 하면 같은 URL 의 **일부 출현**이 잘려도 다른 출현이 그것을 가린다
    # (Codex 9차 MEDIUM #2). 치환 기록은 출현마다 남으므로 수를 맞출 수 있다.
    _, replaced = fit_prose_urls(markdown_text)
    replaced_counts = Counter(replaced)
    source_counts = Counter(blocks_mod.body_urls(markdown_text, unique=False))
    rendered_counts = Counter(blocks_mod.body_urls(joined, unique=False))

    for token in sorted(set(rendered_counts) - set(source_counts)):
        problems.append(f"URL 분절: {token[:48]}…")
    for url in sorted(source_counts):
        accounted = rendered_counts[url] + replaced_counts[url]
        if accounted == source_counts[url]:
            continue
        problems.append(
            f"URL 출현 불일치({source_counts[url]}→{accounted}): {url[:40]}…"
        )
    return problems


def _is_item_block(block: str) -> bool:
    """카톡 항목 블록인가 (제목 줄 + URL 줄, 딱 두 줄)."""
    lines = block.split("\n")
    return len(lines) == 2 and _is_url_line(lines[1])


def _shrink_item_block(block: str, limit: int) -> str:
    """항목 블록이 한도를 넘으면 **제목만** 줄이고 URL은 보존한다 (개정 v2.6 (5))."""
    if not _is_item_block(block):
        return block
    head, url_line = block.split("\n")
    room = limit - len(url_line) - 1
    if room <= 1:
        # URL 자체가 한도를 넘는다 — 자르지 않고 그대로 둔다(링크 보존이 우선).
        return block
    if len(head) > room:
        head = head[: max(1, room - 1)] + "…"
    return f"{head}\n{url_line}"


URL_TOO_LONG_NOTICE = "(URL 길이 초과 — 원문 확인)"


def _url_too_long_block(block: str, limit: int) -> str:
    """한도를 넘는 URL 줄을 표기로 대체 (오버사이즈 조각 금지 — 사이클 7 #9)."""
    head = block.split("\n")[0]
    room = limit - len(URL_TOO_LONG_NOTICE) - 3
    if len(head) > room:
        head = head[: max(1, room - 1)] + "…"
    return f"{head}\n  {URL_TOO_LONG_NOTICE}"


def _pack_blocks(blocks: Sequence[str], limit: int) -> List[str]:
    """블록(항목·머리글·메모)을 쪼개지 않고 한도 이하로 묶는다 (개정 v2.6 (5))."""
    chunks: List[str] = []
    current: List[str] = []

    def flush() -> None:
        if current:
            text = "\n".join(current).strip("\n")
            if text:
                chunks.append(text)
            current.clear()

    for block in blocks:
        block = _shrink_item_block(block, limit)
        if len(block) > limit:
            flush()
            if _is_item_block(block):
                # 사이클 7 (#9): 오버사이즈 조각은 **절대 만들지 않는다.**
                # 정상 경로에서는 compose 가 이런 항목을 보류로 내리므로 여기에
                # 오지 않는다. 손으로 고친 본문에서 오면 URL 을 표기로 대체한다.
                chunks.append(_url_too_long_block(block, limit))
            else:
                chunks.extend(chunk_plaintext(block, limit))
            continue
        candidate = len("\n".join(current + [block]))
        if current and candidate > limit:
            flush()
            if not block.strip():
                continue
        current.append(block)
    flush()
    return chunks or [""]


def kakao_blocks(data: Dict, headline: Optional[str] = None) -> List[str]:
    """카톡 평문을 블록 단위로 (항목은 `제목 줄 + URL 줄` 한 덩어리)."""
    blocks: List[str] = [
        "{} ({})\n{}{}".format(
            f"📋 협의회 주간 정책브리핑 {data['week']}",
            data["period_label"],
            KAKAO_HEADLINE_PREFIX,
            headline.strip() if headline else KAKAO_HEADLINE_PLACEHOLDER,
        ),
        "",
    ]

    for section in ITEM_SECTIONS:
        blocks.append(SECTION_HEADINGS[section])
        items = data["sections"].get(section) or []
        if not items:
            blocks.append("  (항목 없음)")
        for item in items:
            blocks.append(kakao_item_block(item))
        blocks.append("")

    if data.get("council_notes"):
        blocks.append(SECTION_HEADINGS[SECTION_COUNCIL])
        for note in data["council_notes"]:
            # 사이클 8 #4: 해설의 한도 초과 URL 은 안내 문구로 (조각 초과 금지)
            blocks.append(fit_prose_urls(f"· {note}")[0])
        blocks.append("")

    if data.get("member_news"):
        blocks.append(SECTION_HEADINGS[SECTION_MEMBER])
        for company, contents in sorted(data["member_news"].items()):
            for content in contents:
                # 사이클 9 #5: 회원사 소식의 긴 URL 도 같은 치환을 지난다
                blocks.append(fit_prose_urls(f"· {company}: {content}")[0])
        blocks.append("")

    return blocks


def render_kakao_chunks(
    data: Dict, headline: Optional[str] = None
) -> List[str]:
    """카톡 평문을 메시지 한도(4096자) 조각 리스트로 (개정 v2.5 #12 + v2.6 (5))."""
    return _pack_blocks(kakao_blocks(data, headline), KAKAO_CHUNK_LIMIT)


def kakao_file_text(data: Dict, headline: Optional[str] = None) -> str:
    """`.kakao.txt` 파일 내용 — 조각 사이를 `---8<---`로 끊는다."""
    separator = f"\n{KAKAO_CHUNK_SEPARATOR}\n"
    return separator.join(render_kakao_chunks(data, headline)) + "\n"


def _kakao_prose_line(text: str) -> str:
    """마크다운 산문 줄을 카톡 평문으로 (강조·글머리 기호만 평탄화)."""
    line = text.strip()
    if line == EMPTY_SECTION_LINE:
        return KAKAO_EMPTY_SECTION_LINE
    line = line.replace(MARKER, KAKAO_HEADLINE_PLACEHOLDER)
    line = _BOLD_RE.sub(r"\1", line)
    if line.startswith("- "):
        line = "· " + line[2:]
    return line


def kakao_blocks_from_markdown(markdown_text: str) -> List[str]:
    """**발송본 마크다운에서** 카톡 평문 블록을 만든다 (사이클 6 #5).

    `.kakao.txt` 를 md 와 따로 손보면 둘이 갈라진다 — 재검토가 md 에서 죽은 링크를
    지워도 카톡에는 그대로 남았다(Codex 신규 #8). 그래서 갱신 경로는 하나다:
    **md → 이 렌더러 → .kakao.txt**. 항목 판정은 alert.digest.blocks 가 정본이므로
    카톡의 항목 덩어리와 게이트의 항목 계수가 갈라질 수 없다.
    """
    from alert.digest import blocks as blocks_mod

    head: List[str] = []
    body: List[str] = []
    seen_section = False

    markdown_text, _ = fit_prose_urls(markdown_text)
    for block in blocks_mod.parse_blocks(markdown_text):
        kind = block["kind"]
        if kind == "comment":
            # 레인 표기·보류 목록은 발송 대상이 아니다
            continue
        if kind == "section":
            if body:
                body.append("")
            body.append(block["name"])
            seen_section = True
            continue
        if kind == "item":
            # 항목은 제목 줄 + URL 줄이 **한 덩어리**다 (쪼개지 않는다)
            body.append(f"{block['title']}\n  {block['url']}")
            continue
        for raw in block["lines"]:
            text = raw.strip()
            if not text:
                continue
            if text.startswith("# "):
                text = text[2:]
            (body if seen_section else head).append(_kakao_prose_line(text))

    blocks: List[str] = []
    if head:
        blocks.append("\n".join(head))
        blocks.append("")
    blocks.extend(body)
    blocks.append("")
    return blocks


def kakao_file_text_from_markdown(markdown_text: str) -> str:
    """`.kakao.txt` 내용을 발송본 마크다운에서 재생성 (사이클 6 #5)."""
    separator = f"\n{KAKAO_CHUNK_SEPARATOR}\n"
    chunks = _pack_blocks(
        kakao_blocks_from_markdown(markdown_text), KAKAO_CHUNK_LIMIT
    )
    return separator.join(chunks) + "\n"


def render_kakao(data: Dict, headline: Optional[str] = None) -> str:
    """카톡 평문 렌더 (같은 틀, 마크다운 장식·보류 주석 없음)."""
    return "\n".join(kakao_blocks(data, headline)).rstrip() + "\n"


def compose_digest(
    db_path: str,
    week_str: Optional[str] = None,
    output_path: Optional[Path] = None,
    forms_csv_path: Optional[Path] = None,
    warnings_out: Optional[List[str]] = None,
    exclude_urls: Optional[Set[str]] = None,
    today: Optional[date] = None,
    stats_out: Optional[Dict] = None,
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
        stats_out=stats_out,
    )

    markdown = render_markdown(data)

    if output_path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(markdown, encoding="utf-8")
        # 사이클 8 #1: 항목 정본 파일. 해시는 **방금 쓴 파일 바이트**에서 뽑는다
        # (텍스트를 다시 인코딩하면 개행 변환으로 어긋날 수 있다).
        write_items_manifest(
            output_path, items_manifest(data, output_path.read_bytes())
        )
        # 사이클 12 #4: 카톡 파일은 **md 를 읽는 렌더러 하나만** 쓴다.
        # data 경로와 md 경로가 따로 있으면 회원사명 접두처럼 줄 모양이 다른 곳에서
        # 길이 판정이 갈라져, 최초 카톡본과 재생성본·검사 대상이 달라졌다.
        output_path.with_name(
            output_path.name.replace(".md", "") + ".kakao.txt"
        ).write_text(
            kakao_file_text_from_markdown(output_path.read_text(encoding="utf-8")),
            encoding="utf-8",
        )

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
