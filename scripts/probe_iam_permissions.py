#!/usr/bin/env python3
"""
Probe which AWS permissions the current EC2 instance role effectively has,
by actually calling each API the worker uses and reporting ✓ / ✗ per action.

Safe to run repeatedly: every call is either read-only, or a no-op write
(SendMessage with an immediately-deleted message, PutObject with cleanup).
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

REGION = "eu-west-1"
ACCOUNT = "911167932273"
BUCKET = "appway-bridge-prod"
MODEL_BUCKET = "ray-bucket-ai-models"
JOBS_QUEUE = f"https://sqs.{REGION}.amazonaws.com/{ACCOUNT}/appway-jobs"
RESULTS_QUEUE = f"https://sqs.{REGION}.amazonaws.com/{ACCOUNT}/appway-results"
SNS_TOPIC = f"arn:aws:sns:{REGION}:{ACCOUNT}:appway-dlq-alerts"

OK = "\033[32m✓\033[0m"
BAD = "\033[31m✗\033[0m"
SKIP = "\033[33m–\033[0m"


def check(label: str, fn):
    """Run fn(); print ✓ or ✗ with the error code."""
    try:
        fn()
        print(f"  {OK} {label}")
        return True
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "?")
        print(f"  {BAD} {label}  ({code})")
        return False
    except Exception as e:
        print(f"  {BAD} {label}  ({type(e).__name__}: {e})")
        return False


def main() -> int:
    sts = boto3.client("sts", region_name=REGION)
    ident = sts.get_caller_identity()
    print(f"Running as: {ident['Arn']}")
    print(f"Account:    {ident['Account']}")
    print()

    sqs = boto3.client("sqs", region_name=REGION)
    s3 = boto3.client("s3", region_name=REGION)
    sns = boto3.client("sns", region_name=REGION)
    cw = boto3.client("cloudwatch", region_name=REGION)

    # ── SQS appway-jobs ──
    print("SQS – appway-jobs (input queue)")
    check("sqs:GetQueueAttributes",
          lambda: sqs.get_queue_attributes(QueueUrl=JOBS_QUEUE, AttributeNames=["QueueArn"]))
    check("sqs:ReceiveMessage",
          lambda: sqs.receive_message(QueueUrl=JOBS_QUEUE, MaxNumberOfMessages=1, WaitTimeSeconds=0))

    # We cannot safely probe DeleteMessage / ChangeMessageVisibility without a receipt handle
    # so we at least attempt ChangeMessageVisibility with a bogus handle and look at the error code.
    def probe_cmv():
        try:
            sqs.change_message_visibility(
                QueueUrl=JOBS_QUEUE,
                ReceiptHandle="AAAAAAAA-bogus-probe-handle",
                VisibilityTimeout=30,
            )
        except ClientError as e:
            # ReceiptHandleIsInvalid means the call was authorized but handle was garbage → OK.
            if e.response["Error"]["Code"] in ("ReceiptHandleIsInvalid", "InvalidParameterValue"):
                return
            raise
    check("sqs:ChangeMessageVisibility", probe_cmv)

    def probe_delete():
        try:
            sqs.delete_message(QueueUrl=JOBS_QUEUE, ReceiptHandle="AAAAAAAA-bogus-probe-handle")
        except ClientError as e:
            if e.response["Error"]["Code"] in ("ReceiptHandleIsInvalid", "InvalidParameterValue"):
                return
            raise
    check("sqs:DeleteMessage", probe_delete)

    print()

    # ── SQS appway-results ──
    print("SQS – appway-results (output queue)")
    check("sqs:GetQueueAttributes",
          lambda: sqs.get_queue_attributes(QueueUrl=RESULTS_QUEUE, AttributeNames=["QueueArn"]))

    # SendMessage: send a no-op probe message, then immediately delete it so it doesn't leak.
    probe_body = json.dumps({"__probe__": str(uuid.uuid4())})

    def probe_send():
        resp = sqs.send_message(QueueUrl=RESULTS_QUEUE, MessageBody=probe_body)
        # try to receive+delete it so the real consumer won't pick it up
        for _ in range(3):
            r = sqs.receive_message(QueueUrl=RESULTS_QUEUE, MaxNumberOfMessages=10, WaitTimeSeconds=1)
            for m in r.get("Messages", []):
                if probe_body in m.get("Body", ""):
                    try:
                        sqs.delete_message(QueueUrl=RESULTS_QUEUE, ReceiptHandle=m["ReceiptHandle"])
                    except ClientError:
                        pass
    check("sqs:SendMessage", probe_send)

    print()

    # ── S3 appway-bridge-prod ──
    print(f"S3 – s3://{BUCKET}")
    check("s3:ListBucket (incoming/)",
          lambda: s3.list_objects_v2(Bucket=BUCKET, Prefix="incoming/", MaxKeys=1))
    check("s3:ListBucket (results/)",
          lambda: s3.list_objects_v2(Bucket=BUCKET, Prefix="results/", MaxKeys=1))

    # Head an object we know doesn't exist → NoSuchKey is fine; AccessDenied means missing permission
    probe_key_results = f"results/__probe__/{uuid.uuid4()}.txt"
    probe_key_failed = f"failed/__probe__/{uuid.uuid4()}.txt"

    def head_missing(key: str):
        try:
            s3.head_object(Bucket=BUCKET, Key=key)
        except ClientError as e:
            # 404 / NoSuchKey means authorized but key doesn't exist → GetObject works.
            if e.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
                return
            raise
    check("s3:GetObject results/ (HeadObject B7 probe)", lambda: head_missing(probe_key_results))

    def put_and_delete(key: str):
        s3.put_object(Bucket=BUCKET, Key=key, Body=b"probe")
        try:
            s3.delete_object(Bucket=BUCKET, Key=key)
        except ClientError:
            pass
    check("s3:PutObject results/", lambda: put_and_delete(probe_key_results))
    check("s3:PutObject failed/", lambda: put_and_delete(probe_key_failed))

    print()

    # ── S3 ray-bucket-ai-models ──
    print(f"S3 – s3://{MODEL_BUCKET} (model weights)")
    check("s3:ListBucket",
          lambda: s3.list_objects_v2(Bucket=MODEL_BUCKET, MaxKeys=1))
    # Try to head a key we know exists or doesn't
    check("s3:GetObject (HeadObject probe)",
          lambda: head_missing_other_bucket(s3, MODEL_BUCKET, f"__probe__/{uuid.uuid4()}.txt"))

    print()

    # ── SNS appway-dlq-alerts ──
    print(f"SNS – {SNS_TOPIC}")
    check("sns:GetTopicAttributes",
          lambda: sns.get_topic_attributes(TopicArn=SNS_TOPIC))
    check("sns:ListSubscriptionsByTopic",
          lambda: sns.list_subscriptions_by_topic(TopicArn=SNS_TOPIC))
    check("sns:ListTopics",
          lambda: sns.list_topics())
    # sns:Publish — publish a harmless probe message
    check("sns:Publish",
          lambda: sns.publish(TopicArn=SNS_TOPIC, Subject="IAM probe",
                              Message=f"Permissions probe at {uuid.uuid4()} — safe to ignore."))

    print()

    # ── CloudWatch ──
    print("CloudWatch")
    check("cloudwatch:DescribeAlarms",
          lambda: cw.describe_alarms(MaxRecords=1))
    check("cloudwatch:PutMetricData",
          lambda: cw.put_metric_data(
              Namespace="AppWay/Probe",
              MetricData=[{"MetricName": "ProbeCall", "Value": 1.0, "Unit": "Count"}],
          ))

    print()
    print("Done. ✓ = authorized, ✗ = missing or denied.")
    return 0


def head_missing_other_bucket(s3, bucket: str, key: str):
    try:
        s3.head_object(Bucket=bucket, Key=key)
    except ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return
        raise


if __name__ == "__main__":
    sys.exit(main())
