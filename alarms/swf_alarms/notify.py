"""Notification channels. Email first; Mattermost etc. can slot in here.

Channels take an Alarm + channel config and return True on successful send,
False on any failure. Failures must be logged but must not raise - a stuck
channel should not block other channels or future
alarms.
"""
from __future__ import annotations

import logging
import os
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage


log = logging.getLogger(__name__)


@dataclass
class Alarm:
    alarm_name: str
    dedupe_key: str
    subject: str
    body: str
    recipients: list[str]
    data: dict


def send_email(
    alarm: Alarm,
    *,
    provider: str,
    from_addr: str,
    region: str = "",
    smtp_host: str = "",
    smtp_port: int = 25,
    smtp_timeout: int = 20,
) -> bool:
    provider = provider.lower()
    if provider == "smtp":
        return send_email_smtp(
            alarm,
            host=smtp_host,
            port=smtp_port,
            timeout=smtp_timeout,
            from_addr=from_addr,
        )
    if provider == "ses":
        return send_email_ses(alarm, region=region, from_addr=from_addr)
    log.error("Unknown email provider %r for alarm %s", provider, alarm.alarm_name)
    return False


def send_email_smtp(
    alarm: Alarm,
    *,
    host: str,
    port: int,
    timeout: int,
    from_addr: str,
) -> bool:
    if not host:
        log.error("SMTP send FAILED for %s: missing smtp_host", alarm.dedupe_key)
        return False
    if not alarm.recipients:
        log.error("SMTP send FAILED for %s: no recipients", alarm.dedupe_key)
        return False
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = ", ".join(alarm.recipients)
    msg["Subject"] = alarm.subject
    msg.set_content(alarm.body)
    try:
        with smtplib.SMTP(host=host, port=port, timeout=timeout) as smtp:
            smtp.send_message(msg)
        log.info("SMTP send OK: server=%s:%s to=%s", host, port,
                 ",".join(alarm.recipients))
        return True
    except (OSError, smtplib.SMTPException) as e:
        log.error("SMTP send FAILED for %s to %s via %s:%s: %s",
                  alarm.dedupe_key, alarm.recipients, host, port, e)
        return False


def send_email_ses(alarm: Alarm, *, region: str, from_addr: str) -> bool:
    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError as e:
        log.error("SES send FAILED for %s: boto3/botocore unavailable: %s",
                  alarm.dedupe_key, e)
        return False

    kwargs = {'region_name': region}
    access_key = os.getenv('AWS_ACCESS_KEY_ID') or os.getenv('AWS_ACCESS_KEY')
    secret_key = os.getenv('AWS_SECRET_ACCESS_KEY') or os.getenv('AWS_SECRET_KEY')
    if access_key and secret_key:
        kwargs['aws_access_key_id'] = access_key
        kwargs['aws_secret_access_key'] = secret_key
    ses = boto3.client("ses", **kwargs)
    try:
        resp = ses.send_email(
            Source=from_addr,
            Destination={"ToAddresses": alarm.recipients},
            Message={
                "Subject": {"Data": alarm.subject, "Charset": "UTF-8"},
                "Body": {"Text": {"Data": alarm.body, "Charset": "UTF-8"}},
            },
        )
        log.info("SES send OK: MessageId=%s to=%s", resp.get("MessageId"),
                 ",".join(alarm.recipients))
        return True
    except (BotoCoreError, ClientError) as e:
        log.error("SES send FAILED for %s to %s: %s",
                  alarm.dedupe_key, alarm.recipients, e)
        return False
