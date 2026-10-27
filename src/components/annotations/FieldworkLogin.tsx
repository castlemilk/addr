"use client";

import { Layers3, LoaderCircle, LockKeyhole, RefreshCw, Settings2 } from "lucide-react";
import AsciiNeighbourhood from "./AsciiNeighbourhood";
import GoogleSignIn from "./GoogleSignIn";
import EmailSignIn from "./EmailSignIn";
import "./login.css";

export default function FieldworkLogin({ server, loading, unavailable, onConnected, onSettings, onRetry }: {
  server: string; loading: boolean; unavailable: boolean;
  onConnected: () => void; onSettings: () => void; onRetry: () => void;
}) {
  return <main className="fw-login">
    <div className="fw-login-panel">
      <header className="fw-login-brand" aria-label="Fieldwork"><Layers3 size={31} strokeWidth={1.7} /><span>Fieldwork</span></header>
      <div className="fw-login-content">
        <Layers3 className="fw-login-emblem" size={43} strokeWidth={1.25} aria-hidden="true" />
        {/* Opening (a remembered sign-in is being checked) and an outage are not a sign-in request: no "Sign in" wording. */}
        <h1>{loading ? "Opening Fieldwork" : unavailable ? "Workspace unavailable" : "Sign in to Fieldwork"}</h1>
        <p className="fw-login-intro">Your imagery, models and feedback,<br className="fw-login-break" /> in one workspace.</p>
        {loading ? <div className="fw-login-opening" role="status"><LoaderCircle size={19} className="fw-spin" />Opening your workspace…</div>
          : unavailable ? <div className="fw-login-unavailable"><p role="alert">The workspace could not be reached. Check your connection and try again.</p><button type="button" className="fw-login-google" onClick={onRetry}><RefreshCw size={18} />Retry connection</button></div>
          : <><GoogleSignIn server={server} variant="login" onConnected={onConnected} onSignedOut={onConnected} /><EmailSignIn server={server} onConnected={onConnected}/></>}
        {!loading && !unavailable && <p className="fw-login-access"><LockKeyhole size={14} aria-hidden="true" />Your signed-in account controls workspace access.</p>}
        <button type="button" className="fw-login-settings" onClick={onSettings}><Settings2 size={15} />Connection settings</button>
      </div>
      <footer className="fw-login-footer"><span>Fieldwork</span><span>Image review workspace</span></footer>
    </div>
    <AsciiNeighbourhood />
  </main>;
}
