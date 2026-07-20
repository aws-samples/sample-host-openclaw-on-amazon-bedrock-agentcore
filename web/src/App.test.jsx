import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";

const styles = readFileSync("src/styles.css", "utf8");

function response(payload, { status = 200, contentType = "application/json" } = {}) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    headers: new Headers({ "content-type": contentType }),
    json: async () => payload,
    text: async () => String(payload),
  });
}

function overview(overrides = {}) {
  return {
    version: "personal-operator.pilot-overview.v1",
    externalEffects: false,
    connection: {
      provider: "google-gmail-readonly",
      status: "CONNECTED",
      access: "READ_ONLY",
    },
    lastScan: {
      scanId: `scan_00000000001700000000_${"s".repeat(32)}`,
      status: "SUCCEEDED",
      startedAt: 1700000000,
      completedAt: 1700000012,
      resultCount: 2,
      failureCode: null,
      feedback: null,
    },
    workspace: {
      runtimeState: "IDLE",
      workspaceReceipt: {
        generation: "gen_1234567890abcdef",
        manifestSha256: "a".repeat(64),
      },
      fileCount: 2,
      opportunityCount: 2,
      draftCount: 1,
    },
    capability: {
      provider: "google-gmail-readonly",
      mode: "READ_ONLY",
      externalEffects: false,
    },
    export: {
      format: "ZIP",
      encrypted: false,
      deterministic: true,
      includes: ["memory", "receipts", "schedules", "workspace"],
    },
    deletion: { status: "AVAILABLE", minimumReconciliationMinutes: 30 },
    ...overrides,
  };
}

