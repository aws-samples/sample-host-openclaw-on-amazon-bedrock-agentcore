/**
 * Per-user Cognito token minting, shared by the Bedrock proxy
 * (agentcore-proxy.js; it uses the ID token) and the contract server
 * (agentcore-contract.js, which uses the ACCESS token as the bearer for the
 * AgentCore Gateway MCP server entry in openclaw.json).
 *
 * The Gateway's CUSTOM_JWT authorizer accepts only the access token: an ID
 * token is refused with 403 "insufficient_scope". One ADMIN_USER_PASSWORD_AUTH
 * call returns both, so they are cached together.
 *
 * One Cognito user per actorId ("telegram:123"). Each user's password is
 * generated with crypto.randomBytes and stored in its own Secrets Manager
 * secret, `openclaw/cognito-user/<userPoolId>/<namespace>`. Nothing derives a
 * password from a shared key.
 *
 * The secret lives outside `openclaw/user/<namespace>/`, the prefix the
 * agent's scoped session credentials (and the api-keys skill) can read and
 * write, so the agent cannot list, read or overwrite it. Only the execution
 * role reaches it. The pool id in the name keeps two deployments that share an
 * account and region from using each other's values.
 *
 * The stored secret is authoritative:
 * - A new or pre-existing user without a secret claims one with CreateSecret
 *   first; a concurrent caller that loses the claim adopts the winner's value,
 *   so every process sets the same password on Cognito.
 * - Users created by the previous scheme have no secret. On their next
 *   authentication they get a fresh password (claimed, then set on Cognito);
 *   no user has to re-enrol.
 * - If Cognito refuses the stored password (an interrupted claim, or the user
 *   was reset outside this module), the stored value is set on Cognito again
 *   and authentication is retried once.
 */
"use strict";

const crypto = require("crypto");

const SECRET_PREFIX = "openclaw/cognito-user/";

function actorToNamespace(actorId) {
  return String(actorId || "").replace(/:/g, "_");
}

function defaultSecretId(actorId, userPoolId) {
  return SECRET_PREFIX + userPoolId + "/" + actorToNamespace(actorId);
}

function generateRandomPassword() {
  // 24 random bytes -> 32 base64url chars. The fixed "Aa1" suffix guarantees
  // the pool's lowercase, uppercase and digit classes (stacks/security_stack.py
  // password_policy); symbols are not required by the pool.
  return crypto.randomBytes(24).toString("base64url") + "Aa1";
}

