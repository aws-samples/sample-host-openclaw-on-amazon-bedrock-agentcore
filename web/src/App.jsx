import { useEffect, useMemo, useState } from "react";
import { api, forgetCsrf, rememberCsrf } from "./api";

function LogoutButton() {
  const [state, setState] = useState("ready");
  async function logout() {
    setState("loading");
    try {
      await api("/api/session/logout", { method: "POST", body: {}, csrf: true });
      forgetCsrf();
      setState("done");
    } catch {
      setState("error");
    }
  }
  if (state === "done") return <span className="nav-status" role="status">Signed out</span>;
  return (
    <button className="nav-action" disabled={state === "loading"} onClick={logout} type="button">
      {state === "error" ? "Try sign out again" : "Sign out"}
    </button>
  );
}

function Shell({ eyebrow, title, children, authenticated = true }) {
  return (
    <main className="shell">
      <nav aria-label="Primary">
        <a className="brand" href="/">PO<span>.</span></a>
        <div className="nav-links">
          <a href="/">Overview</a>
          <a href="/connections">Connections</a>
          <a href="/workspace">Workspace</a>
          <a href="/export">Export</a>
          <a href="/delete">Delete</a>
          {authenticated && <LogoutButton />}
        </div>
      </nav>
      <section className="surface">
        <p className="eyebrow">{eyebrow}</p>
        <h1>{title}</h1>
        {children}
      </section>
      <p className="boundary">Your AI workspace never receives provider credentials.</p>
    </main>
  );
}

function Status({ state, children }) {
  if (state === "loading") return <p className="status" role="status">Loading…</p>;
  if (state === "error") return <p className="error" role="alert">{children}</p>;
  return children;
}

const STATIC_RETURN_PATHS = new Set(["/", "/connections", "/workspace", "/export", "/delete"]);

function validReturnPath(value) {
  return typeof value === "string" && (
    STATIC_RETURN_PATHS.has(value) || /^\/workspace\?draft=[A-Za-z0-9_-]{8,128}$/.test(value)
  );
}

function assertReadOnlyOverview(payload) {
  if (
    !payload
    || payload.version !== "personal-operator.pilot-overview.v1"
    || payload.externalEffects !== false
    || payload.capability?.externalEffects !== false
    || payload.capability?.mode !== "READ_ONLY"
    || payload.connection?.access !== "READ_ONLY"
  ) {
    throw new Error("The read-only boundary is unavailable.");
  }
  return payload;
}

function TicketBootstrap() {
  const ticket = useMemo(() => new URLSearchParams(location.search).get("ticket"), []);
  const [state, setState] = useState(ticket ? "loading" : "missing");
  const [destination, setDestination] = useState("");
  const [error, setError] = useState("");
  useEffect(() => {
    if (!ticket) return;
    api("/api/session/connect", { method: "POST", body: { ticket } })
      .then(({ payload }) => {
        if (!validReturnPath(payload.returnPath)) {
          throw new Error("The secure session returned an invalid destination.");
        }
        rememberCsrf(payload.csrfToken);
        history.replaceState({}, "", payload.returnPath);
        setDestination(payload.returnPath);
        setState("ready");
      })
      .catch((failure) => {
        setError(failure.message);
        setState("error");
      });
  }, [ticket]);
  if (state === "ready") return <Route path={destination.split("?")[0]} />;
  return (
    <Shell eyebrow="Private control plane" title="Your operator, under your control." authenticated={false}>
      <Status state={state}>
        {state === "missing" && <p>Open the one-time connection link from your Telegram conversation.</p>}
        {state === "error" && error}
      </Status>
    </Shell>
  );
}

