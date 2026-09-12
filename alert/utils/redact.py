"""로그·예외 문자열에서 봇 토큰을 가린다 (계약 W10, 크리틱 #4).

왜 공용 모듈인가: 토큰은 예외 메시지 안의 **URL**로 샌다
(`https://api.telegram.org/bot123:SECRET/sendMessage`). 유출 지점이 발신 코드가
아니라 로깅 코드이므로, 모든 로깅 경로가 같은 치환을 통과해야 한다.
"""

import re

REDACTED = "bot<redacted>"

# `bot<digits>:<token>` — 텔레그램 봇 토큰의 URL 형태. 토큰 본문은 base64url 문자.
_BOT_TOKEN_RE = re.compile(r"bot\d+:[A-Za-z0-9_\-]+")


def redact(text, secrets=()):
    """토큰 패턴(그리고 알려진 시크릿 값)을 가린 문자열.

    Args:
        text: 원본 (예외 객체도 받는다 — str()로 변환한다)
        secrets: 값을 아는 시크릿들 (URL 밖에 맨몸으로 나온 토큰까지 가린다)

    Returns:
        치환된 문자열
    """
    out = "" if text is None else str(text)
    out = _BOT_TOKEN_RE.sub(REDACTED, out)
    for secret in secrets or ():
        if secret and len(str(secret)) >= 8:
            out = out.replace(str(secret), "<redacted>")
    return out
