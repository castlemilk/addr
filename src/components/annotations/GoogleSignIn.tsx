"use client";
import { useEffect, useRef, useState } from "react";
import { LoaderCircle } from "lucide-react";
import { firebaseConnection, googleSignInError, lastGoogleEmail, signInGoogle, signInNotice, signInWithGoogleIdToken, signOutGoogle } from "../../lib/annotations/firebase";
import { offerQuickSignIn } from "../../lib/annotations/google-identity";

function GoogleMark() {
  return <svg className="fw-google-mark" viewBox="0 0 24 24" aria-hidden="true"><path fill="#4285f4" d="M21.6 12.23c0-.71-.06-1.39-.18-2.05H12v3.88h5.38a4.6 4.6 0 0 1-2 3.02v2.51h3.24c1.9-1.75 2.98-4.33 2.98-7.36Z" /><path fill="#34a853" d="M12 22c2.7 0 4.96-.9 6.62-2.41l-3.24-2.51c-.9.6-2.05.96-3.38.96-2.61 0-4.83-1.76-5.62-4.12H3.04v2.59A10 10 0 0 0 12 22Z" /><path fill="#fbbc05" d="M6.38 13.92A6 6 0 0 1 6.06 12c0-.67.12-1.32.32-1.92V7.49H3.04A10 10 0 0 0 2 12c0 1.61.39 3.14 1.04 4.51l3.34-2.59Z" /><path fill="#ea4335" d="M12 5.96c1.47 0 2.79.5 3.82 1.49l2.87-2.87A9.6 9.6 0 0 0 12 2a10 10 0 0 0-8.96 5.49l3.34 2.59A5.99 5.99 0 0 1 12 5.96Z" /></svg>;
}

