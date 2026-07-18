import { useEffect, useMemo, useState } from "react";
import { api, rememberCsrf } from "./api";

function Shell({ eyebrow, title, children }) {
  return (
    <main className="shell">
      <nav aria-label="Primary">
        <a className="brand" href="/">PO<span>.</span></a>
        <div className="nav-links">
          <a href="/connections">Connections</a>
          <a href="/workspace">Workspace</a>
          <a href="/export">Export</a>
          <a href="/delete">Delete</a>
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

function ConnectPage() {
  const ticket = useMemo(() => new URLSearchParams(location.search).get("ticket"), []);
  const [state, setState] = useState(ticket ? "loading" : "missing");
  const [error, setError] = useState("");
  useEffect(() => {
    if (!ticket) return;
    api("/api/session/connect", { method: "POST", body: { ticket } })
      .then(({ payload }) => {
        rememberCsrf(payload.csrfToken);
        history.replaceState({}, "", "/connections");
        setState("ready");
      })
      .catch((failure) => {
        setError(failure.message);
        setState("error");
      });
  }, [ticket]);
  return (
    <Shell eyebrow="Private control plane" title="Your operator, under your control.">
      <Status state={state}>
        {state === "missing" && <p>Open the one-time connection link from your Telegram conversation.</p>}
        {state === "ready" && <ConnectionsContent />}
        {state === "error" && error}
      </Status>
    </Shell>
  );
}

function ConnectionsContent() {
  return (
    <div className="stack">
      <p className="lede">Connect data sources here. The trusted control plane holds access; the Linux workspace receives only bounded results.</p>
      <article className="connection-card">
        <div className="provider-mark" aria-hidden="true">G</div>
        <div><h2>Gmail</h2><p>Read-only pilot · finds unanswered follow-ups</p></div>
        <a className="button primary" href="/oauth/google/start">Connect safely</a>
      </article>
    </div>
  );
}

function ConnectionsPage() {
  return <Shell eyebrow="Connections" title="Bring your context. Keep your keys."><ConnectionsContent /></Shell>;
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
              <ul className="opportunity-list">
                {view.gmail.opportunities.map((opportunity) => (
                  <li key={opportunity.id}>
                    <a href={opportunity.sourceUrl} rel="noreferrer">{opportunity.title}</a>
                    <p>{opportunity.reason}</p>
                  </li>
                ))}
              </ul>
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
      <p className="lede">Export your authored files, memory, schedules, and effect receipts. Credentials are never included.</p>
      <button className="button primary" disabled={state === "loading"} onClick={download}>Create encrypted-ready ZIP</button>
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
      <p className="lede">Your active application data is removed in two passes, at least 15 minutes apart. The first pass immediately blocks new work, revokes local connections, and stops your runtime.</p>
      <p>Already queued Telegram updates, logs, and backups age out under their retention periods. Read-only Google records are removed. For the configured founder, the send credential is disabled and scheduled with a 7-day recovery window. Revoke the app separately in Google Account settings to end the provider grant.</p>
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

export default function App() {
  const path = location.pathname;
  if (path.startsWith("/approve/")) return <ApprovalPage token={decodeURIComponent(path.slice(9))} />;
  if (path === "/connections") return <ConnectionsPage />;
  if (path === "/workspace") return <WorkspacePage />;
  if (path === "/export") return <ExportPage />;
  if (path === "/delete") return <DeletePage />;
  return <ConnectPage />;
}
