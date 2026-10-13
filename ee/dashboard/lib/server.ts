/** Server-component access to the control plane, using the session cookie directly. */
import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { ApiError, errorFrom } from "./api";
import { controlPlaneUrl } from "./config";
import { COOKIE_NAME } from "./session";
import type { Me } from "./types";

export async function serverGet<T>(path: string): Promise<T> {
  const token = (await cookies()).get(COOKIE_NAME)?.value;
  if (!token) throw new ApiError(401, "unauthenticated", "Sign in to continue");
  let res: Response;
  try {
    res = await fetch(`${controlPlaneUrl()}${path}`, {
      headers: { authorization: `Bearer ${token}`, accept: "application/json" },
      cache: "no-store",
      redirect: "manual",
      signal: AbortSignal.timeout(15_000),
    });
  } catch {
    throw new ApiError(502, "upstream_unavailable", "The control plane could not be reached");
  }
  const text = await res.text();
  let parsed: unknown;
  try {
    parsed = text ? JSON.parse(text) : undefined;
  } catch {
    parsed = undefined;
  }
  if (!res.ok) throw errorFrom(res.status, parsed, text);
  return parsed as T;
}

/** The signed-in user, or a redirect that clears a dead session and sends them to /login. */
export async function requireMe(): Promise<Me> {
  try {
    return await serverGet<Me>("/api/me");
  } catch (e) {
    if (e instanceof ApiError && e.status === 401) redirect("/auth/expired");
    throw e;
  }
}
