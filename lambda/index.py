# lambda/index.py
import json
import os
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError
import requests  # 用來呼叫 SendGrid API

# 環境變數（由 Terraform 設定）
VERIFY_BASE_URL = os.environ.get("VERIFY_BASE_URL")      # e.g. https://demo.xxx/validateEmail
FROM_EMAIL = os.environ.get("FROM_EMAIL")                # e.g. noreply@ibchiang.me
DDB_TABLE_NAME = os.environ.get("DDB_TABLE_NAME")        # e.g. <project>-verification-email
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
SENDGRID_SECRET_ARN = os.environ.get("SENDGRID_SECRET_ARN")

dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
ddb_table = dynamodb.Table(DDB_TABLE_NAME)

secrets_client = boto3.client("secretsmanager", region_name=AWS_REGION)


def _log(msg: str, **kwargs):
    """小工具：log 到 CloudWatch，方便 debug。"""
    record = {"message": msg, **kwargs}
    print(json.dumps(record, default=str))


def _get_sendgrid_api_key() -> str:
    """
    從 Secrets Manager 讀取 SendGrid API key。
    Secret 內容格式預期為：
    {
      "SENDGRID_API_KEY": "SG.xxxxx..."
    }
    """
    # 先看環境變數，有就直接用
    direct = os.environ.get("SENDGRID_API_KEY")
    if direct:
        return direct

    # 沒有才從 Secrets Manager 拿
    if not SENDGRID_SECRET_ARN:
        raise RuntimeError("SENDGRID_SECRET_ARN is not configured")

    try:
        resp = secrets_client.get_secret_value(SecretId=SENDGRID_SECRET_ARN)
        raw = resp.get("SecretString") or "{}"
        data = json.loads(raw)
        api_key = data.get("SENDGRID_API_KEY")
        if not api_key:
            raise RuntimeError("SENDGRID_API_KEY not found in secret")
        return api_key
    except ClientError as e:
        _log("Failed to read SendGrid secret", error=str(e))
        raise


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
    """使用 SendGrid 寄出驗證信。"""
    if not VERIFY_BASE_URL:
        raise RuntimeError("VERIFY_BASE_URL is not configured")
    if not FROM_EMAIL:
        raise RuntimeError("FROM_EMAIL is not configured")

    api_key = _get_sendgrid_api_key()

    verify_link = f"{VERIFY_BASE_URL}?email={email}&token={token}"

    subject = "Verify your account"
    content = (
        f"Hi {first_name},\n\n"
        f"Please click the link below to verify your account:\n"
        f"{verify_link}\n\n"
        f"If you did not sign up, you can ignore this email."
    )

    url = "https://api.sendgrid.com/v3/mail/send"

    payload = {
        "personalizations": [
            {
                "to": [{"email": email}],
                "subject": subject,
            }
        ],
        "from": {"email": FROM_EMAIL},
        "content": [
            {
                "type": "text/plain",
                "value": content,
            }
        ],
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    resp = requests.post(url, headers=headers, data=json.dumps(payload))

    if resp.status_code >= 400:
        _log(
            "SendGrid send failed",
            status_code=resp.status_code,
            body=resp.text,
            email=email,
        )
        raise RuntimeError(f"SendGrid send failed: {resp.status_code}")

    _log("SendGrid send success", email=email, status_code=resp.status_code)


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
