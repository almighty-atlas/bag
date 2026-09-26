import { useEffect, useState } from "preact/hooks";

import { ApiError, tokens, type ApiToken } from "./api";

/** Client tokens for scripts and the Linux client; the secret is shown exactly once. */
export function Tokens() {
  const [rows, setRows] = useState<ApiToken[]>([]);
  const [name, setName] = useState("");
  const [fresh, setFresh] = useState<{ name: string; token: string } | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = () =>
    tokens
      .list()
      .then(setRows)
      .catch((failure: unknown) =>
        setError(
          failure instanceof ApiError && failure.status === 403
            ? "Token lassen sich nur mit Passwort-Anmeldung verwalten."
            : "Laden fehlgeschlagen.",
        ),
      );

  useEffect(() => {
    void load();
  }, []);

  const create = async (event: Event) => {
    event.preventDefault();
    setError(null);
    try {
      const created = await tokens.create(name.trim());
      setFresh({ name: created.name, token: created.token });
      setName("");
      await load();
    } catch (failure: unknown) {
      setError(failure instanceof ApiError ? failure.message : "Anlegen fehlgeschlagen.");
    }
  };

  const revoke = async (row: ApiToken) => {
    if (!confirm(`Token „${row.name}“ widerrufen? Clients damit verlieren sofort den Zugang.`)) return;
    await tokens.revoke(row.id);
    await load();
  };

  return (
    <section>
      <h1>Client-Token</h1>
      <p class="hint">
        Ein Token pro Gerät oder Skript. Der Wert erscheint nur einmal; gespeichert wird nur sein Hash.
      </p>
      <form class="card" onSubmit={create}>
        <div class="row">
          <input
            placeholder="Name, z. B. laptop"
            value={name}
            onInput={(e) => setName((e.target as HTMLInputElement).value)}
            required
          />
          <button type="submit" disabled={!name.trim()}>
            Token anlegen
          </button>
        </div>
        {fresh && (
          <p class="status" role="status">
            Neuer Token „{fresh.name}“ (jetzt sicher speichern): <code class="secret">{fresh.token}</code>
          </p>
        )}
        {error && <p class="error">{error}</p>}
      </form>
      <table>
        <tbody>
          {rows.map((row) => (
            <tr key={row.id} class={row.revoked_at ? "muted" : ""}>
              <td>{row.name}</td>
              <td>{new Date(row.created_at).toLocaleDateString()}</td>
              <td>{row.last_used_at ? `zuletzt ${new Date(row.last_used_at).toLocaleString()}` : "nie benutzt"}</td>
              <td>
                {row.revoked_at ? (
                  "widerrufen"
                ) : (
                  <button type="button" class="link" onClick={() => void revoke(row)}>
                    widerrufen
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
