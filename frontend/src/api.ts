export type Health = { status: string; version: string };
export type Me = { uid: string; email: string | null; email_verified: boolean };
export type AuthConfig = { apiKey: string; authDomain: string };

/** Returns the current ID token, or null when nobody is signed in. */
export type TokenSource = () => Promise<string | null>;

/** Every API call goes through here: it adds the ID token when someone is signed in. */
export async function apiFetch(
  path: string,
  getToken: TokenSource,
  init: RequestInit = {},
): Promise<Response> {
  const headers = new Headers(init.headers);
  const token = await getToken();
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  return fetch(path, { ...init, headers });
}

async function getJson<T>(path: string, getToken: TokenSource): Promise<T> {
  const response = await apiFetch(path, getToken);
  if (!response.ok) {
    throw new Error(`${path} failed with HTTP ${response.status}`);
  }
  return (await response.json()) as T;
}

export function fetchHealth(getToken: TokenSource): Promise<Health> {
  return getJson<Health>("/api/health", getToken);
}

export function fetchMe(getToken: TokenSource): Promise<Me> {
  return getJson<Me>("/api/me", getToken);
}

/** The public Firebase web config. Null until Terraform has set it up. */
export async function fetchAuthConfig(): Promise<AuthConfig | null> {
  const response = await fetch("/auth-config.json");
  if (!response.ok) {
    return null;
  }
  return (await response.json()) as AuthConfig;
}
