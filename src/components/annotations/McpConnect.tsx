import { useEffect, useState } from "react";
import { firebaseConnection } from "../../lib/annotations/firebase";
import GoogleSignIn from "./GoogleSignIn";
import EmailSignIn from "./EmailSignIn";
import "./login.css";

const mcpOrigin = "https://addr-crm-mcp.ben-ebsworth.workers.dev";
type Details = { clientName: string; callbackHost: string; scope: string };
export default function McpConnect() {
  const [connection, setConnection] =
    useState<Awaited<ReturnType<typeof firebaseConnection>>>(null);
  const [details, setDetails] = useState<Details | null>(null),
    [email, setEmail] = useState(""),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  const state = new URLSearchParams(window.location.search).get("state") ?? "";
  useEffect(() => {
    let active = true;
    let unsubscribe: (() => void) | undefined;
    if (!/^[A-Za-z0-9_-]{32,256}$/.test(state)) {
      setError("Start a new connection from the intended assistant account.");
      return;
    }
    Promise.all([
      firebaseConnection(),
      fetch(`${mcpOrigin}/connect/info?state=${encodeURIComponent(state)}`, {
        credentials: "include",
        cache: "no-store",
        redirect: "error",
      }).then(async (response) => {
        if (!response.ok)
          throw new Error(
            "This connection expired or belongs to another browser. Restart it from your assistant account.",
          );
        return response.json() as Promise<Details>;
      }),
    ])
      .then(([value, info]) => {
        if (!active) return;
        if (
          !value?.sharedWorkspaces ||
          info.scope !== "fieldwork:read" ||
          typeof info.clientName !== "string" ||
          typeof info.callbackHost !== "string"
        )
          throw new Error("The private workspace connection is not ready.");
        setConnection(value);
        setDetails(info);
        unsubscribe = value.sdk.onAuthStateChanged(value.auth, (user) => {
          if (active) setEmail(user?.email ?? "");
        });
      })
      .catch((failure) => {
        if (active)
          setError(
            failure instanceof Error
              ? failure.message
              : "The connection could not be opened.",
          );
      });
    return () => {
      active = false;
      unsubscribe?.();
    };
  }, [state]);
  async function approve() {
    const user = connection?.auth.currentUser;
    if (!user || !details) return;
    setBusy(true);
    setError("");
    try {
      const idToken = await user.getIdToken();
      if (connection.auth.currentUser !== user)
        throw new Error("Your account changed. Restart the connection.");
      const response = await fetch(`${mcpOrigin}/connect/complete`, {
        method: "POST",
        credentials: "include",
        redirect: "error",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          state,
          decision: "approve",
          idToken,
          refreshToken: user.refreshToken,
        }),
      });
      const result = (await response.json()) as { redirectTo?: unknown };
      if (!response.ok || typeof result.redirectTo !== "string")
        throw new Error(
          "Connection failed or expired. Restart it from your assistant account.",
        );
      const target = new URL(result.redirectTo),
        loopback = ["localhost", "127.0.0.1", "[::1]"].includes(
          target.hostname,
        );
      if (
        target.host !== details.callbackHost ||
        target.username ||
        target.password ||
        (target.protocol !== "https:" &&
          !(target.protocol === "http:" && loopback))
      )
        throw new Error(
          "The return address could not be verified. Restart the connection.",
        );
      window.location.assign(target.href);
    } catch (failure) {
      setError(
        failure instanceof Error ? failure.message : "The connection failed.",
      );
      setBusy(false);
    }
  }
  return (
    <main className="fw-login">
      <section className="fw-login-panel">
        <h1>Connect Fieldwork</h1>
        {details ? (
          <>
            <p>
              <strong>{details.clientName}</strong> requests read access to your
              private workspace, projects, items, labels and images.
            </p>
            <p>
              Access returns to <strong>{details.callbackHost}</strong>. It
              cannot edit annotations or approve human reviews. Renewal can
              continue while used, with sign-in required after 30 days of
              inactivity.
            </p>
            <GoogleSignIn
              server=""
              onConnected={() => window.location.reload()}
              onSignedOut={() => window.location.reload()}
            />
            <EmailSignIn
              server=""
              onConnected={() => window.location.reload()}
            />
            {email && (
              <>
                <p>
                  Connect as <strong>{email}</strong>
                </p>
                <button
                  className="fw-login-google"
                  disabled={busy}
                  onClick={() => void approve()}
                >
                  {busy ? "Connecting…" : "Allow read access"}
                </button>
              </>
            )}
            <a href="/">Cancel and return to Fieldwork</a>
          </>
        ) : (
          !error && <p role="status">Checking the connection…</p>
        )}
        {error && <p role="alert">{error}</p>}
      </section>
    </main>
  );
}
