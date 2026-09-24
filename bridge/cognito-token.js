/**
 * Per-user Cognito token minting, shared by the Bedrock proxy
 * (agentcore-proxy.js, where it originated; it uses the ID token) and the
 * contract server (agentcore-contract.js, which uses the ACCESS token as the
 * bearer for the AgentCore Gateway MCP server entry in openclaw.json).
 *
 * The Gateway's CUSTOM_JWT authorizer accepts only the access token: an ID
 * token is refused with 403 "insufficient_scope" (verified live 2026-09-24).
 * One ADMIN_USER_PASSWORD_AUTH call returns both, so they are cached together.
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
  const tokenCache = new Map(); // actorId -> { id: {token, expiresAt, expiresIn}, access: {...} }

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
   * Authenticate actorId and cache both tokens (until 60 s before expiry;
   * `force` bypasses the cache). Returns the cache entry or null when Cognito
   * is not configured.
   */
  async function authenticate(actorId, { force = false } = {}) {
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
    const result = response.AuthenticationResult;
    const expiresIn = result.ExpiresIn || 3600;
    const expiresAt = Date.now() + (expiresIn - 60) * 1000;
    const entry = {
      expiresAt,
      id: { token: result.IdToken, expiresAt, expiresIn },
      access: { token: result.AccessToken, expiresAt, expiresIn },
    };
    tokenCache.set(actorId, entry);
    log(`[cognito] token acquired for ${actorId} (expires in ${expiresIn}s)`);
    return entry;
  }

  /** `{ token, expiresAt, expiresIn }` with the ID token (aud = client id). */
  async function getIdToken(actorId, opts) {
    const entry = await authenticate(actorId, opts);
    return entry ? entry.id : null;
  }

  /**
   * `{ token, expiresAt, expiresIn }` with the ACCESS token (client_id = client
   * id, scope aws.cognito.signin.user.admin) — the only token type the
   * AgentCore Gateway accepts as a bearer.
   */
  async function getAccessToken(actorId, opts) {
    const entry = await authenticate(actorId, opts);
    if (!entry) return null;
    if (!entry.access.token) throw new Error("Cognito returned no AccessToken");
    return entry.access;
  }

  return { isConfigured, derivePassword, ensureUser, getIdToken, getAccessToken, _cache: tokenCache };
}

module.exports = { createCognitoTokenProvider };
