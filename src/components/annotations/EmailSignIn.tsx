import { useEffect, useState, type FormEvent } from "react";
import {
  firebaseConnection,
  signInEmail,
} from "../../lib/annotations/firebase";

export default function EmailSignIn({
  server,
  onConnected,
}: {
  server: string;
  onConnected: () => void;
}) {
  const [connection, setConnection] =
    useState<Awaited<ReturnType<typeof firebaseConnection>>>(null);
  const [create, setCreate] = useState(false),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    firebaseConnection(server).then(
      (value) => {
        if (active) setConnection(value);
      },
      () => {},
    );
    return () => {
      active = false;
    };
  }, [server]);
  if (!connection?.sharedWorkspaces) return null;
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!connection) return;
    const data = new FormData(event.currentTarget);
    setBusy(true);
    setError("");
    try {
      await signInEmail(
        connection,
        String(data.get("email")),
        String(data.get("password")),
        create,
      );
      onConnected();
    } catch {
      setError(
        create
          ? "Your account could not be created. Check the email and password, or sign in to an existing account."
          : "Sign-in could not be completed. Check your email and password, then try again.",
      );
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="fw-email-signin" aria-label="Email account">
      <form onSubmit={submit}>
        <label className="fw-field">
          Email
          <input
            name="email"
            type="email"
            autoComplete="username"
            required
            maxLength={254}
          />
        </label>
        <label className="fw-field">
          Password
          <input
            name="password"
            type="password"
            autoComplete={create ? "new-password" : "current-password"}
            required
            minLength={create ? 12 : 1}
            maxLength={256}
          />
        </label>
        {error && <p role="alert">{error}</p>}
        <button className="fw-login-google" disabled={busy}>
          {busy
            ? "Please wait…"
            : create
              ? "Create private workspace"
              : "Sign in with email"}
        </button>
        <button
          type="button"
          className="fw-login-signout"
          disabled={busy}
          onClick={() => {
            setCreate(!create);
            setError("");
          }}
        >
          {create ? "Use an existing account" : "Create an account"}
        </button>
      </form>
      <p>Your workspace is private to your account.</p>
    </section>
  );
}
