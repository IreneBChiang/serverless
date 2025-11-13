# lambda/index.py
import json
import os
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

# 環境變數（由 Terraform 設定）
VERIFY_BASE_URL = os.environ.get("VERIFY_BASE_URL")  # e.g. https://demo.ibchiang.me/validateEmail
FROM_EMAIL = os.environ.get("FROM_EMAIL")            # SES verified sender
DDB_TABLE_NAME = os.environ.get("DDB_TABLE_NAME")    # e.g. user_verification_email
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

ses_client = boto3.client("ses", region_name=AWS_REGION)
dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
ddb_table = dynamodb.Table(DDB_TABLE_NAME)


def _log(msg: str, **kwargs):
    """小工具：log 到 CloudWatch，方便 debug。"""
    record = {"message": msg, **kwargs}
    print(json.dumps(record, default=str))


def _already_sent(email: str, token: str) -> bool:
    """
    用 DynamoDB 檢查某個 email+token 是否已經寄過。
    如果已經寄過，就不再寄，避免 duplicate email。
    """
    try:
        resp = ddb_table.get_item(Key={"email": email})
    except ClientError as e:
        _log("DDB get_item failed", error=str(e))
        return False

    item = resp.get("Item")
    if not item:
        return False

    last_token = item.get("last_token")
    if last_token == token:
        _log("Duplicate token detected, skip sending", email=email, token=token)
        return True

    return False


def _mark_sent(email: str, token: str):
    """寄信成功後，把 email+token 記到 DynamoDB。"""
    now = datetime.now(timezone.utc).isoformat()

    try:
        ddb_table.put_item(
            Item={
                "email": email,
                "last_token": token,
                "last_sent_at": now,
            }
        )
    except ClientError as e:
        _log("DDB put_item failed", error=str(e), email=email, token=token)


def _send_verification_email(email: str, first_name: str, token: str):
    """呼叫 SES 寄出驗證信。"""
    if not VERIFY_BASE_URL:
        raise RuntimeError("VERIFY_BASE_URL is not configured")
    if not FROM_EMAIL:
        raise RuntimeError("FROM_EMAIL is not configured")

    verify_link = f"{VERIFY_BASE_URL}?token={token}"

    subject = "Verify your account"
    text_body = (
        f"Hi {first_name},\n\n"
        f"Please click the link below to verify your account:\n"
        f"{verify_link}\n\n"
        f"If you did not sign up, you can ignore this email."
    )

    html_body = f"""
    <html>
      <body>
        <p>Hi {first_name},</p>
        <p>Please click the link below to verify your account:</p>
        <p><a href="{verify_link}">{verify_link}</a></p>
        <p>If you did not sign up, you can ignore this email.</p>
      </body>
    </html>
    """

    try:
        resp = ses_client.send_email(
            Source=FROM_EMAIL,
            Destination={"ToAddresses": [email]},
            Message={
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {
                    "Text": {"Data": text_body, "Charset": "UTF-8"},
                    "Html": {"Data": html_body, "Charset": "UTF-8"},
                },
            },
        )
        _log("SES send_email success", email=email, message_id=resp["MessageId"])
    except ClientError as e:
        _log("SES send_email failed", email=email, error=str(e))
        raise


def handler(event, context):
    """
    Lambda 入口點，SNS event 大概是這樣：

    {
      "Records": [
        {
          "Sns": {
            "Message": "{\"email\": \"xxx\", \"first_name\": \"Irene\", \"token\": \"...\"}"
          }
        }
      ]
    }
    """
    _log("Lambda invoked", raw_event=event)

    records = event.get("Records", [])
    if not records:
        _log("No SNS records")
        return {"statusCode": 200, "body": json.dumps({"message": "no records"})}

    for rec in records:
        sns = rec.get("Sns") or {}
        msg_str = sns.get("Message")

        if not msg_str:
            _log("Empty SNS message, skip")
            continue

        try:
            msg = json.loads(msg_str)
        except json.JSONDecodeError:
            _log("Message is not valid JSON, skip", message=msg_str)
            continue

        email = msg.get("email")
        first_name = msg.get("first_name") or "there"
        token = msg.get("token")

        if not email or not token:
            _log("Missing email or token, skip", msg=msg)
            continue

        # 避免重複 email
        if _already_sent(email, token):
            continue

        # 寄信
        _send_verification_email(email, first_name, token)

        # 記錄寄過
        _mark_sent(email, token)

    return {"statusCode": 200, "body": json.dumps({"message": "ok"})}