function OverviewPage() {
  const [view, setView] = useState({ state: "loading" });
  useEffect(() => {
    api("/api/overview")
      .then(({ payload }) => setView({ state: "ready", data: assertReadOnlyOverview(payload) }))
      .catch((error) => setView({ state: "error", error: error.message }));
  }, []);

  async function recordFeedback(response) {
    const scan = view.data.lastScan;
    setView((current) => ({ ...current, feedbackState: "loading" }));
    try {
      await api(`/api/scans/${encodeURIComponent(scan.scanId)}/feedback`, {
        method: "POST", body: { response }, csrf: true,
      });
      setView((current) => ({
        ...current,
        feedbackState: "done",
        data: { ...current.data, lastScan: { ...current.data.lastScan, feedback: response } },
      }));
    } catch (error) {
      setView((current) => ({ ...current, feedbackState: "error", feedbackError: error.message }));
    }
  }

  return (
    <Shell eyebrow="Read-only pilot" title="Your read-only operator.">
      <Status state={view.state}>{view.error}</Status>
      {view.state === "ready" && (
        <div className="overview-grid">
          <article className="summary-card safety-card">
            <p className="card-kicker">Safety boundary</p>
            <h2>External effects off</h2>
            <p>Gmail access is read-only. This pilot can find, explain, and draft locally; it cannot send or request approval.</p>
          </article>
          <article className="summary-card">
            <p className="card-kicker">Connection</p>
            <h2>{connectionLabel(view.data.connection.status)}</h2>
            <p>Google Gmail · read-only</p>
            <a className="text-link" href="/connections">Manage connection</a>
          </article>
          <article className="summary-card">
            <p className="card-kicker">Last scan</p>
            <h2>{scanLabel(view.data.lastScan)}</h2>
            {view.data.lastScan && (
              <p>{view.data.lastScan.resultCount ?? 0} follow-ups · {view.data.lastScan.status.toLowerCase()}</p>
            )}
            {view.data.lastScan
              && ["SUCCEEDED", "EMPTY"].includes(view.data.lastScan.status)
              && !view.data.lastScan.feedback && (
                <div className="feedback-actions" aria-label="Scan feedback">
                  <button className="button quiet" disabled={view.feedbackState === "loading"} onClick={() => recordFeedback("USEFUL")}>Useful</button>
                  <button className="button quiet" disabled={view.feedbackState === "loading"} onClick={() => recordFeedback("NOT_USEFUL")}>Not useful</button>
                </div>
              )}
            {view.data.lastScan?.feedback && (
              <p className="success compact" role="status">Feedback recorded: {view.data.lastScan.feedback === "USEFUL" ? "useful" : "not useful"}.</p>
            )}
            {view.feedbackState === "error" && <p className="error compact" role="alert">{view.feedbackError}</p>}
          </article>
          <article className="summary-card">
            <p className="card-kicker">Workspace</p>
            <h2>Runtime {view.data.workspace.runtimeState}</h2>
            <p>{view.data.workspace.fileCount} files · {view.data.workspace.draftCount} local drafts</p>
            {view.data.workspace.workspaceReceipt && (
              <p className="receipt-chip">{view.data.workspace.workspaceReceipt.generation}</p>
            )}
            <a className="text-link" href="/workspace">Open workspace</a>
          </article>
          <article className="summary-card">
            <p className="card-kicker">Portability</p>
            <h2>Deterministic export</h2>
            <p>Unencrypted ZIP · workspace, memory, schedules, and receipts</p>
            <a className="text-link" href="/export">Review export</a>
          </article>
          <article className="summary-card">
            <p className="card-kicker">Deletion</p>
            <h2>Permanent account removal</h2>
            <p>Two-pass deletion with a minimum {view.data.deletion.minimumReconciliationMinutes}-minute reconciliation window.</p>
            <a className="text-link" href="/delete">Review deletion</a>
          </article>
        </div>
      )}
    </Shell>
  );
}

function connectionLabel(status) {
  return {
    CONNECTED: "Gmail connected",
    REAUTH_REQUIRED: "Gmail needs reconnection",
    DISCONNECTED: "Gmail disconnected",
  }[status] || "Connection unavailable";
}

function scanLabel(scan) {
  if (!scan) return "No scan yet";
  return {
    RUNNING: "Scan in progress",
    SUCCEEDED: "Scan complete",
    EMPTY: "Nothing waiting",
    FAILED: scan.failureCode === "AUTHORIZATION" ? "Reconnect Gmail to scan" : "Scan needs a retry",
  }[scan.status] || "Scan unavailable";
}

