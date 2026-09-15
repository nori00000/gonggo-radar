# alert/certs — 사이트별 보조 인증서 번들 (P2-X)

여기 있는 PEM 은 **서버가 보내지 않는 중간 인증서**다. 신뢰 루트가 아니다.

## 왜 있나

감사 V §4: `ggeea.or.kr` 은 TLS 핸드셰이크에서 **자기 인증서 1장만** 보낸다
(`openssl s_client -showcerts` 실측 — `depth=0 CN=ggeea.or.kr` 뒤로 아무것도
없다). 브라우저는 AIA fetch 로 스스로 메우지만 Python `ssl` 은 메우지 않으므로
`CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate` 로 끝난다.
**서버 측 결함이고 정공법은 서버 수정 요청이다** — 이 파일은 그때까지의 우회다.

## 규율

- 이 번들은 기본 신뢰 저장소를 **대체하지 않고 더한다**
  (`ssl.create_default_context()` → `load_verify_locations(cafile=...)`).
  대체하면 루트 저장소가 이 레포 커밋 시점에 얼어붙는다.
- `verify=False` 는 어디에도 없다. 검증은 그대로 켜져 있고, 호스트명 확인도 켜져
  있다 — 없던 사슬 한 칸을 채워 줄 뿐이다.
- 서버가 인증서를 갱신하면서 **발급 CA 가 바뀌면 이 파일은 무용지물이 된다.**
  그때는 다시 `접속 실패` 로 잡히고(감시 잡 H1), 사람이 갱신하거나 지운다.
  조용히 통과하는 길은 없다.

## 목록

| 파일 | 소스 | 주체 | 발급자 | 만료 | SHA-256 |
|---|---|---|---|---|---|
| `ggeea-intermediate.pem` | `ggeea` | Sectigo Public Server Authentication CA DV R36 | Sectigo Public Server Authentication Root R46 | 2036-03-21 | `8C:54:C3:34:B6:6B:A4:E4:26:77:2A:F4:A3:F9:13:6C:19:A1:AE:C7:29:FD:B2:8C:53:5C:07:A5:A4:EF:22:E0` |

출처: 잎 인증서의 AIA `CA Issuers` —
`http://crt.sectigo.com/SectigoPublicServerAuthenticationCADVR36.crt` (2026-09-15 수집).
