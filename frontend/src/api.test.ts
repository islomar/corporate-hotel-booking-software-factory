import { afterEach, describe, expect, it, vi } from "vitest";
import { apiFetch, fetchAuthConfig, fetchHealth, fetchMe } from "./api";

function stubFetch(response: Response) {
  const fetchMock = vi.fn().mockResolvedValue(response);
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function sentHeaders(fetchMock: ReturnType<typeof vi.fn>): Headers {
  const init = fetchMock.mock.calls[0][1] as RequestInit;
  return new Headers(init.headers);
}

describe("apiFetch", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("sends the ID token as a bearer token", async () => {
    const fetchMock = stubFetch(new Response("{}"));

    await apiFetch("/api/me", async () => "id-token");

    expect(fetchMock).toHaveBeenCalledWith("/api/me", expect.anything());
    expect(sentHeaders(fetchMock).get("Authorization")).toBe("Bearer id-token");
  });

  it("sends no Authorization header when nobody is signed in", async () => {
    const fetchMock = stubFetch(new Response("{}"));

    await apiFetch("/api/health", async () => null);

    expect(sentHeaders(fetchMock).has("Authorization")).toBe(false);
  });

  it("keeps the caller's headers", async () => {
    const fetchMock = stubFetch(new Response("{}"));

    await apiFetch("/api/me", async () => "t", {
      headers: { Accept: "application/json" },
    });

    expect(sentHeaders(fetchMock).get("Accept")).toBe("application/json");
    expect(sentHeaders(fetchMock).get("Authorization")).toBe("Bearer t");
  });
});

describe("API calls", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("fetchHealth sends the token too", async () => {
    const fetchMock = stubFetch(
      new Response(JSON.stringify({ status: "ok", version: "v" })),
    );

    expect(await fetchHealth(async () => "t")).toEqual({
      status: "ok",
      version: "v",
    });
    expect(sentHeaders(fetchMock).get("Authorization")).toBe("Bearer t");
  });

  it("fetchMe returns the signed-in user", async () => {
    stubFetch(
      new Response(JSON.stringify({ uid: "u1", email: "ada@example.com" })),
    );

    expect(await fetchMe(async () => "t")).toEqual({
      uid: "u1",
      email: "ada@example.com",
    });
  });

  it("fetchMe fails on 401", async () => {
    stubFetch(new Response("", { status: 401 }));

    await expect(fetchMe(async () => null)).rejects.toThrow("HTTP 401");
  });

  it("fetchAuthConfig returns the web config", async () => {
    stubFetch(
      new Response(JSON.stringify({ apiKey: "k", authDomain: "d.example" })),
    );

    expect(await fetchAuthConfig()).toEqual({
      apiKey: "k",
      authDomain: "d.example",
    });
  });

  it("fetchAuthConfig returns null while sign-in is not configured", async () => {
    stubFetch(new Response("", { status: 404 }));

    expect(await fetchAuthConfig()).toBeNull();
  });
});
