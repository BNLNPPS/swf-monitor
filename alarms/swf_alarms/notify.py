"""Notification channels. Email (SES) first; Mattermost etc. can slot in here.

Channels take an Alarm + EmailConfig (+ future per-channel config) and return
True on successful send, False on any failure. Failures must be logged but
must not raise — a stuck channel should not block other channels or future
alarms.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import boto3
from botocore.exceptions import BotoCoreError, ClientError


log = logging.getLogger(__name__)


@dataclass
class Alarm:
    alarm_name: str
    dedupe_key: str
    subject: str
    body: str
    recipients: list[str]
    data: dict


def send_email_ses(alarm: Alarm, *, region: str, from_addr: str) -> bool:
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