function ConnectionsContent({ connection, onDisconnect, state }) {
  const status = connection?.status;
  return (
    <div className="stack">
      <p className="lede">Connect data sources here. The trusted control plane holds access; the Linux workspace receives only bounded results.</p>
      <article className="connection-card">
        <div className="provider-mark" aria-hidden="true">G</div>
        <div>
          <h2>Gmail</h2>
          <p>Read-only pilot · {connectionLabel(status)}</p>
        </div>
        {status === "CONNECTED" ? (
          <button className="button quiet" disabled={state === "loading"} onClick={onDisconnect}>Disconnect Gmail</button>
        ) : (
          <a className="button primary" href="/oauth/google/start">
            {status === "REAUTH_REQUIRED" ? "Reconnect Gmail" : "Connect read-only Gmail"}
          </a>
        )}
      </article>
    </div>
  );
}

function ConnectionsPage() {
  const [view, setView] = useState({ state: "loading" });
  useEffect(() => {
    api("/api/overview")
      .then(({ payload }) => setView({ state: "ready", data: assertReadOnlyOverview(payload) }))
      .catch((error) => setView({ state: "error", error: error.message }));
  }, []);
  async function disconnect() {
    setView((current) => ({ ...current, actionState: "loading" }));
    try {
      await api("/api/connections/google-gmail-readonly/disconnect", {
        method: "POST", body: {}, csrf: true,
      });
      setView((current) => ({
        ...current,
        actionState: "done",
        data: { ...current.data, connection: { ...current.data.connection, status: "DISCONNECTED" } },
      }));
    } catch (error) {
      setView((current) => ({ ...current, actionState: "error", actionError: error.message }));
    }
  }
  return (
    <Shell eyebrow="Connections" title="Bring your context. Keep your keys.">
      <Status state={view.state}>{view.error}</Status>
      {view.state === "ready" && (
        <ConnectionsContent connection={view.data.connection} onDisconnect={disconnect} state={view.actionState} />
      )}
      {view.actionState === "error" && <p className="error" role="alert">{view.actionError}</p>}
    </Shell>
  );
}

function ApprovalPage({ token }) {
  const [view, setView] = useState({ state: "loading" });
  useEffect(() => {
    api(`/approve/${encodeURIComponent(token)}`)
      .then(({ payload }) => setView({ state: "ready", action: payload }))
      .catch((error) => setView({ state: "error", error: error.message }));
  }, [token]);
  async function decide(verb) {
    setView((current) => ({ ...current, state: "loading" }));
    try {
      const action = view.action;
      const body = verb === "approve"
        ? { token, revision: action.revision, args: action.args }
        : { revision: action.revision };
      const { payload } = await api(`/api/actions/${action.actionId}/${verb}`, {
        method: "POST", body, csrf: true,
      });
      setView({ state: "decided", decision: payload.state });
    } catch (error) {
      setView({ state: "error", error: error.message });
    }
  }
  return (
    <Shell eyebrow="Exact approval" title="Review what will happen.">
      <Status state={view.state}>{view.error}</Status>
      {view.state === "ready" && (
        <article className="approval-card">
          <div className="receipt-row"><span>To</span><strong>{view.action.args.to}</strong></div>
          <div className="receipt-row"><span>Subject</span><strong>{view.action.args.subject}</strong></div>
          <div className="message-preview"><p>{view.action.args.body}</p></div>
          <p className="hash">Payload {view.action.payloadHash}</p>
          <div className="actions">
            <button className="button primary" onClick={() => decide("approve")}>Approve exact email</button>
            <button className="button quiet" onClick={() => decide("reject")}>Reject</button>
          </div>
        </article>
      )}
      {view.state === "decided" && <p className="success" role="status">Decision recorded: {view.decision}</p>}
    </Shell>
  );
}

