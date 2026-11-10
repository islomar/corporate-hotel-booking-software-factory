import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import type { AuthClient, SignedInUser } from "./auth";

type Routes = Record<string, () => Response>;

function stubApi(routes: Routes) {
  const fetchMock = vi.fn((path: string) => {
    const route = routes[path];
    return Promise.resolve(route ? route() : new Response("", { status: 404 }));
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

const healthy: Routes = {
  "/api/health": () =>
    new Response(JSON.stringify({ status: "ok", version: "test" })),
};

class FakeAuth implements AuthClient {
  listener: (user: SignedInUser | null) => void = () => {};
  token: string | null = null;
  signInWithGoogle = vi.fn(async () => {});
  signInWithEmail = vi.fn(async (_email: string, _password: string) => {});
  signUpWithEmail = vi.fn(async (_email: string, _password: string) => {});
  signOut = vi.fn(async () => {});

  onChange(listener: (user: SignedInUser | null) => void) {
    this.listener = listener;
    listener(null);
    return () => {};
  }

  async getToken() {
    return this.token;
  }

  signIn(email: string, token: string) {
    this.token = token;
    act(() => this.listener({ email }));
  }
}

describe("App", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("shows the backend status when the health check succeeds", async () => {
    stubApi(healthy);

    render(<App loadAuth={async () => null} />);

    expect(await screen.findByRole("status")).toHaveTextContent(
      "Backend status: ok (version test)",
    );
  });

  it("shows an error when the health check fails", async () => {
    stubApi({ "/api/health": () => new Response("", { status: 503 }) });

    render(<App loadAuth={async () => null} />);

    expect(await screen.findByRole("alert")).toHaveTextContent("HTTP 503");
  });

  it("stays signed out while sign-in is not configured", async () => {
    stubApi(healthy);

    render(<App loadAuth={async () => null} />);

    expect(await screen.findByText("You are signed out.")).toBeVisible();
    expect(
      await screen.findByText("Sign-in is not available yet."),
    ).toBeVisible();
    expect(screen.queryByRole("button", { name: /Google/ })).toBeNull();
  });

  it("signs in with Google", async () => {
    stubApi(healthy);
    const auth = new FakeAuth();

    render(<App loadAuth={async () => auth} />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Sign in with Google" }),
    );

    expect(auth.signInWithGoogle).toHaveBeenCalledOnce();
  });

  it("signs in and signs up with email and password", async () => {
    stubApi(healthy);
    const auth = new FakeAuth();

    render(<App loadAuth={async () => auth} />);
    fireEvent.change(await screen.findByLabelText("Email"), {
      target: { value: "ada@example.com" },
    });
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "s3cret-pass" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    fireEvent.click(screen.getByRole("button", { name: "Create account" }));

    expect(auth.signInWithEmail).toHaveBeenCalledWith(
      "ada@example.com",
      "s3cret-pass",
    );
    expect(auth.signUpWithEmail).toHaveBeenCalledWith(
      "ada@example.com",
      "s3cret-pass",
    );
  });

  it("shows why a sign-in failed", async () => {
    stubApi(healthy);
    const auth = new FakeAuth();
    auth.signInWithGoogle.mockRejectedValue(
      new Error("auth/popup-closed-by-user"),
    );

    render(<App loadAuth={async () => auth} />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Sign in with Google" }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "auth/popup-closed-by-user",
    );
  });

  it("asks the API who is signed in, with the ID token, and signs out", async () => {
    const fetchMock = stubApi({
      ...healthy,
      "/api/me": () =>
        new Response(JSON.stringify({ uid: "u1", email: "ada@example.com" })),
    });
    const auth = new FakeAuth();

    render(<App loadAuth={async () => auth} />);
    await screen.findByRole("button", { name: "Sign in with Google" });
    auth.signIn("ada@example.com", "id-token");

    expect(
      await screen.findByText("Signed in as ada@example.com"),
    ).toBeVisible();
    const meCall = fetchMock.mock.calls.find(([path]) => path === "/api/me");
    const init = (meCall as unknown as [string, RequestInit])[1];
    expect(new Headers(init.headers).get("Authorization")).toBe(
      "Bearer id-token",
    );

    fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
    expect(auth.signOut).toHaveBeenCalledOnce();
  });
});
