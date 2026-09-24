/**
 * Per-user Cognito ID token minting, shared by the Bedrock proxy
 * (agentcore-proxy.js, where it originated) and the contract server
 * (agentcore-contract.js, which needs the same token as the bearer for the
 * AgentCore Gateway MCP server entry in openclaw.json).
 *
 * One Cognito user per actorId ("telegram:123"), created on first use with a
 * password derived as HMAC-SHA256(COGNITO_PASSWORD_SECRET, actorId). Both
 * callers MUST derive the password identically, which is why this lives in
 * one module.
 */
"use strict";

const crypto = require("crypto");

function createCognitoTokenProvider(config) {
  const {
    userPoolId,
    clientId,
    passwordSecret,
    region,
    client, // injectable CognitoIdentityProviderClient (tests)
    commands, // injectable command classes (tests)
    log = console.log,
  } = config;

  let _client = client || null;
  let _commands = commands || null;
  function sdk() {
    if (!_commands) _commands = require("@aws-sdk/client-cognito-identity-provider");
    return _commands;
  }
  const tokenCache = new Map(); // actorId -> { token, expiresAt }

  function isConfigured() {
    return Boolean(userPoolId && clientId && passwordSecret);
  }

  function derivePassword(actorId) {
    return crypto
      .createHmac("sha256", passwordSecret)
      .update(actorId)
      .digest("base64url")
      .slice(0, 32);
  }

  function getClient() {
    if (!_client) {
      _client = new (sdk().CognitoIdentityProviderClient)({ region });
    }
    return _client;
  }

  async function ensureUser(actorId) {
    const { AdminGetUserCommand, AdminCreateUserCommand, AdminSetUserPasswordCommand } = sdk();
    const c = getClient();
    try {
      await c.send(new AdminGetUserCommand({ UserPoolId: userPoolId, Username: actorId }));
    } catch (err) {
      if (err.name !== "UserNotFoundException") throw err;
      const password = derivePassword(actorId);
      await c.send(
        new AdminCreateUserCommand({
          UserPoolId: userPoolId,
          Username: actorId,
          MessageAction: "SUPPRESS",
          TemporaryPassword: password,
        }),
      );
      await c.send(
        new AdminSetUserPasswordCommand({
          UserPoolId: userPoolId,
          Username: actorId,
          Password: password,
          Permanent: true,
        }),
      );
      log(`[cognito] user provisioned: ${actorId}`);
    }
  }

  /**
   * Return `{ token, expiresAt, expiresIn }` for actorId (cached until 60 s
   * before expiry; `{ force: true }` bypasses the cache), or null when Cognito
   * is not configured.
   */
  async function getIdToken(actorId, { force = false } = {}) {
    if (!isConfigured()) return null;
    const cached = tokenCache.get(actorId);
    if (!force && cached && cached.expiresAt > Date.now()) return cached;

    await ensureUser(actorId);
    const { AdminInitiateAuthCommand } = sdk();
    const response = await getClient().send(
      new AdminInitiateAuthCommand({
        UserPoolId: userPoolId,
        ClientId: clientId,
        AuthFlow: "ADMIN_USER_PASSWORD_AUTH",
        AuthParameters: { USERNAME: actorId, PASSWORD: derivePassword(actorId) },
      }),
    );
    const token = response.AuthenticationResult.IdToken;
    const expiresIn = response.AuthenticationResult.ExpiresIn || 3600;
    const entry = { token, expiresAt: Date.now() + (expiresIn - 60) * 1000, expiresIn };
    tokenCache.set(actorId, entry);
    log(`[cognito] token acquired for ${actorId} (expires in ${expiresIn}s)`);
    return entry;
  }

  return { isConfigured, derivePassword, ensureUser, getIdToken, _cache: tokenCache };
}

module.exports = { createCognitoTokenProvider };
