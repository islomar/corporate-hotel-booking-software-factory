import { useEffect, useRef, useState, type FormEvent } from "react";
import { fetchHealth, fetchMe, type Health, type Me } from "./api";
import { loadAuthClient, type AuthClient, type SignedInUser } from "./auth";

type HealthState =
  | { kind: "loading" }
  | { kind: "ok"; health: Health }
  | { kind: "error"; message: string };

type AuthState =
  | { kind: "loading" }
  | { kind: "unavailable" }
  | { kind: "failed"; message: string }
  | { kind: "ready"; client: AuthClient; user: SignedInUser | null };

type Props = { loadAuth?: () => Promise<AuthClient | null> };

export default function App({ loadAuth = loadAuthClient }: Props) {
  const [health, setHealth] = useState<HealthState>({ kind: "loading" });
  const [auth, setAuth] = useState<AuthState>({ kind: "loading" });
  // Every API call asks the sign-in client for the ID token: none before it has loaded.
  const client = useRef<AuthClient | null>(null);

  useEffect(() => {
    fetchHealth(async () => (client.current ? client.current.getToken() : null))
      .then((value) => setHealth({ kind: "ok", health: value }))
      .catch((error: unknown) =>
        setHealth({ kind: "error", message: String(error) }),
      );
  }, []);

  useEffect(() => {
    // Strict Mode runs effects twice: the first run must not subscribe.
    const run = { cancelled: false, unsubscribe: () => {} };
    loadAuth()
      .then((loaded) => {
        if (run.cancelled) return;
        if (!loaded) {
          setAuth({ kind: "unavailable" });
          return;
        }
        client.current = loaded;
        run.unsubscribe = loaded.onChange((user) =>
          setAuth({ kind: "ready", client: loaded, user }),
        );
      })
      .catch((error: unknown) => {
        if (!run.cancelled) setAuth({ kind: "failed", message: String(error) });
      });
    return () => {
      run.cancelled = true;
      run.unsubscribe();
    };
  }, [loadAuth]);

  return (
    <main>
      <h1>Corporate Hotel Booking</h1>
      {health.kind === "loading" && <p>Checking the backend…</p>}
      {health.kind === "ok" && (
        <p role="status">
          Backend status: {health.health.status} (version{" "}
          {health.health.version})
        </p>
      )}
      {health.kind === "error" && <p role="alert">{health.message}</p>}
      {auth.kind === "ready" && auth.user ? (
        <Account client={auth.client} />
      ) : (
        <SignIn auth={auth} />
      )}
    </main>
  );
}

function SignIn({ auth }: { auth: AuthState }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);

  if (auth.kind !== "ready") {
    return (
      <section aria-labelledby="sign-in">
        <h2 id="sign-in">Sign in</h2>
        <p>You are signed out.</p>
        {auth.kind === "unavailable" && <p>Sign-in is not available yet.</p>}
        {auth.kind === "failed" && (
          <p role="alert">Sign-in could not load: {auth.message}</p>
        )}
      </section>
    );
  }

  const attempt = (action: () => Promise<void>) => {
    setError(null);
    action().catch((reason: unknown) => setError(String(reason)));
  };
  const submit = (event: FormEvent) => {
    event.preventDefault();
    attempt(() => auth.client.signInWithEmail(email, password));
  };

  return (
    <section aria-labelledby="sign-in">
      <h2 id="sign-in">Sign in</h2>
      <p>You are signed out.</p>
      <button
        type="button"
        onClick={() => attempt(() => auth.client.signInWithGoogle())}
      >
        Sign in with Google
      </button>
      <form onSubmit={submit}>
        <label>
          Email
          <input
            type="email"
            autoComplete="email"
            required
            value={email}
            onChange={(event) => setEmail(event.target.value)}
          />
        </label>
        <label>
          Password
          <input
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
        </label>
        <button type="submit">Sign in</button>
        <button
          type="button"
          onClick={() =>
            attempt(() => auth.client.signUpWithEmail(email, password))
          }
        >
          Create account
        </button>
      </form>
      {error && <p role="alert">{error}</p>}
    </section>
  );
}

function Account({ client }: { client: AuthClient }) {
  const [me, setMe] = useState<Me | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchMe(() => client.getToken())
      .then(setMe)
      .catch((reason: unknown) => setError(String(reason)));
  }, [client]);

  return (
    <section aria-labelledby="account">
      <h2 id="account">Account</h2>
      {me && <p>Signed in as {me.email ?? me.uid}</p>}
      {error && <p role="alert">{error}</p>}
      <button
        type="button"
        onClick={() =>
          client.signOut().catch((reason: unknown) => setError(String(reason)))
        }
      >
        Sign out
      </button>
    </section>
  );
}