function WorkspacePage() {
  const selectedDraftId = useMemo(
    () => new URLSearchParams(location.search).get("draft"),
    [],
  );
  const [view, setView] = useState({ state: "loading" });
  useEffect(() => {
    Promise.all([api("/api/workspace"), api("/api/gmail")])
      .then(([workspace, gmail]) => setView({
        state: "ready",
        data: workspace.payload,
        gmail: gmail.payload,
      }))
      .catch((error) => setView({ state: "error", error: error.message }));
  }, []);
  return (
    <Shell eyebrow="Portable state" title="Your private workspace.">
      <Status state={view.state}>{view.error}</Status>
      {view.state === "ready" && (
        <div className="workspace-sections">
          <section aria-labelledby="gmail-drafts-heading">
            <h2 id="gmail-drafts-heading">Private Gmail drafts</h2>
            <p className="lede">Editing saves a new local revision. It never sends email.</p>
            {view.gmail.drafts.length === 0 && <p>No prepared drafts yet. Use Prepare or Edit after a Telegram scan.</p>}
            <div className="draft-list">
              {orderedDrafts(view.gmail.drafts, selectedDraftId).map((draft) => (
                <DraftEditor key={draft.actionId} initialDraft={draft} selected={draft.actionId === selectedDraftId} />
              ))}
            </div>
          </section>
          {view.gmail.opportunities.length > 0 && (
            <section aria-labelledby="opportunities-heading">
              <h2 id="opportunities-heading">Current follow-ups</h2>
              <div className="source-card-list">
                {view.gmail.opportunities.map((opportunity) => (
                  <article className="source-card" key={opportunity.id}>
                    <p className="card-kicker">Source-backed follow-up</p>
                    <h3>{opportunity.title}</h3>
                    <p>{opportunity.reason}</p>
                    <dl>
                      <div><dt>Contact</dt><dd>{opportunity.correspondent}</dd></div>
                      <div><dt>Subject</dt><dd>{opportunity.subject || "No subject"}</dd></div>
                      <div><dt>Waiting since</dt><dd>{opportunity.waitingSince}</dd></div>
                    </dl>
                    <a className="button quiet" href={opportunity.sourceUrl} rel="noreferrer">Open source in Gmail</a>
                  </article>
                ))}
              </div>
            </section>
          )}
          <section aria-labelledby="files-heading">
            <h2 id="files-heading">Workspace files</h2>
            {view.data.files.length === 0 && <p>No authored files yet.</p>}
            <ul className="file-list">
              {view.data.files.map((file) => (
                <li key={file.path}>
                  <span>{file.path}</span>
                  <span>{formatBytes(file.size)}</span>
                </li>
              ))}
            </ul>
          </section>
        </div>
      )}
    </Shell>
  );
}

function orderedDrafts(drafts, selectedDraftId) {
  if (!selectedDraftId) return drafts;
  return [...drafts].sort((left, right) => (
    Number(right.actionId === selectedDraftId) - Number(left.actionId === selectedDraftId)
  ));
}

function DraftEditor({ initialDraft, selected }) {
  const [draft, setDraft] = useState(initialDraft);
  const [subject, setSubject] = useState(initialDraft.subject);
  const [body, setBody] = useState(initialDraft.body);
  const [state, setState] = useState("ready");
  const [message, setMessage] = useState("");
  const field = draft.actionId;

  async function save(event) {
    event.preventDefault();
    setState("loading");
    setMessage("");
    try {
      const { payload } = await api(`/api/gmail/drafts/${encodeURIComponent(draft.actionId)}`, {
        method: "POST",
        body: { revision: draft.revision, subject, body },
        csrf: true,
      });
      setDraft(payload.draft);
      setSubject(payload.draft.subject);
      setBody(payload.draft.body);
      setMessage(`Saved revision ${payload.draft.revision}. Nothing was sent.`);
      setState("done");
    } catch (error) {
      setMessage(error.message);
      setState("error");
    }
  }

  return (
    <form className={`draft-card${selected ? " selected" : ""}`} onSubmit={save}>
      <div className="receipt-row"><span>To</span><strong>{draft.to}</strong></div>
      <label htmlFor={`subject-${field}`}>Subject</label>
      <input id={`subject-${field}`} value={subject} maxLength={200} required onChange={(event) => setSubject(event.target.value)} />
      <label htmlFor={`body-${field}`}>Body</label>
      <textarea id={`body-${field}`} value={body} required onChange={(event) => setBody(event.target.value)} />
      <p className="hash">Revision {draft.revision} · Payload {draft.payloadHash}</p>
      <button className="button primary" disabled={state === "loading"} type="submit">Save new revision</button>
      {state === "loading" && <p role="status">Saving local revision…</p>}
      {state === "done" && <p className="success" role="status">{message}</p>}
      {state === "error" && <p className="error" role="alert">{message}</p>}
    </form>
  );
}

