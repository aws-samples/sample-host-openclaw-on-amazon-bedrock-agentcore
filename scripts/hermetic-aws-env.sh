#!/usr/bin/env bash
# Run one command without access to host AWS credential-provider state.
# Callers provide a private temporary HOME and remain responsible for cleanup.

run_with_hermetic_aws_env() {
  if [[ "$#" -lt 2 ]]; then
    printf 'hermetic AWS environment: HOME and command are required\n' >&2
    return 2
  fi

  local isolated_home="$1"
  shift
  mkdir -p "${isolated_home}"
  chmod 0700 "${isolated_home}"

  env \
    -u AWS_PROFILE \
    -u AWS_DEFAULT_PROFILE \
    -u AWS_ACCESS_KEY_ID \
    -u AWS_SECRET_ACCESS_KEY \
    -u AWS_SESSION_TOKEN \
    -u AWS_SECURITY_TOKEN \
    -u AWS_CREDENTIAL_EXPIRATION \
    -u AWS_CONFIG_FILE \
    -u AWS_SHARED_CREDENTIALS_FILE \
    -u AWS_SDK_LOAD_CONFIG \
    -u AWS_WEB_IDENTITY_TOKEN_FILE \
    -u AWS_ROLE_ARN \
    -u AWS_ROLE_SESSION_NAME \
    -u AWS_CONTAINER_CREDENTIALS_RELATIVE_URI \
    -u AWS_CONTAINER_CREDENTIALS_FULL_URI \
    -u AWS_CONTAINER_AUTHORIZATION_TOKEN \
    -u AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE \
    -u AWS_EC2_METADATA_SERVICE_ENDPOINT \
    -u AWS_EC2_METADATA_SERVICE_ENDPOINT_MODE \
    HOME="${isolated_home}" \
    AWS_CONFIG_FILE=/dev/null \
    AWS_SHARED_CREDENTIALS_FILE=/dev/null \
    AWS_EC2_METADATA_DISABLED=true \
    BOTO_CONFIG=/dev/null \
    "$@"
}
