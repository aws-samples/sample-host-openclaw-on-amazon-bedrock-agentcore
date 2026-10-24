#!/usr/bin/env python3
"""Resolve or create the ECR repository for the AgentCore Runtime bridge image.

Restricts repository names to the prefixes accepted by the deployment
controller and handles a concurrent create by reading the repository back.
The CLI requires --region, AWS credentials, and the pinned deployment SDK;
it prints the repository URI for the subsequent image build and push.
"""

import argparse

from botocore.exceptions import ClientError

from runtime_deploy import check_sdk_versions, validate_repository


def ensure_repository(client, name):
    validate_repository(name)
    try:
        response = client.describe_repositories(repositoryNames=[name])
    except ClientError as error:
        if error.response["Error"]["Code"] != "RepositoryNotFoundException":
            raise
        try:
            return client.create_repository(
                repositoryName=name,
                imageTagMutability="IMMUTABLE",
                imageScanningConfiguration={"scanOnPush": True},
            )["repository"]["repositoryUri"]
        except ClientError as create_error:
            if create_error.response["Error"]["Code"] != "RepositoryAlreadyExistsException":
                raise
            response = client.describe_repositories(repositoryNames=[name])
    repositories = response.get("repositories", [])
    if len(repositories) != 1 or repositories[0].get("repositoryName") != name:
        raise ValueError("ECR did not return the requested repository")
    return repositories[0]["repositoryUri"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Resolve or create the deployment image repository")
    parser.add_argument("--region", required=True)
    parser.add_argument("--name", default="openclaw-bridge")
    args = parser.parse_args()
    check_sdk_versions()
    import boto3
    print(ensure_repository(boto3.client("ecr", region_name=args.region), args.name))