function createCognitoTokenProvider(config) {
  const {
    userPoolId,
    clientId,
    region,
    client, // injectable CognitoIdentityProviderClient (tests)
    commands, // injectable Cognito command classes (tests)
    secretsClient, // injectable SecretsManagerClient (tests)
    secretsCommands, // injectable Secrets Manager command classes (tests)
    secretId = defaultSecretId,
    generatePassword = generateRandomPassword,
    log = console.log,
  } = config;

  let _client = client || null;
  let _commands = commands || null;
  let _secretsClient = secretsClient || null;
  let _secretsCommands = secretsCommands || null;

  function sdk() {
    if (!_commands) _commands = require("@aws-sdk/client-cognito-identity-provider");
    return _commands;
  }
  function smSdk() {
    if (!_secretsCommands) _secretsCommands = require("@aws-sdk/client-secrets-manager");
    return _secretsCommands;
  }

  const tokenCache = new Map(); // actorId -> { id: {token, expiresAt, expiresIn}, access: {...} }
  const inFlight = new Map(); // actorId -> pending authentication

  function isConfigured() {
    return Boolean(userPoolId && clientId);
  }

  function getClient() {
    if (!_client) {
      _client = new (sdk().CognitoIdentityProviderClient)({ region });
    }
    return _client;
  }

  function getSecretsClient() {
    if (!_secretsClient) {
      _secretsClient = new (smSdk().SecretsManagerClient)({ region });
    }
    return _secretsClient;
  }

  function nameOf(actorId) {
    return secretId(actorId, userPoolId);
  }

  async function fetchStoredPassword(actorId) {
    const { GetSecretValueCommand } = smSdk();
    try {
      const resp = await getSecretsClient().send(
        new GetSecretValueCommand({ SecretId: nameOf(actorId) }),
      );
      return resp.SecretString || null;
    } catch (err) {
      if (err && err.name === "ResourceNotFoundException") return null;
      throw err;
    }
  }

  /**
   * Store `password` as the user's secret unless one already exists, and
   * return the value that is now authoritative (ours, or the existing one).
   */
  async function claimPassword(actorId, password) {
    const { CreateSecretCommand } = smSdk();
    try {
      await getSecretsClient().send(
        new CreateSecretCommand({ Name: nameOf(actorId), SecretString: password }),
      );
      return password;
    } catch (err) {
      if (!err || err.name !== "ResourceExistsException") throw err;
      const existing = await fetchStoredPassword(actorId);
      if (!existing) throw new Error("Cognito password secret exists but has no value");
      return existing;
    }
  }

  async function setPermanentPassword(actorId, password) {
    const { AdminSetUserPasswordCommand } = sdk();
    await getClient().send(
      new AdminSetUserPasswordCommand({
        UserPoolId: userPoolId,
        Username: actorId,
        Password: password,
        Permanent: true,
      }),
    );
  }

  /** Make sure the Cognito user exists and return its authoritative password. */
  async function ensureUser(actorId) {
    const { AdminGetUserCommand, AdminCreateUserCommand } = sdk();
    const c = getClient();
    let userExists = true;
    try {
      await c.send(new AdminGetUserCommand({ UserPoolId: userPoolId, Username: actorId }));
    } catch (err) {
      if (err.name !== "UserNotFoundException") throw err;
      userExists = false;
    }

    if (userExists) {
      const stored = await fetchStoredPassword(actorId);
      if (stored) return stored;
      // Created by the previous derivation scheme (or outside this module):
      // claim a fresh password, then set whichever value won the claim.
      const password = await claimPassword(actorId, generatePassword());
      await setPermanentPassword(actorId, password);
      log("[cognito] user password migrated to a per-user secret: " + actorId);
      return password;
    }

    const password = await claimPassword(actorId, generatePassword());
    try {
      await c.send(
        new AdminCreateUserCommand({
          UserPoolId: userPoolId,
          Username: actorId,
          MessageAction: "SUPPRESS",
          TemporaryPassword: password,
        }),
      );
    } catch (err) {
      // A concurrent caller created the user first; it set the same claimed
      // password, and setting it again below is harmless.
      if (err.name !== "UsernameExistsException") throw err;
    }
    await setPermanentPassword(actorId, password);
    log("[cognito] user provisioned: " + actorId);
    return password;
  }

  async function initiateAuth(actorId, password) {
    const { AdminInitiateAuthCommand } = sdk();
    return getClient().send(
      new AdminInitiateAuthCommand({
        UserPoolId: userPoolId,
        ClientId: clientId,
        AuthFlow: "ADMIN_USER_PASSWORD_AUTH",
        AuthParameters: { USERNAME: actorId, PASSWORD: password },
      }),
    );
  }

  async function authenticateUncached(actorId) {
    const password = await ensureUser(actorId);
    let response;
    try {
      response = await initiateAuth(actorId, password);
    } catch (err) {
      if (err.name !== "NotAuthorizedException" && err.name !== "PasswordResetRequiredException") throw err;
      // Cognito holds a different password than the stored secret. The secret
      // is authoritative: set it on Cognito again and retry once.
      await setPermanentPassword(actorId, password);
      log("[cognito] user password re-synchronised from its secret: " + actorId);
      response = await initiateAuth(actorId, password);
    }
    const result = response.AuthenticationResult;
    const expiresIn = result.ExpiresIn || 3600;
    const expiresAt = Date.now() + (expiresIn - 60) * 1000;
    const entry = {
      expiresAt,
      id: { token: result.IdToken, expiresAt, expiresIn },
      access: { token: result.AccessToken, expiresAt, expiresIn },
    };
    tokenCache.set(actorId, entry);
    log("[cognito] token acquired for " + actorId + " (expires in " + expiresIn + "s)");
    return entry;
  }

  /**
   * Authenticate actorId and cache both tokens (until 60 s before expiry;
   * `force` bypasses the cache). Returns the cache entry or null when Cognito
   * is not configured. Concurrent calls for one actor share one attempt.
   */
  async function authenticate(actorId, { force = false } = {}) {
    if (!isConfigured()) return null;
    const cached = tokenCache.get(actorId);
    if (!force && cached && cached.expiresAt > Date.now()) return cached;
    const pending = inFlight.get(actorId);
    if (pending) return pending;
    const attempt = authenticateUncached(actorId);
    inFlight.set(actorId, attempt);
    const clear = () => { if (inFlight.get(actorId) === attempt) inFlight.delete(actorId); };
    attempt.then(clear, clear);
    return attempt;
  }

  /** `{ token, expiresAt, expiresIn }` with the ID token (aud = client id). */
  async function getIdToken(actorId, opts) {
    const entry = await authenticate(actorId, opts);
    return entry ? entry.id : null;
  }

  /**
   * `{ token, expiresAt, expiresIn }` with the ACCESS token (client_id = client
   * id, scope aws.cognito.signin.user.admin), the only token type the
   * AgentCore Gateway accepts as a bearer.
   */
  async function getAccessToken(actorId, opts) {
    const entry = await authenticate(actorId, opts);
    if (!entry) return null;
    if (!entry.access.token) throw new Error("Cognito returned no AccessToken");
    return entry.access;
  }

  return { isConfigured, ensureUser, getIdToken, getAccessToken, _cache: tokenCache };
}

module.exports = { createCognitoTokenProvider, defaultSecretId, generateRandomPassword, SECRET_PREFIX };
