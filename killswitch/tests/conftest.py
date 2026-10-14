"""pytest fixtures for killswitch tests: moto account with students, operator,
EduCleanupRole, SNS topic (+ SQS spy) and fake Bedrock API keys."""
import os
import sys
from pathlib import Path

import boto3
import pytest

HERE = Path(__file__).resolve().parent
KS = HERE.parent
ROOT = KS.parent
for p in (str(ROOT), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

# Never touch real AWS from tests.
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ["AWS_SESSION_TOKEN"] = "testing"
os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
os.environ.pop("AWS_PROFILE", None)

from moto import mock_aws  # noqa: E402

from killswitch_test_support import FakeServiceSpecificCredentials, load_module  # noqa: E402


@pytest.fixture
def lam():
    """Fresh copy of the Lambda module (lambda_src.py)."""
    return load_module("ks_lambda_src", KS / "lambda_src.py")


@pytest.fixture
def ssc():
    return FakeServiceSpecificCredentials()


@pytest.fixture
def aws():
    with mock_aws():
        yield


@pytest.fixture
def iam(aws):
    return boto3.client("iam", region_name="us-east-1")


@pytest.fixture
def world(aws, iam, ssc, lam, monkeypatch):
    """Account with students, an operator, EduCleanupRole and an SNS topic + SQS spy."""
    for name in ("alice", "bob", "carol", "Admin-Ops"):
        iam.create_user(UserName=name)
    iam.create_group(GroupName="students")
    for name in ("alice", "bob"):
        iam.add_user_to_group(GroupName="students", UserName=name)
    keys = {}
    for name in ("alice", "bob", "carol", "Admin-Ops"):
        keys[name] = iam.create_access_key(UserName=name)["AccessKey"]["AccessKeyId"]
    # carol has a second key the operator had already disabled before the incident
    old = iam.create_access_key(UserName="carol")["AccessKey"]["AccessKeyId"]
    iam.update_access_key(UserName="carol", AccessKeyId=old, Status="Inactive")
    keys["carol-old"] = old
    ssc.add("alice", "ACCAEXAMPLEBEDROCK01")
    ssc.add("alice", "ACCAEXAMPLEBEDROCK02")
    ssc.add("alice", "ACCAEXAMPLEBEDROCK03")          # third one sits on page 2
    ssc.add("alice", "ACCAEXAMPLECODECOMM1", service="codecommit.amazonaws.com")
    ssc.add("Admin-Ops", "ACCAEXAMPLEOPERATOR1")
    iam.create_role(RoleName="EduCleanupRole", AssumeRolePolicyDocument='{"Version":"2012-10-17","Statement":[]}')
    iam.put_role_policy(RoleName="EduCleanupRole", PolicyName="keep",
                        PolicyDocument='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":"*","Resource":"*"}]}')

    sns = boto3.client("sns", region_name="us-east-1")
    sqs = boto3.client("sqs", region_name="us-east-1")
    topic = sns.create_topic(Name="edu-killswitch-us-east-1-Topic-TEST")["TopicArn"]
    qurl = sqs.create_queue(QueueName="spy")["QueueUrl"]
    qarn = sqs.get_queue_attributes(QueueUrl=qurl, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    sns.subscribe(TopicArn=topic, Protocol="sqs", Endpoint=qarn)

    monkeypatch.setenv("TARGET_MODE", "ALL_IAM_USERS")
    monkeypatch.setenv("TARGET_GROUP", "")
    monkeypatch.setenv("EXEMPT_USERS", "admin-ops")   # case-insensitive match
    monkeypatch.setenv("TOPIC_ARN", topic)
    monkeypatch.setenv("ACCOUNT_ID", "123456789012")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    lambda_iam = ssc.attach(boto3.client("iam", region_name="us-east-1"))
    monkeypatch.setattr(lam, "_iam", lambda: lambda_iam)

    class World:
        pass

    w = World()
    w.iam, w.ssc, w.lam, w.keys, w.topic, w.sqs, w.queue = iam, ssc, lam, keys, topic, sqs, qurl
    w.lambda_iam = lambda_iam

    def messages():
        out = []
        while True:
            r = sqs.receive_message(QueueUrl=qurl, MaxNumberOfMessages=10)
            if not r.get("Messages"):
                return out
            out += r["Messages"]
            for m in r["Messages"]:
                sqs.delete_message(QueueUrl=qurl, ReceiptHandle=m["ReceiptHandle"])

    w.messages = messages
    return w
