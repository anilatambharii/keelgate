import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  ApiError,
  api,
  buildUrl,
  errorFrom,
  filenameFromDisposition,
  request,
  setUnauthenticatedHandler,
} from "@/lib/api";
import { jsonResponse } from "./helpers";

describe("errorFrom", () => {
  it("reads the stable {error:{code,message}} shape", () => {
    const e = errorFrom(402, { error: { code: "plan_required", message: "Upgrade" } });
    expect(e).toBeInstanceOf(ApiError);
    expect(e.status).toBe(402);
    expect(e.code).toBe("plan_required");
    expect(e.message).toBe("Upgrade");
  });

  it("understands FastAPI validation errors", () => {
    const e = errorFrom(422, {
      detail: [{ loc: ["body", "email"], msg: "Field required", type: "missing" }],
    });
    expect(e.code).toBe("invalid_input");
    expect(e.message).toBe("email: Field required");
  });

  it("falls back to the status for non-JSON bodies", () => {
    const e = errorFrom(502, undefined, "Bad gateway");
    expect(e.code).toBe("http_502");
    expect(e.message).toBe("Bad gateway");
  });

  it("keeps the whole body as details (deploy refusals carry a report)", () => {
    const body = { error: { code: "tests_failed", message: "no" }, report: { total: 2 } };
    const e = errorFrom(409, body);
    expect((e.details as typeof body).report.total).toBe(2);
  });
});

describe("request", () => {
  const fetchMock = vi.fn();
  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
    setUnauthenticatedHandler(null);
  });
  afterEach(() => {
    fetchMock.mockReset();
    vi.unstubAllGlobals();
  });

  it("calls the BFF with same-origin credentials and parses JSON", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ items: [] }));
    const out = await api.listKeys();
    expect(out).toEqual({ items: [] });
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/bff/api/keys");
    expect(init.credentials).toBe("same-origin");
  });

  it("sends a JSON body with a content type", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ id: "1" }, { status: 201 }));
    await api.createKey("ci", 30);
    const [, init] = fetchMock.mock.calls[0];
    expect(init.method).toBe("POST");
    expect(init.headers["content-type"]).toBe("application/json");
    expect(JSON.parse(init.body)).toEqual({ name: "ci", expires_in_days: 30 });
  });

  it("returns undefined for 204", async () => {
    fetchMock.mockResolvedValue(new Response(null, { status: 204 }));
    await expect(api.revokeKey("k1")).resolves.toBeUndefined();
  });

  it("throws ApiError with the server's code and message", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ error: { code: "forbidden", message: "nope" } }, { status: 403 }),
    );
    await expect(api.listUsers()).rejects.toMatchObject({ status: 403, code: "forbidden", message: "nope" });
  });

  it("calls the unauthenticated handler on 401", async () => {
    const handler = vi.fn();
    setUnauthenticatedHandler(handler);
    fetchMock.mockResolvedValue(
      jsonResponse({ error: { code: "unauthenticated", message: "x" } }, { status: 401 }),
    );
    await expect(api.me()).rejects.toMatchObject({ status: 401 });
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it("turns a network failure into a friendly ApiError", async () => {
    fetchMock.mockRejectedValue(new TypeError("fetch failed"));
    await expect(request("GET", "/api/me")).rejects.toMatchObject({ code: "network_error", status: 0 });
  });

  it("encodes path parts and drops empty query values", () => {
    expect(buildUrl("/api/traces", { q: "a b", errors_only: false, offset: 0, limit: undefined, x: "" })).toBe(
      "/bff/api/traces?q=a+b&errors_only=false&offset=0",
    );
  });
});

describe("filenameFromDisposition", () => {
  it("extracts a quoted name and strips path separators", () => {
    expect(filenameFromDisposition('attachment; filename="audit-1.csv"')).toBe("audit-1.csv");
    expect(filenameFromDisposition('attachment; filename="../x/evil.zip"')).toBe(".._x_evil.zip");
    expect(filenameFromDisposition(null)).toBeNull();
  });
});
