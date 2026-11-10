import { afterEach, describe, expect, it, vi } from "vitest";

const firebase = vi.hoisted(() => ({
  auth: { currentUser: null as null | { getIdToken: () => Promise<string> } },
  initializeApp: vi.fn((config: unknown) => ({ config })),
}));

vi.mock("@firebase/app", () => ({ initializeApp: firebase.initializeApp }));
vi.mock("@firebase/auth", () => ({
  getAuth: () => firebase.auth,
  GoogleAuthProvider: class {},
  createUserWithEmailAndPassword: vi.fn(),
  onAuthStateChanged: vi.fn(),
  signInWithEmailAndPassword: vi.fn(),
  signInWithPopup: vi.fn(),
  signOut: vi.fn(),
}));

import { firebaseAuthClient, loadAuthClient } from "./auth";

const config = { apiKey: "k", authDomain: "d.example" };

describe("loadAuthClient", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("returns null while the backend has no web config", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response("", { status: 404 })),
    );

    expect(await loadAuthClient()).toBeNull();
  });

  it("starts Firebase with the backend's web config", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response(JSON.stringify(config))),
    );

    expect(await loadAuthClient()).not.toBeNull();
    expect(firebase.initializeApp).toHaveBeenCalledWith(config);
  });
});

describe("firebaseAuthClient.getToken", () => {
  afterEach(() => {
    firebase.auth.currentUser = null;
  });

  it("is null while nobody is signed in", async () => {
    const client = await firebaseAuthClient(config);

    expect(await client.getToken()).toBeNull();
  });

  it("is the signed-in user's ID token", async () => {
    firebase.auth.currentUser = { getIdToken: async () => "id-token" };
    const client = await firebaseAuthClient(config);

    expect(await client.getToken()).toBe("id-token");
  });
});
