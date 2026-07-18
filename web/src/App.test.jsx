import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";

function response(payload, { status = 200, contentType = "application/json" } = {}) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    headers: new Headers({ "content-type": contentType }),
    json: async () => payload,
    text: async () => String(payload),
  });
}

beforeEach(() => {
  sessionStorage.clear();
  vi.stubGlobal("fetch", vi.fn());
});

afterEach(() => vi.unstubAllGlobals());

describe("consumer control surface", () => {
  it("bootstraps a one-time Telegram ticket and keeps CSRF only in session storage", async () => {
    history.replaceState({}, "", "/?ticket=one-time-ticket");
    fetch.mockReturnValue(response({ csrfToken: "x".repeat(43), expiresAt: 123 }));
    render(<App />);
    expect(screen.getByRole("status")).toHaveTextContent("Loading");
    await screen.findByRole("heading", { name: "Gmail" });
    expect(fetch).toHaveBeenCalledWith(
      "/api/session/connect",
      expect.objectContaining({ method: "POST", credentials: "same-origin" }),
    );
    expect(sessionStorage.getItem("personal-operator.csrf")).toBe("x".repeat(43));
    expect(localStorage.length).toBe(0);
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
    expect(screen.getByText("ada@example.com")).toBeInTheDocument();
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
    expect(screen.getByText(/7-day recovery window/i)).toBeInTheDocument();
    expect(screen.getByText(/Google Account settings/i)).toBeInTheDocument();
    const button = screen.getByRole("button", { name: "Start permanent deletion" });
    expect(button).toBeDisabled();
    fireEvent.change(screen.getByLabelText(/Type/), { target: { value: "DELETE" } });
    expect(button).toBeEnabled();
    fetch.mockReturnValue(response({ error: "not now" }, { status: 409 }));
    fireEvent.click(button);
    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
  });
});
