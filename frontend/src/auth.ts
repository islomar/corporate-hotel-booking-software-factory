import { fetchAuthConfig, type AuthConfig } from "./api";

export type SignedInUser = { email: string | null };

/** What the app needs from sign-in. The Firebase SDK implements it; tests use a fake. */
export type AuthClient = {
  onChange(listener: (user: SignedInUser | null) => void): () => void;
  getToken(): Promise<string | null>;
  signInWithGoogle(): Promise<void>;
  signInWithEmail(email: string, password: string): Promise<void>;
  signUpWithEmail(email: string, password: string): Promise<void>;
  signOut(): Promise<void>;
};

/**
 * Identity Platform through the Firebase JS SDK (ADR-50e4). Loaded only when configured.
 * `@firebase/app` and `@firebase/auth` are what `firebase/app` and `firebase/auth` re-export.
 * The `firebase` package would add about 110 lockfile entries (Firestore, Crashlytics, protobufjs)
 * that this app never imports.
 */
export async function firebaseAuthClient(
  config: AuthConfig,
): Promise<AuthClient> {
  const { initializeApp } = await import("@firebase/app");
  const {
    GoogleAuthProvider,
    createUserWithEmailAndPassword,
    getAuth,
    onAuthStateChanged,
    signInWithEmailAndPassword,
    signInWithPopup,
    signOut,
  } = await import("@firebase/auth");
  const auth = getAuth(initializeApp(config));
  return {
    onChange: (listener) =>
      onAuthStateChanged(auth, (user) =>
        listener(user ? { email: user.email } : null),
      ),
    // getIdToken refreshes the token when it is about to expire.
    getToken: async () =>
      auth.currentUser ? auth.currentUser.getIdToken() : null,
    // A popup, not a redirect: redirects lose the result when the auth domain
    // (firebaseapp.com) differs from the app's domain and the browser partitions storage.
    signInWithGoogle: async () => {
      await signInWithPopup(auth, new GoogleAuthProvider());
    },
    signInWithEmail: async (email, password) => {
      await signInWithEmailAndPassword(auth, email, password);
    },
    signUpWithEmail: async (email, password) => {
      await createUserWithEmailAndPassword(auth, email, password);
    },
    signOut: () => signOut(auth),
  };
}

/** Null while the backend has no web config: the app then runs signed out. */
export async function loadAuthClient(): Promise<AuthClient | null> {
  const config = await fetchAuthConfig();
  return config ? firebaseAuthClient(config) : null;
}
