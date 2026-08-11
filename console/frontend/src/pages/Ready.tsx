import { useAuth } from "../auth";

export default function ReadyPage() {
  const { user, logout } = useAuth();

  return (
    <div className="app-frame">
      <header className="topbar">
        <strong>KIN Mail Console</strong>
        <div>
          <span className="mono" style={{ marginRight: "0.85rem", color: "var(--muted)" }}>
            {user?.username}
          </span>
          <button type="button" onClick={() => void logout()}>
            Sign out
          </button>
        </div>
      </header>
      <main className="main">
        <div className="ready">
          <p className="brand">KIN Mail</p>
          <h1>Console ready</h1>
          <p>
            Setup wizard belum diimplementasi. Fondasi auth, TLS, dan service isolation sudah
            aktif — slice berikutnya akan menambah wizard 1-VM / 2-VM.
          </p>
        </div>
      </main>
    </div>
  );
}
