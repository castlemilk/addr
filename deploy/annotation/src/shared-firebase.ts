import {
  createRemoteJWKSet,
  jwtVerify,
  errors,
  type JWTVerifyGetKey,
} from "jose";
import { OAuthError } from "@cloudflare/workers-oauth-provider";

const keys = createRemoteJWKSet(
  new URL(
    "https://www.googleapis.com/service_accounts/v1/jwk/securetoken@system.gserviceaccount.com",
  ),
);
export interface IdentityConfig {
  FIREBASE_PROJECT_ID: string;
  FIREBASE_API_KEY: string;
}
export interface AccountSession {
  uid: string;
  idToken: string;
  refreshToken: string;
}

export async function verifyIdentity(
  token: string,
  config: IdentityConfig,
  jwks: JWTVerifyGetKey = keys,
): Promise<string> {
  const invalid = () =>
    new OAuthError("invalid_grant", {
      description:
        "Your service sign-in expired or belongs to another service. Sign in to Fieldwork again.",
    });
  if (!config.FIREBASE_PROJECT_ID || !token || token.length > 16384)
    throw invalid();
  let payload;
  try {
    ({ payload } = await jwtVerify(token, jwks, {
      issuer: `https://securetoken.google.com/${config.FIREBASE_PROJECT_ID}`,
      audience: config.FIREBASE_PROJECT_ID,
      algorithms: ["RS256"],
      requiredClaims: ["exp", "iat", "sub", "auth_time"],
      clockTolerance: 15,
    }));
  } catch (error) {
    if (
      error instanceof errors.JOSEError &&
      !["ERR_JWKS_TIMEOUT", "ERR_JWKS_INVALID"].includes(error.code)
    )
      throw invalid();
    throw new OAuthError("temporarily_unavailable", {
      description:
        "Service sign-in could not be verified right now. Retry shortly.",
      statusCode: 503,
      headers: { "Retry-After": "5" },
    });
  }
  const now = Math.floor(Date.now() / 1000);
  const provider = (
    payload.firebase as { sign_in_provider?: unknown } | undefined
  )?.sign_in_provider;
  if (
    !["google.com", "password"].includes(String(provider)) ||
    typeof payload.email !== "string"
  )
    throw invalid();
  if (
    typeof payload.sub !== "string" ||
    !payload.sub ||
    payload.sub.length > 128 ||
    typeof payload.iat !== "number" ||
    payload.iat > now + 15 ||
    typeof payload.auth_time !== "number" ||
    payload.auth_time <= 0 ||
    payload.auth_time > now + 15
  )
    throw invalid();
  return payload.sub;
}

export async function renewAccount(
  session: AccountSession,
  config: IdentityConfig,
  fetcher: typeof fetch = fetch,
): Promise<AccountSession> {
  if (
    !config.FIREBASE_API_KEY ||
    !session.uid ||
    !session.refreshToken ||
    session.refreshToken.length > 16384
  ) {
    throw new OAuthError("invalid_grant", {
      description: "This service connection is invalid. Reconnect Fieldwork.",
    });
  }
  let response: Response;
  try {
    response = await fetcher(
      `https://securetoken.googleapis.com/v1/token?key=${encodeURIComponent(config.FIREBASE_API_KEY)}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({
          grant_type: "refresh_token",
          refresh_token: session.refreshToken,
        }),
        redirect: "manual",
        signal: AbortSignal.timeout(10000),
      },
    );
  } catch {
    throw new OAuthError("temporarily_unavailable", {
      description: "Service sign-in is temporarily unavailable. Retry shortly.",
      statusCode: 503,
      headers: { "Retry-After": "5" },
    });
  }
  if (response.status === 429 || response.status >= 500) {
    throw new OAuthError("temporarily_unavailable", {
      description: "Service sign-in is temporarily unavailable. Retry shortly.",
      statusCode: 503,
      headers: { "Retry-After": "5" },
    });
  }
  if (!response.ok)
    throw new OAuthError("invalid_grant", {
      description:
        "Your service credential expired or was revoked. Reconnect Fieldwork.",
    });
  const data: unknown = await response.json();
  if (!data || typeof data !== "object")
    throw new Error("Invalid service renewal response.");
  const value = data as Record<string, unknown>;
  if (
    value.user_id !== session.uid ||
    typeof value.id_token !== "string" ||
    !value.id_token ||
    typeof value.refresh_token !== "string" ||
    !value.refresh_token ||
    Number(value.expires_in) < 2700
  ) {
    throw new OAuthError("invalid_grant", {
      description:
        "This credential belongs to another service account or is invalid. Reconnect the intended Fieldwork account.",
    });
  }
  if ((await verifyIdentity(value.id_token, config)) !== session.uid) {
    throw new OAuthError("invalid_grant", {
      description:
        "The renewed service account differs from the authorized account. Reconnect Fieldwork.",
    });
  }
  return {
    uid: session.uid,
    idToken: value.id_token,
    refreshToken: value.refresh_token,
  };
}
