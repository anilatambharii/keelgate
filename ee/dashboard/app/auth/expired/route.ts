import { clearCookieHeader } from "@/lib/session";

export const dynamic = "force-dynamic";

/** The layout sends users here when their session no longer works: drop the cookie, go to login. */
export function GET(req: Request): Response {
  const headers = new Headers({ location: new URL("/login?expired=1", req.url).toString() });
  headers.append("set-cookie", clearCookieHeader());
  headers.set("cache-control", "no-store");
  return new Response(null, { status: 303, headers });
}
