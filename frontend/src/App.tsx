import { useEffect, useState } from "react";
import { fetchHealth, type Health } from "./api";

type State =
  | { kind: "loading" }
  | { kind: "ok"; health: Health }
  | { kind: "error"; message: string };

export default function App() {
  const [state, setState] = useState<State>({ kind: "loading" });

  useEffect(() => {
    fetchHealth()
      .then((health) => setState({ kind: "ok", health }))
      .catch((error: unknown) =>
        setState({ kind: "error", message: String(error) }),
      );
  }, []);

  return (
    <main>
      <h1>Corporate Hotel Booking</h1>
      {state.kind === "loading" && <p>Checking the backend…</p>}
      {state.kind === "ok" && (
        <p role="status">
          Backend status: {state.health.status} (version {state.health.version})
        </p>
      )}
      {state.kind === "error" && <p role="alert">{state.message}</p>}
    </main>
  );
}
