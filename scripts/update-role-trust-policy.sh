#!/bin/bash
# Update the execution role trust policy to allow self-assume
# This must be done after the role is created because IAM rejects
# roles that reference themselves during creation.

set -e

REGION=${CDK_DEFAULT_REGION:-us-east-1}
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
ROLE_NAME="openclaw-agentcore-execution-role"

echo "Updating trust policy for role: $ROLE_NAME"

aws iam update-assume-role-policy \
  --role-name "$ROLE_NAME" \
  --policy-document '{
    "Version": "2012-10-17",
    "Statement": [
      {
        "Effect": "Allow",
        "Principal": {
          "Service": [
            "ecs-tasks.amazonaws.com",
            "bedrock.amazonaws.com",
            "bedrock-agentcore.amazonaws.com"
          ]
        },
        "Action": "sts:AssumeRole"
      },
      {
        "Effect": "Allow",
        "Principal": {
          "AWS": "arn:aws:iam::'"$ACCOUNT"':role/'"$ROLE_NAME"'"
        },
        "Action": "sts:AssumeRole",
        "Condition": {
          "StringLike": {
            "sts:RoleSessionName": "scoped-*"
          }
        }
      }
    ]
  }'

echo "✅ Trust policy updated successfully"
