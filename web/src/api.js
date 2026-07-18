const CSRF_KEY = "personal-operator.csrf";

export function csrfToken() {
  return sessionStorage.getItem(CSRF_KEY) || "";
}

export function rememberCsrf(value) {
  if (typeof value !== "string" || value.length < 32) {
    throw new Error("The secure session did not return a valid CSRF token.");
  }
  sessionStorage.setItem(CSRF_KEY, value);
}

export async function api(path, { method = "GET", body, csrf = false } = {}) {
  const headers = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (csrf) headers["X-PO-CSRF"] = csrfToken();
  const response = await fetch(path, {
    method,
    credentials: "same-origin",
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json")
    ? await response.json()
    : await response.text();
  if (!response.ok) {
    const message = payload?.error || "This operation could not be completed.";
    throw new Error(message);
  }
  return { payload, response };
}
