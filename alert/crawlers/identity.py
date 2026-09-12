"""식별자 보조 - 텍스트 정리와 **레거시 판정**만 남는다 (16차 게이트).

13~15차에 걸쳐 행 식별자를 재설계했다(URL 정규화 해시, 소스별 선언 필드).
사이클마다 새 경합이 나왔다:

- URL 정규화를 넓게 잡으면 다른 페이지가 합쳐지고, 좁게 잡으면 같은 공고가
  갈렸다
- 필드 키와 URL 키를 함께 두니 같은 공고가 두 키로 갈려 한쪽에 철회된
  기간이 남았고, URL 우선으로 통일하니 **URL 이 번호만 담은 API 공고**
  (G2B 차수, Bizinfo ``detailUrl="#"``)가 합쳐졌다

식별자는 **크롤러가 만든 ``source_id``** 로 되돌린다. 저장·조회·갱신은
전부 ``(source, source_id)`` 하나를 쓴다. 남은 것은 두 가지다:

1. ``clean_text`` - ``str(None)`` 이 만드는 문자열 ``"None"`` 차단
   (13차에서 Bizinfo 의 서로 다른 공고가 같은 URL 을 갖던 자리)
2. ``is_legacy_source_id`` - seis 가 **번호만** 쓰던 시절의 행 판정.
   seis 는 공고 종류를 접두로 붙이므로(``fnc:``/``dsgn:``/``epsd:<연도>:``/
   ``itgrd:``/``path:``), ``:`` 가 없는 seis 행이 옛 규칙의 행이다.
"""
from typing import Any

__all__ = ["clean_text", "is_legacy_source_id"]

# 종류 접두를 쓰는 소스. 다른 소스의 ``source_id`` 형식은 건드리지 않는다.
PREFIXED_SOURCES = ("seis",)


def clean_text(value: Any) -> str:
    """``None`` 이 문자열 ``"None"`` 으로 새는 것을 막는다.

    ``str(item.get("detailUrl"))`` 은 값이 없을 때 ``"None"`` 을 만든다.
    그 문자열이 URL 자리에 들어가면 **서로 다른 공고가 같은 URL** 을 갖는다
    (13차 게이트 HIGH).
    """
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in ("none", "null") else text


def is_legacy_source_id(source: str, source_id: Any) -> bool:
    """옛 규칙(번호만)으로 저장된 행인가.

    12차에서 seis 의 ``source_id`` 에 공고 종류 접두를 넣었다 - 같은 번호를
    쓰는 다른 종류의 공고가 한 행을 덮어쓰던 결함 때문이다. 그 전에 저장된
    행은 접두가 없으므로 새 수집과 만나지 않는다. 추측해서 이관하지 않고
    **표시만** 한다 (15차 게이트).

    Args:
        source: 소스 이름
        source_id: 저장된 식별자

    Returns:
        옛 규칙의 행이면 True
    """
    if (source or "").strip() not in PREFIXED_SOURCES:
        return False
    return ":" not in clean_text(source_id)
