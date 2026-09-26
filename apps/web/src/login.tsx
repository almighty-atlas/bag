import { useState } from "preact/hooks";

import { ApiError, api, type Session } from "./api";

export function Login({ onLogin }: { onLogin: (session: Session) => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (event: Event) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      onLogin(await api.login(username, password));
    } catch (failure: unknown) {
      setError(
        failure instanceof ApiError && failure.status === 401
          ? "Benutzername oder Passwort ist falsch."
          : "Anmeldung nicht möglich. Läuft der Server?",
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <main class="centered">
      <form class="card login" onSubmit={submit}>
        <h1>
          <span aria-hidden="true">👝</span> Bag of Holding
        </h1>
        <label>
          Benutzername
          <input
            value={username}
            onInput={(e) => setUsername((e.target as HTMLInputElement).value)}
            autocomplete="username"
            required
          />
        </label>
        <label>
          Passwort
          <input
            type="password"
            value={password}
            onInput={(e) => setPassword((e.target as HTMLInputElement).value)}
            autocomplete="current-password"
            required
          />
        </label>
        {error && <p class="error">{error}</p>}
        <button type="submit" disabled={busy}>
          Anmelden
        </button>
        <p class="hint">Zugang wird auf dem Server mit <code>bag password set</code> eingerichtet.</p>
      </form>
    </main>
  );
}
