"""사이트별 TLS 정책 — **검증을 끄지 않고** 고장난 서버에 닿는다 (P2-X).

감사 V §4 가 오류 두 종을 서로 다른 원인으로 갈라 놓았다. 둘 다 `verify=False`
없이 고칠 수 있고, 이 모듈이 그 두 방법만 제공한다.

1. ``semas`` — **클라이언트 측 정책**이다. 서버는 멀쩡하다:
   ``openssl s_client`` 는 TLSv1.2/AES256-GCM-SHA384 로 붙고 ``Verify return
   code: 0 (ok)`` 다. Python 이 쓰는 OpenSSL 3 의 기본 ``SECLEVEL=2`` 가 서버의
   키·서명 강도를 거부한다. 보안 수준 한 칸을 내리되 **인증서 검증과 호스트명
   확인은 그대로** 둔다(``CERT_REQUIRED`` + ``check_hostname``).

2. ``ggeea`` — **서버 측 결함**이다. 중간 인증서를 보내지 않아
   ``unable to get local issuer certificate`` 가 난다. 기본 신뢰 저장소를
   **대체하지 않고** 그 한 칸만 더한다 — 대체하면 루트 저장소가 커밋 시점에
   얼어붙는다.

두 손잡이 모두 **그 크롤러에만** 적용된다. 세션 단위 어댑터로 붙이므로 다른
소스의 TLS 는 한 글자도 바뀌지 않는다.

여기에 ``verify=False``·``CERT_NONE``·``check_hostname=False`` 는 없다.
그 셋이 생기면 이 모듈의 이유가 사라진다 — 테스트가 그것을 고정한다.
"""

from __future__ import annotations

import ssl
from pathlib import Path
from typing import Optional

import requests
from requests.adapters import HTTPAdapter

# 사이트별 보조 인증서 번들 디렉터리 (alert/certs/README.md 참조)
CERTS_DIR = Path(__file__).resolve().parent.parent / "certs"

# OpenSSL 3 기본값은 SECLEVEL=2 다. 1 로 내리면 예전 키·서명 강도를 받아 준다 —
# 암호 스위트 강도만 내리는 것이고 신원 검증은 건드리지 않는다.
LEGACY_CIPHERS = "DEFAULT@SECLEVEL=1"


class CertsNotFound(RuntimeError):
    """선언한 번들이 없다 — 조용히 검증을 낮추지 않고 실패한다 (fail-closed)."""


def build_context(legacy_security_level: bool = False,
                  extra_ca_file: Optional[Path] = None) -> ssl.SSLContext:
    """검증이 켜진 컨텍스트를 만든다.

    Args:
        legacy_security_level: True 면 ``SECLEVEL=1``. 암호 스위트 강도만 내린다.
        extra_ca_file: 기본 저장소에 **더할** PEM (대체가 아니다).

    Raises:
        CertsNotFound: ``extra_ca_file`` 이 없을 때.
    """
    context = ssl.create_default_context()
    # 방어: create_default_context 의 기본값이지만, 이 두 줄이 이 모듈의 계약이다.
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = True
    if legacy_security_level:
        context.set_ciphers(LEGACY_CIPHERS)
    if extra_ca_file is not None:
        path = Path(extra_ca_file)
        if not path.exists():
            raise CertsNotFound(f"보조 인증서 번들이 없습니다: {path}")
        context.load_verify_locations(cafile=str(path))
    return context


class _ContextAdapter(HTTPAdapter):
    """정해진 ``SSLContext`` 로만 붙는 어댑터."""

    def __init__(self, context: ssl.SSLContext, *args, **kwargs):
        self._ssl_context = context
        super().__init__(*args, **kwargs)

    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = self._ssl_context
        return super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, *args, **kwargs):
        kwargs["ssl_context"] = self._ssl_context
        return super().proxy_manager_for(*args, **kwargs)


def apply_tls_policy(session: requests.Session,
                     legacy_security_level: bool = False,
                     extra_ca_file: Optional[Path] = None) -> ssl.SSLContext:
    """세션의 https 경로에 사이트별 TLS 정책을 붙이고 컨텍스트를 돌려준다."""
    context = build_context(legacy_security_level, extra_ca_file)
    session.mount("https://", _ContextAdapter(context))
    return context
