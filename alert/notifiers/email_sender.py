"""이메일 알림 전송 모듈 -- SMTP를 통한 다이제스트 이메일."""

import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import List

from alert.config import get_config
from alert.models import AnalyzedAnnouncement
from alert.utils.logger import setup_logger


class EmailNotifier:
    """SMTP를 사용한 이메일 다이제스트 발송."""

    def __init__(self) -> None:
        """환경변수와 설정에서 이메일 구성을 로드."""
        self.logger = setup_logger("email_notifier")

        cfg = get_config()
        self.email_config = cfg.notifier.email

        # 환경변수에서 인증 정보 로드
        self.sender = os.getenv("EMAIL_SENDER", self.email_config.sender)
        self.password = os.getenv("EMAIL_PASSWORD", self.email_config.password)

        recipients_raw = os.getenv("EMAIL_RECIPIENTS", "")
        if recipients_raw:
            self.recipients = [r.strip() for r in recipients_raw.split(",") if r.strip()]
        else:
            self.recipients = self.email_config.recipients

        if not self.sender or not self.password:
            self.logger.warning(
                "EMAIL_SENDER 또는 EMAIL_PASSWORD가 설정되지 않았습니다. "
                "이메일 알림이 비활성화됩니다."
            )

        if not self.recipients:
            self.logger.warning("EMAIL_RECIPIENTS가 설정되지 않았습니다.")

    def _create_html_table(self, announcements: List[AnalyzedAnnouncement]) -> str:
        """공고 목록을 HTML 테이블로 변환.

        Args:
            announcements: 분석된 공고 리스트

        Returns:
            HTML 테이블 문자열
        """
        rows = []
        for ann in announcements:
            # 기간 포맷
            period = ""
            if ann.period_start or ann.period_end:
                start = ann.period_start or "미정"
                end = ann.period_end or "미정"
                period = f"{start} ~ {end}"
            else:
                period = "미정"

            # 점수에 따른 색상 지정
            score_pct = ann.relevance_score * 100
            if ann.relevance_score >= 0.7:
                score_color = "#28a745"  # green
            elif ann.relevance_score >= 0.5:
                score_color = "#ffc107"  # yellow
            elif ann.relevance_score >= 0.3:
                score_color = "#fd7e14"  # orange
            else:
                score_color = "#6c757d"  # gray

            row = f"""
            <tr>
                <td><a href="{ann.url}" style="color: #007bff; text-decoration: none;">{ann.title}</a></td>
                <td>{ann.author or "미상"}</td>
                <td>{ann.target or "미상"}</td>
                <td>{period}</td>
                <td style="color: {score_color}; font-weight: bold;">{score_pct:.0f}%</td>
            </tr>
            """
            rows.append(row)

        table_rows = "\n".join(rows)

        html_table = f"""
        <table style="width: 100%; border-collapse: collapse; font-family: Arial, sans-serif;">
            <thead>
                <tr style="background-color: #f8f9fa; border-bottom: 2px solid #dee2e6;">
                    <th style="padding: 12px; text-align: left; border: 1px solid #dee2e6;">공고명</th>
                    <th style="padding: 12px; text-align: left; border: 1px solid #dee2e6;">주관</th>
                    <th style="padding: 12px; text-align: left; border: 1px solid #dee2e6;">대상</th>
                    <th style="padding: 12px; text-align: left; border: 1px solid #dee2e6;">기간</th>
                    <th style="padding: 12px; text-align: left; border: 1px solid #dee2e6;">관련도</th>
                </tr>
            </thead>
            <tbody>
                {table_rows}
            </tbody>
        </table>
        """
        return html_table

    def send_digest(self, announcements: List[AnalyzedAnnouncement], date_str: str) -> bool:
        """공고 다이제스트를 HTML 이메일로 전송.

        Args:
            announcements: 분석된 공고 리스트
            date_str: 날짜 문자열 (예: "2026-03-24")

        Returns:
            전송 성공 여부
        """
        if not self.sender or not self.password:
            self.logger.warning("이메일 인증 정보가 없어 전송을 건너뜁니다.")
            return False

        if not self.recipients:
            self.logger.warning("수신자가 없어 전송을 건너뜁니다.")
            return False

        # 이메일 메시지 구성
        msg = MIMEMultipart("alternative")
        msg["From"] = self.sender
        msg["To"] = ", ".join(self.recipients)
        msg["Subject"] = f"[농업공고알림] {date_str} 관련 공고 {len(announcements)}건"

        # HTML 본문 생성
        html_table = self._create_html_table(announcements)

        html_body = f"""
        <html>
        <head>
            <meta charset="UTF-8">
        </head>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
            <h2 style="color: #28a745;">🌾 농업공고알림 - {date_str}</h2>
            <p>오늘 수집된 관련 공고는 총 <strong>{len(announcements)}건</strong>입니다.</p>
            {html_table}
            <hr style="margin-top: 30px; border: none; border-top: 1px solid #dee2e6;">
            <p style="font-size: 12px; color: #6c757d;">
                이 메일은 농업공고알림봇에서 자동 발송되었습니다.<br>
                문의사항은 관리자에게 연락해주세요.
            </p>
        </body>
        </html>
        """

        # HTML 파트 추가
        html_part = MIMEText(html_body, "html", "utf-8")
        msg.attach(html_part)

        # SMTP 전송 (공용 경로)
        if not self._send_via_smtp(msg):
            return False

        self.logger.info(
            f"이메일 다이제스트 전송 성공: {len(announcements)}건, "
            f"수신자 {len(self.recipients)}명"
        )
        return True

    def _send_via_smtp(self, msg: MIMEMultipart) -> bool:
        """구성된 메시지를 SMTP로 전송 (connect/starttls/login/send).

        Args:
            msg: 전송할 MIME 메시지

        Returns:
            전송 성공 여부
        """
        try:
            with smtplib.SMTP(
                self.email_config.smtp_server,
                self.email_config.smtp_port,
                timeout=30
            ) as server:
                if self.email_config.use_tls:
                    server.starttls()

                server.login(self.sender, self.password)
                server.send_message(msg)

            return True

        except smtplib.SMTPException as exc:
            self.logger.error(f"SMTP 오류로 이메일 전송 실패: {exc}")
            return False
        except Exception as exc:
            self.logger.error(f"이메일 전송 중 예외 발생: {exc}")
            return False

    def send_html(self, subject: str, html_body: str, recipients: List[str]) -> bool:
        """임의의 HTML 본문을 지정 수신자에게 전송 (기존 SMTP 경로 재사용).

        Args:
            subject: 메일 제목
            html_body: HTML 본문
            recipients: 수신자 이메일 목록

        Returns:
            전송 성공 여부
        """
        if not self.sender or not self.password:
            self.logger.error("이메일 인증 정보가 설정되지 않았습니다.")
            return False

        if not recipients:
            self.logger.error("수신자가 설정되지 않았습니다.")
            return False

        msg = MIMEMultipart("alternative")
        msg["From"] = self.sender
        msg["To"] = ", ".join(recipients)
        msg["Subject"] = subject
        msg.attach(MIMEText(html_body, "html", "utf-8"))

        if not self._send_via_smtp(msg):
            return False

        self.logger.info(f"HTML 메일 전송 성공: 수신자 {len(recipients)}명")
        return True

    def send_test(self) -> bool:
        """테스트 이메일 전송으로 설정 확인.

        Returns:
            전송 성공 여부
        """
        if not self.sender or not self.password:
            self.logger.error("이메일 인증 정보가 설정되지 않았습니다.")
            return False

        if not self.recipients:
            self.logger.error("수신자가 설정되지 않았습니다.")
            return False

        # 테스트 메시지 구성
        msg = MIMEMultipart("alternative")
        msg["From"] = self.sender
        msg["To"] = ", ".join(self.recipients)
        msg["Subject"] = "[농업공고알림] 테스트 이메일"

        html_body = """
        <html>
        <head>
            <meta charset="UTF-8">
        </head>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
            <h2 style="color: #28a745;">🌾 농업공고알림 테스트</h2>
            <p>이메일 설정이 정상적으로 작동하고 있습니다.</p>
            <p><strong>발신자:</strong> {sender}</p>
            <p><strong>SMTP 서버:</strong> {smtp_server}:{smtp_port}</p>
            <p><strong>TLS:</strong> {use_tls}</p>
        </body>
        </html>
        """.format(
            sender=self.sender,
            smtp_server=self.email_config.smtp_server,
            smtp_port=self.email_config.smtp_port,
            use_tls="활성화" if self.email_config.use_tls else "비활성화"
        )

        html_part = MIMEText(html_body, "html", "utf-8")
        msg.attach(html_part)

        # SMTP 전송
        try:
            with smtplib.SMTP(
                self.email_config.smtp_server,
                self.email_config.smtp_port,
                timeout=30
            ) as server:
                if self.email_config.use_tls:
                    server.starttls()

                server.login(self.sender, self.password)
                server.send_message(msg)

            self.logger.info(f"테스트 이메일 전송 성공 (수신자: {', '.join(self.recipients)})")
            return True

        except smtplib.SMTPException as exc:
            self.logger.error(f"SMTP 오류로 테스트 이메일 전송 실패: {exc}")
            return False
        except Exception as exc:
            self.logger.error(f"테스트 이메일 전송 중 예외 발생: {exc}")
            return False
