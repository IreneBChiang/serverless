# serverless
Contains:

Lambda Python handler (lambda/index.py)

DynamoDB table access (optional) or in-memory dedupe (allowed per assignment)

SNS → Lambda subscription trigger

SES email sending

CI/CD workflow: build + update Lambda function

Workflow File:
/.github/workflows/lambda-ci.yml