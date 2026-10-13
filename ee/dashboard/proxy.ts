/**
 * Runs before every page (Next 16 renamed `middleware` to `proxy`): sets the nonce-based CSP and
 * the other security headers, and redirects by whether the session cookie is present. The cookie's
 * validity is checked by the server layout; this is only the cheap first gate.
 */
import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";
import { buildCsp, SECURITY_HEADERS } from "./lib/csp";
import { COOKIE_NAME } from "./lib/session";

/** Pages a signed-out visitor may see. */
const PUBLIC_PATHS = ["/login", "/signup", "/sso/complete", "/sso/error"];
/** Pages a signed-in user is sent away from. */
const GUEST_ONLY = ["/login", "/signup"];

function matches(pathname: string, paths: string[]): boolean {
  return paths.some((p) => pathname === p || pathname.startsWith(p + "/"));
}

export function proxy(request: NextRequest): NextResponse {
  const { pathname } = request.nextUrl;
  const signedIn = Boolean(request.cookies.get(COOKIE_NAME)?.value);

  let response: NextResponse;
  if (!signedIn && !matches(pathname, PUBLIC_PATHS)) {
    const url = new URL("/login", request.url);
    if (pathname !== "/") url.searchParams.set("next", pathname + request.nextUrl.search);
    response = NextResponse.redirect(url);
  } else if (signedIn && matches(pathname, GUEST_ONLY)) {
    response = NextResponse.redirect(new URL("/", request.url));
  } else {
    const nonce = btoa(crypto.randomUUID());
    const csp = buildCsp(nonce, process.env.NODE_ENV === "development");
    const requestHeaders = new Headers(request.headers);
    requestHeaders.set("x-nonce", nonce);
    requestHeaders.set("Content-Security-Policy", csp);
    response = NextResponse.next({ request: { headers: requestHeaders } });
    response.headers.set("Content-Security-Policy", csp);
  }
  for (const [k, v] of Object.entries(SECURITY_HEADERS)) response.headers.set(k, v);
  response.headers.set("Cache-Control", "no-store");
  return response;
}

export const config = {
  matcher: [
    {
      source: "/((?!_next/static|_next/image|auth/|bff/|favicon.ico|icon.svg).*)",
      missing: [
        { type: "header", key: "next-router-prefetch" },
        { type: "header", key: "purpose", value: "prefetch" },
      ],
    },
  ],
};
