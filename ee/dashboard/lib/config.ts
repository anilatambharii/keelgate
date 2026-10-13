/** Server-side configuration (read at call time so tests can change the environment). */
export function controlPlaneUrl(): string {
  return (process.env.CONTROL_PLANE_URL || "http://localhost:8000").replace(/\/+$/, "");
}

/** The control plane URL an agent or browser should use (shown in snippets, SSO redirect). */
export function controlPlanePublicUrl(): string {
  return (process.env.NEXT_PUBLIC_CONTROL_PLANE_PUBLIC_URL || "http://localhost:8000").replace(
    /\/+$/,
    "",
  );
}
