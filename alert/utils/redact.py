"""로그·예외 문자열에서 봇 토큰을 가린다 (계약 W10, 크리틱 #4).

왜 공용 모듈인가: 토큰은 예외 메시지 안의 **URL**로 샌다
(`https://api.telegram.org/bot123:SECRET/sendMessage`). 유출 지점이 발신 코드가
아니라 로깅 코드이므로, 모든 로깅 경로가 같은 치환을 통과해야 한다.
"""

import re

REDACTED = "bot<redacted>"

# `bot<digits>:<token>` — 텔레그램 봇 토큰의 URL 형태(실제 유출 경로).
_BOT_TOKEN_RE = re.compile(r"bot\d+:[A-Za-z0-9_\-]+")
# URL 밖에 맨몸으로 나온 토큰 (사이클3 #8). 텔레그램 토큰은 봇 id 8~10자리 +
# 시크릿 35자 안팎이다. 경계는 `\b` 대신 문자 클래스 lookaround 로 잡는다 —
# `\b` 는 한글 바로 뒤의 숫자에서 성립하지 않아 `토큰123456789:<35자>` 를 놓쳤다.
# 길이 하한(8~10자리 · 30자)은 `123456:abcdefghijklmnopqrst` 같은 정상 식별자와
# `12:30:45` 같은 시각 문자열을 건드리지 않기 위한 것이다.
_BARE_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_\-])\d{8,10}:[A-Za-z0-9_\-]{30,}(?![A-Za-z0-9_\-])"
)


def redact(text, secrets=()):
    """토큰 패턴(그리고 알려진 시크릿 값)을 가린 문자열.

    **오류 문자열·로그·거부 메시지에만** 쓴다 (사이클3 #8) — 정상 미리보기 본문에
    걸면 멀쩡한 문자열이 변형될 수 있다.

    Args:
        text: 원본 (예외 객체도 받는다 — str()로 변환한다)
        secrets: 값을 아는 시크릿들 (URL 밖에 맨몸으로 나온 토큰까지 가린다)

    Returns:
        치환된 문자열
    """
    out = "" if text is None else str(text)
    out = _BOT_TOKEN_RE.sub(REDACTED, out)
    out = _BARE_TOKEN_RE.sub("<redacted>", out)
    for secret in secrets or ():
        if secret and len(str(secret)) >= 8:
            out = out.replace(str(secret), "<redacted>")
    return out