beforeEach(() => {
  sessionStorage.clear();
  vi.stubGlobal("fetch", vi.fn());
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("consumer control surface", () => {
  it("bootstraps a one-time Telegram ticket and keeps CSRF only in session storage", async () => {
    history.replaceState({}, "", "/?ticket=one-time-ticket");
    fetch
      .mockReturnValueOnce(response({
        csrfToken: "x".repeat(43), expiresAt: 123, returnPath: "/connections",
      }))
      .mockReturnValueOnce(response(overview({
        connection: {
          provider: "google-gmail-readonly", status: "DISCONNECTED", access: "READ_ONLY",
        },
      })));
    render(<App />);
    expect(screen.getByRole("status")).toHaveTextContent("Loading");
    await screen.findByRole("heading", { name: "Gmail" });
    expect(fetch).toHaveBeenCalledWith(
      "/api/session/connect",
      expect.objectContaining({ method: "POST", credentials: "same-origin" }),
    );
    expect(sessionStorage.getItem("personal-operator.csrf")).toBe("x".repeat(43));
    expect(localStorage.length).toBe(0);
    expect(location.pathname).toBe("/connections");
  });

  it("renders the read-only overview, runtime receipt, mobile navigation, and feedback", async () => {
    history.replaceState({}, "", "/");
    sessionStorage.setItem("personal-operator.csrf", "c".repeat(43));
    fetch
      .mockReturnValueOnce(response(overview()))
      .mockReturnValueOnce(response({
        scanId: `scan_00000000001700000000_${"s".repeat(32)}`,
        feedback: "USEFUL",
      }));

    render(<App />);

    expect(await screen.findByRole("heading", { name: /read-only operator/i })).toBeInTheDocument();
    expect(screen.getByText(/External effects off/i)).toBeInTheDocument();
    expect(screen.getByText(/2 follow-ups/i)).toBeInTheDocument();
    expect(screen.getByText(/Runtime IDLE/i)).toBeInTheDocument();
    expect(screen.getByText(/gen_1234567890abcdef/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /send|approve/i })).not.toBeInTheDocument();
    const navigation = screen.getByRole("navigation", { name: "Primary" });
    for (const name of ["Overview", "Connections", "Workspace", "Export", "Delete"]) {
      expect(navigation).toHaveTextContent(name);
    }

    fireEvent.click(screen.getByRole("button", { name: "Useful" }));
    await screen.findByText(/Feedback recorded: useful/i);
    expect(fetch.mock.calls[1][0]).toContain("/api/scans/scan_");
    expect(JSON.parse(fetch.mock.calls[1][1].body)).toEqual({ response: "USEFUL" });
    expect(fetch.mock.calls[1][1].headers["X-PO-CSRF"]).toBe("c".repeat(43));
  });

  it("keeps primary navigation reachable at the mobile breakpoint", () => {
    expect(styles).not.toContain(".nav-links { display: none; }");
    expect(styles).toContain("overflow-x: auto");
    expect(styles).toContain(".overview-grid { grid-template-columns: 1fr; }");
  });

  it("shows reconnect state and disconnects a connected account without provider content", async () => {
    history.replaceState({}, "", "/connections");
    sessionStorage.setItem("personal-operator.csrf", "c".repeat(43));
    fetch.mockReturnValueOnce(response(overview({
      connection: {
        provider: "google-gmail-readonly", status: "REAUTH_REQUIRED", access: "READ_ONLY",
      },
    })));
    const first = render(<App />);
    expect(await screen.findByRole("link", { name: /Reconnect Gmail/i })).toBeInTheDocument();
    first.unmount();

    fetch
      .mockReturnValueOnce(response(overview()))
      .mockReturnValueOnce(response({
        provider: "google-gmail-readonly", status: "DISCONNECTED",
      }));
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: /Disconnect Gmail/i }));
    await screen.findByRole("link", { name: /Connect read-only Gmail/i });
    expect(fetch.mock.calls.at(-1)[0]).toBe(
      "/api/connections/google-gmail-readonly/disconnect",
    );
    expect(fetch.mock.calls.at(-1)[1].headers["X-PO-CSRF"]).toBe("c".repeat(43));
  });

  it("keeps disconnect pending on 202 DISCONNECTING and completes it on retry", async () => {
    history.replaceState({}, "", "/connections");
    sessionStorage.setItem("personal-operator.csrf", "c".repeat(43));
    fetch
      .mockReturnValueOnce(response(overview()))
      .mockReturnValueOnce(response(
        { provider: "google-gmail-readonly", status: "DISCONNECTING", remoteGrantRevoked: false },
        { status: 202 },
      ))
      .mockReturnValueOnce(response(
        { provider: "google-gmail-readonly", status: "DISCONNECTED", remoteGrantRevoked: false },
        { status: 200 },
      ));
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: /Disconnect Gmail/i }));

    // The bounded purge needed two passes; the UI must not present the account
    // as disconnected until the server confirms DISCONNECTED.
    await screen.findByRole("link", { name: /Connect read-only Gmail/i });
    const disconnectCalls = fetch.mock.calls.filter(
      ([path]) => path === "/api/connections/google-gmail-readonly/disconnect",
    );
    expect(disconnectCalls).toHaveLength(2);
  });

  it("fails closed if the pilot overview ever enables external effects", async () => {
    history.replaceState({}, "", "/");
    fetch.mockReturnValue(response(overview({ externalEffects: true })));

    render(<App />);

    expect(await screen.findByRole("alert")).toHaveTextContent(/read-only boundary/i);
    expect(screen.queryByRole("button", { name: /send|approve/i })).not.toBeInTheDocument();
  });

  it("renders exact approval data and sends a CSRF-protected decision only on click", async () => {
    history.replaceState({}, "", "/approve/signed-token");
    sessionStorage.setItem("personal-operator.csrf", "c".repeat(43));
    fetch
      .mockReturnValueOnce(response({
        actionId: "action_12345678", revision: 2, payloadHash: "a".repeat(64),
        args: { to: "ada@example.com", subject: "Follow up", body: "Hello Ada" },
      }))
      .mockReturnValueOnce(response({ state: "APPROVED" }));
    render(<App />);
    await screen.findByText("Hello Ada");
    expect(fetch).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "Approve exact email" }));
    await screen.findByText(/Decision recorded: APPROVED/);
    expect(fetch.mock.calls[1][1].headers["X-PO-CSRF"]).toBe("c".repeat(43));
  });

  it("renders the bounded workspace file schema returned by the trusted API", async () => {
    history.replaceState({}, "", "/workspace");
    fetch.mockImplementation((path) => response(path === "/api/workspace" ? {
        userId: "user_founder",
        runtimeState: "IDLE",
        workspaceReceipt: null,
        files: [
          { path: "memory.md", size: 12 },
          { path: "notes/plan.md", size: 2048 },
        ],
      } : { userId: "user_founder", opportunities: [], drafts: [] }));

    render(<App />);

    expect(await screen.findByText("memory.md")).toBeInTheDocument();
    expect(screen.getByText("12 B")).toBeInTheDocument();
    expect(screen.getByText("notes/plan.md")).toBeInTheDocument();
    expect(screen.getByText("2.0 KB")).toBeInTheDocument();
  });

  it("reviews and edits an immutable local Gmail draft without exposing send", async () => {
    history.replaceState({}, "", "/workspace?draft=draft_action_12345678");
    sessionStorage.setItem("personal-operator.csrf", "c".repeat(43));
    fetch.mockImplementation((path, options = {}) => {
      if (path === "/api/workspace") {
        return response({
          userId: "user_founder", runtimeState: "IDLE",
          workspaceReceipt: null, files: [],
        });
      }
      if (path === "/api/gmail") {
        return response({
          userId: "user_founder",
          opportunities: [{
            id: "opportunity_123", title: "Follow up with Ada",
            reason: "You wrote last week and have not received a reply.",
            waitingSince: "2026-07-10T10:00:00+00:00",
            sourceUrl: "https://mail.google.com/mail/u/0/#inbox/thread-1",
            correspondent: "ada@example.com", subject: "Project update",
            confidence: 0.9,
          }],
          drafts: [{
            actionId: "draft_action_12345678", revision: 1,
            to: "ada@example.com", subject: "Following up",
            body: "Hi Ada, just following up.", payloadHash: "a".repeat(64),
          }],
        });
      }
      if (path === "/api/gmail/drafts/draft_action_12345678" && options.method === "POST") {
        return response({ draft: {
          actionId: "draft_action_12345678", revision: 2,
          to: "ada@example.com", subject: "Updated subject",
          body: "Updated body", payloadHash: "b".repeat(64),
        } });
      }
      throw new Error(`unexpected request: ${path}`);
    });

    render(<App />);

    expect(await screen.findByDisplayValue("Following up")).toBeInTheDocument();
    expect(screen.getAllByText("ada@example.com")).toHaveLength(2);
    expect(screen.getByRole("link", { name: /Open source in Gmail/i })).toHaveAttribute(
      "href", "https://mail.google.com/mail/u/0/#inbox/thread-1",
    );
    expect(screen.queryByRole("button", { name: /send/i })).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Subject"), { target: { value: "Updated subject" } });
    fireEvent.change(screen.getByLabelText("Body"), { target: { value: "Updated body" } });
    fireEvent.click(screen.getByRole("button", { name: "Save new revision" }));

    await screen.findByText("Saved revision 2. Nothing was sent.");
    const edit = fetch.mock.calls.find(([path]) => path.includes("/api/gmail/drafts/"));
    expect(edit[1].headers["X-PO-CSRF"]).toBe("c".repeat(43));
    expect(JSON.parse(edit[1].body)).toEqual({
      revision: 1,
      subject: "Updated subject",
      body: "Updated body",
    });
  });

  it("exposes accessible error state and requires typed deletion confirmation", async () => {
    history.replaceState({}, "", "/delete");
    render(<App />);
    expect(screen.getByText(/active application data is removed in two passes/i)).toBeInTheDocument();
    expect(screen.getByText(/minimum 30-minute reconciliation/i)).toBeInTheDocument();
    expect(screen.getByText(/Google Account settings/i)).toBeInTheDocument();
    const button = screen.getByRole("button", { name: "Start permanent deletion" });
    expect(button).toBeDisabled();
    fireEvent.change(screen.getByLabelText(/Type/), { target: { value: "DELETE" } });
    expect(button).toBeEnabled();
    fetch.mockReturnValue(response({ error: "not now" }, { status: 409 }));
    fireEvent.click(button);
    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
  });

  it("describes the deterministic export truthfully as an unencrypted ZIP", () => {
    history.replaceState({}, "", "/export");

    render(<App />);

    expect(screen.getByText(/deterministic, unencrypted ZIP/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Create unencrypted ZIP/i })).toBeInTheDocument();
    expect(screen.queryByText(/encrypted-ready/i)).not.toBeInTheDocument();
  });

  it("previews a dry-run import plan and gates activation on the exact bundle hash", async () => {
    history.replaceState({}, "", "/import");
    sessionStorage.setItem("personal-operator.csrf", "c".repeat(43));
    const bundleHash = "a".repeat(64);
    const planId = `importplan_${"b".repeat(64)}`;
    const baseGeneration = "generation_00000000000000000000";
    fetch.mockImplementation((path) => {
      if (path === "/api/import/plan") {
        return response({
          bundleHash,
          planId,
          baseGeneration,
          objectCount: 7,
          totalBytes: 1024,
          schedulesDisabled: true,
          connectorsDisconnected: true,
          effectsReplayable: false,
        });
      }
      if (path === "/api/import/activate") {
        return response({
          state: "ACTIVATED",
          activatedGeneration: "generation_00000000000000000001",
          bundleHash,
          planId,
        });
      }
      throw new Error(`unexpected request: ${path}`);
    });

    render(<App />);
    expect(screen.getByText(/dry-run-first transfer/i)).toBeInTheDocument();

    const file = new File([new Uint8Array([80, 75, 3, 4, 9, 9])], "bundle.zip", {
      type: "application/zip",
    });
    fireEvent.change(screen.getByLabelText(/Choose a portable bundle/i), {
      target: { files: [file] },
    });
    fireEvent.click(await screen.findByRole("button", { name: /Preview import/i }));

    await screen.findByText(/Dry-run plan/i);
    expect(screen.getAllByText(bundleHash, { exact: false }).length).toBeGreaterThan(0);
    expect(screen.getByText(/Portable objects: 7/)).toBeInTheDocument();
    expect(screen.getByText(/Total content: 1.0 KB/)).toBeInTheDocument();
    expect(screen.getByText(/Schedules land/i, { selector: "li" })).toHaveTextContent("DISABLED");
    expect(screen.getByText(/Connector descriptors land/i, { selector: "li" })).toHaveTextContent("DISCONNECTED");
    expect(screen.getByText(/Past effects land/i, { selector: "li" })).toHaveTextContent("non-replayable");

    const activate = screen.getByRole("button", { name: "Activate import" });
    expect(activate).toBeDisabled();
    fireEvent.click(screen.getByRole("checkbox"));
    expect(activate).toBeEnabled();

    fireEvent.click(activate);
    await screen.findByText(/Import activated/i);

    const planCall = fetch.mock.calls.find((call) => call[0] === "/api/import/plan");
    expect(planCall[1].headers["X-PO-CSRF"]).toBe("c".repeat(43));
    const activateCall = fetch.mock.calls.find((call) => call[0] === "/api/import/activate");
    expect(JSON.parse(activateCall[1].body)).toEqual({
      bundle: btoa(String.fromCharCode(80, 75, 3, 4, 9, 9)),
      bundleHash,
      planId,
      baseGeneration,
      confirm: true,
    });
  });

  it("logs out with replacement navigation and removes private workspace state from the DOM", async () => {
    history.replaceState({}, "", "/workspace?draft=draft_action_12345678");
    sessionStorage.setItem("personal-operator.csrf", "c".repeat(43));
    fetch.mockImplementation((path) => {
      if (path === "/api/workspace") {
        return response({
          userId: "user_founder", runtimeState: "IDLE",
          workspaceReceipt: null, files: [{ path: "memory.md", size: 12 }],
        });
      }
      if (path === "/api/gmail") {
        return response({
          userId: "user_founder",
          opportunities: [{
            id: "opportunity_123", title: "Follow up with Ada",
            reason: "Ada is waiting.", waitingSince: "2026-07-10T10:00:00+00:00",
            sourceUrl: "https://mail.google.com/mail/u/0/#inbox/thread-1",
            correspondent: "ada@example.com", subject: "Project update",
            confidence: 0.9,
          }],
          drafts: [{
            actionId: "draft_action_12345678", revision: 1,
            to: "ada@example.com", subject: "Following up",
            body: "Hi Ada, just following up.", payloadHash: "a".repeat(64),
          }],
        });
      }
      if (path === "/api/session/logout") return response({}, { status: 204 });
      throw new Error(`unexpected request: ${path}`);
    });
    const replaceDocument = vi.fn();
    render(<App replaceDocument={replaceDocument} />);
    await screen.findByDisplayValue("Hi Ada, just following up.");
    expect(screen.getAllByText("ada@example.com")).toHaveLength(2);
    expect(screen.getByText("memory.md")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Sign out" }));

    await screen.findByRole("heading", { name: "Signed out" });
    expect(sessionStorage.getItem("personal-operator.csrf")).toBeNull();
    expect(fetch.mock.calls.at(-1)[0]).toBe("/api/session/logout");
    expect(fetch.mock.calls.at(-1)[1].headers["X-PO-CSRF"]).toBe("c".repeat(43));
    expect(replaceDocument).toHaveBeenCalledWith("/signed-out");
    expect(screen.queryByText("ada@example.com")).not.toBeInTheDocument();
    expect(screen.queryByDisplayValue("Hi Ada, just following up.")).not.toBeInTheDocument();
    expect(screen.queryByText("memory.md")).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Open source in Gmail/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "Primary" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Sign out" })).not.toBeInTheDocument();
  });
});