export default function GoogleSignIn({server,onConnected,onSignedOut,variant="dialog"}:{server:string;onConnected:()=>void;onSignedOut:()=>void;variant?:"dialog"|"login"}) {
  const [connection,setConnection] = useState<Awaited<ReturnType<typeof firebaseConnection>>>(null);
  const [loading,setLoading] = useState(true), [error,setError] = useState("");
  // popup: Google's sign-in window; quick: the browser's Google account (prompt); signout.
  const [busy,setBusy] = useState<""|"popup"|"quick"|"signout">("");
  const [email,setEmail] = useState("");
  const [hint,setHint] = useState(()=>lastGoogleEmail());
  const [attempt,setAttempt] = useState(0);
  const connected = useRef(onConnected);
  const working = useRef(false); // this tab is signing in or out right now
  useEffect(()=>{connected.current = onConnected;},[onConnected]);
  useEffect(()=>{
    let active = true;
    firebaseConnection(server).then(value=>{
      if (active) {setConnection(value);setEmail(value?.auth.currentUser?.email || "");setLoading(false);}
    },error=>{if(active){setError(googleSignInError(error));setLoading(false);}});
    return ()=>{active=false;};
  },[server,attempt]);
  // Another tab signed in (the sign-in is shared by every tab): open the workspace here too, keeping this tab's deep link.
  useEffect(()=>{
    if (variant !== "login" || !connection) return;
    let shown = connection.auth.currentUser?.uid ?? "";
    return connection.sdk.onAuthStateChanged(connection.auth,user=>{
      const uid = user?.uid ?? "";
      if (uid && uid !== shown && !working.current) connected.current();
      shown = uid;
    });
  },[variant,connection]);
  // Quick sign-in with the browser's Google account: login screen only, while signed out, when the server names an OAuth
  // client. Not right after a refused saved sign-in: the reason note stays on its own, without Chrome offering that account.
  const quickClientId = variant === "login" && connection && !email && !signInNotice() ? connection.googleClientId : "";
  useEffect(()=>{
    if (!quickClientId || !connection) return;
    let active = true;
    const stop = offerQuickSignIn(quickClientId,idToken=>{
      if (!active) return;
      working.current = true;setBusy("quick");setError("");
      signInWithGoogleIdToken(connection,idToken).then(()=>{if(active) connected.current();},
        failure=>{if(active){setEmail(connection.auth.currentUser?.email || "");setError(googleSignInError(failure));}})
        .finally(()=>{working.current = false;if(active) setBusy("");});
    });
    return ()=>{active=false;stop();};
  },[quickClientId,connection]);
  async function signIn(switchAccount: boolean) {
    if (!connection) return;
    working.current = true;setBusy("popup");setError("");
    try {await signInGoogle(connection,switchAccount);onConnected();}
    catch(error){setEmail(connection.auth.currentUser?.email || "");setError(googleSignInError(error));}
    finally{working.current = false;setBusy("");}
  }
  async function signOut() {
    if (!connection) return;
    working.current = true;setBusy("signout");setError("");
    try {await signOutGoogle(connection);setEmail("");setHint("");onSignedOut();}
    catch(error){setError(googleSignInError(error));}
    finally{working.current = false;setBusy("");}
  }
  const notice = !email && !error && !busy ? signInNotice() : "";
  const action = email ? "Switch Google account" : hint ? `Continue as ${hint}` : "Continue with Google";
  if (variant === "login") return <section className="fw-login-google-section" aria-label="Google account">
    {email && <p className="fw-login-account">Current account: {email}</p>}
    {notice && <p className="fw-login-google-reason" role="status">{notice}</p>}
    <button type="button" className="fw-login-google" disabled={loading || !!busy || !connection} onClick={()=>void signIn(!!email)} aria-busy={loading || !!busy}>
      {loading || busy ? <LoaderCircle size={20} className="fw-spin" aria-hidden="true" /> : <GoogleMark />}
      <span className="fw-login-google-label">{loading ? "Getting sign-in ready…" : busy === "popup" ? "Connecting to Google…" : busy === "quick" ? "Signing in…" : busy === "signout" ? "Signing out…" : action}</span>
    </button>
    {!loading && !connection && !error && <p className="fw-login-google-note">Google sign-in is not configured on this server. Open Connection settings to use a workspace access token.</p>}
    {!email && hint && connection && <button type="button" className="fw-login-signout" disabled={!!busy} onClick={()=>void signIn(true)}>Switch Google account</button>}
    {email && <button type="button" className="fw-login-signout" disabled={!!busy} onClick={()=>void signOut()}>Sign out of this account</button>}
    {error && <p role="alert" className="fw-login-google-error">{error}</p>}
    {!loading && !connection && error && <button type="button" className="fw-login-signout" onClick={()=>{setLoading(true);setError("");setAttempt(value=>value+1);}}>Retry Google sign-in</button>}
    {busy === "popup" && <p className="fw-login-google-status" role="status">Finish signing in in the Google window.</p>}
  </section>;
  return <section className="fw-google-signin" aria-label="Google account">
    {loading ? <p role="status">Checking Google sign-in…</p> : connection ? <>
      <p className="fw-modal-copy">{email ? `Signed in as ${email}` : "Use your Google account to open this workspace."}</p>
      {notice && <p className="fw-field-help" role="status">{notice}</p>}
      <div className="fw-dialog-actions"><button type="button" className="fw-button fw-primary" disabled={!!busy} onClick={()=>void signIn(!!email)}><GoogleMark />{busy ? "Please wait…" : action}</button>
      {!email && hint && <button type="button" className="fw-button" disabled={!!busy} onClick={()=>void signIn(true)}>Switch Google account</button>}
      {email && <button type="button" className="fw-button" disabled={!!busy} onClick={()=>void signOut()}>Sign out</button>}</div>
    </> : !error && <p className="fw-field-help">Google sign-in is not configured on this server yet. You can still connect with a workspace access token below.</p>}
    {error && <p role="alert" className="fw-form-error">{error}</p>}
  </section>;
}