function formatBytes(value) {
  if (value < 1024) return `${value} B`;
  return `${(value / 1024).toFixed(1)} KB`;
}

function ExportPage() {
  const [state, setState] = useState("ready");
  async function download() {
    setState("loading");
    try {
      const response = await fetch("/api/export", { credentials: "same-origin" });
      if (!response.ok) throw new Error("Export could not be created.");
      const url = URL.createObjectURL(await response.blob());
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = "personal-operator-export.zip";
      anchor.click();
      URL.revokeObjectURL(url);
      setState("done");
    } catch {
      setState("error");
    }
  }
  return (
    <Shell eyebrow="Portability" title="Take your operator with you.">
      <p className="lede">Create a deterministic, unencrypted ZIP containing your authored files, memory, schedules, and receipts. Credentials are never included; store the download securely.</p>
      <button className="button primary" disabled={state === "loading"} onClick={download}>Create unencrypted ZIP</button>
      {state === "loading" && <p role="status">Building export…</p>}
      {state === "done" && <p className="success" role="status">Export created.</p>}
      {state === "error" && <p className="error" role="alert">Export could not be created.</p>}
    </Shell>
  );
}

function DeletePage() {
  const [confirmation, setConfirmation] = useState("");
  const [state, setState] = useState("ready");
  async function remove() {
    setState("loading");
    try {
      const { payload } = await api("/api/delete", {
        method: "POST", body: { confirm: confirmation }, csrf: true,
      });
      setState(payload.status === "deleted" ? "done" : "pending");
    } catch {
      setState("error");
    }
  }
  return (
    <Shell eyebrow="Danger zone" title="Delete your operator.">
      <p className="lede">Your active application data is removed in two passes with a minimum 30-minute reconciliation window. The first pass immediately blocks new work, revokes local connections, and stops your runtime.</p>
      <p>Already queued Telegram updates, logs, and backups age out under their retention periods. Local read-only Google records and credentials are removed. Revoke the app separately in Google Account settings to end the provider grant.</p>
      <label htmlFor="confirmation">Type <strong>DELETE</strong> to continue</label>
      <input id="confirmation" value={confirmation} onChange={(event) => setConfirmation(event.target.value)} autoComplete="off" />
      <button className="button danger" disabled={confirmation !== "DELETE" || state === "loading"} onClick={remove}>Start permanent deletion</button>
      {state === "loading" && <p role="status">Revoking access…</p>}
      {state === "done" && <p className="success" role="status">Your operator was deleted.</p>}
      {state === "pending" && <p role="status">Deletion is safely pending runtime reconciliation.</p>}
      {state === "error" && <p className="error" role="alert">Deletion could not be completed yet.</p>}
    </Shell>
  );
}

function Route({ path }) {
  if (path.startsWith("/approve/")) return <ApprovalPage token={decodeURIComponent(path.slice(9))} />;
  if (path === "/connections") return <ConnectionsPage />;
  if (path === "/workspace") return <WorkspacePage />;
  if (path === "/export") return <ExportPage />;
  if (path === "/delete") return <DeletePage />;
  return <OverviewPage />;
}

export default function App() {
  const ticket = new URLSearchParams(location.search).get("ticket");
  if (ticket) return <TicketBootstrap />;
  return <Route path={location.pathname} />;
}
