import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { KeyShownOnceDialog, KeysPage } from "@/components/keys-page";
import type { ApiKey, CreatedApiKey } from "@/lib/types";
import { makeMe, renderWithApp } from "./helpers";

vi.mock("@/lib/api", async (orig) => {
  const actual = await orig<typeof import("@/lib/api")>();
  return { ...actual, api: { listKeys: vi.fn(), createKey: vi.fn(), revokeKey: vi.fn() } };
});
import { api } from "@/lib/api";

const listKeys = vi.mocked(api.listKeys);
const createKey = vi.mocked(api.createKey);
const revokeKey = vi.mocked(api.revokeKey);

const base: ApiKey = {
  id: "k1",
  key_prefix: "kg_live_ABCD",
  name: "prod",
  created_at: "2026-10-01T00:00:00Z",
  last_used_at: null,
  expires_at: null,
  revoked_at: null,
};
const SECRET = "kg_live_ABCDsecretsecretsecret"; // pragma: allowlist secret
const created: CreatedApiKey = { ...base, key: SECRET };

beforeEach(() => {
  listKeys.mockReset().mockResolvedValue({ items: [base] });
  createKey.mockReset();
  revokeKey.mockReset();
});

describe("KeyShownOnceDialog", () => {
  it("shows the secret with a once-only warning and a copy button", () => {
    renderWithApp(<KeyShownOnceDialog created={created} onClose={() => {}} />);
    expect(screen.getByRole("dialog", { name: /copy your new api key/i })).toBeInTheDocument();
    expect(screen.getByTestId("api-key-secret")).toHaveTextContent(SECRET);
    expect(screen.getByText(/only time you will see this key/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Copy key" })).toBeInTheDocument();
  });

  it("cannot be closed until the user confirms they stored the key, and ignores Escape", async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    renderWithApp(<KeyShownOnceDialog created={created} onClose={onClose} />);
    const done = screen.getByRole("button", { name: "Done" });
    expect(done).toBeDisabled();
    await user.keyboard("{Escape}");
    expect(onClose).not.toHaveBeenCalled();
    await user.click(screen.getByLabelText(/stored this key/i));
    await user.click(done);
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

describe("KeysPage", () => {
  it("lists keys by prefix only", async () => {
    renderWithApp(<KeysPage />);
    expect(await screen.findByText("kg_live_ABCD...")).toBeInTheDocument();
    expect(screen.queryByText(SECRET)).not.toBeInTheDocument();
  });

  it("shows the new secret once after creating, then it is gone for good", async () => {
    createKey.mockResolvedValue(created);
    const user = userEvent.setup();
    renderWithApp(<KeysPage />);
    await screen.findByText("kg_live_ABCD...");
    await user.type(screen.getByLabelText("Name"), "prod");
    await user.click(screen.getByRole("button", { name: "Create key" }));
    expect(await screen.findByTestId("api-key-secret")).toHaveTextContent(SECRET);
    expect(createKey).toHaveBeenCalledWith("prod", null);
    await user.click(screen.getByLabelText(/stored this key/i));
    await user.click(screen.getByRole("button", { name: "Done" }));
    await waitFor(() => expect(screen.queryByTestId("api-key-secret")).not.toBeInTheDocument());
    expect(document.body.textContent).not.toContain(SECRET);
  });

  it("asks for confirmation before revoking", async () => {
    revokeKey.mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderWithApp(<KeysPage />);
    await user.click(await screen.findByRole("button", { name: /Revoke/ }));
    const dialog = screen.getByRole("dialog", { name: /revoke this key/i });
    expect(revokeKey).not.toHaveBeenCalled();
    await user.click(within(dialog).getByRole("button", { name: "Revoke key" }));
    await waitFor(() => expect(revokeKey).toHaveBeenCalledWith("k1"));
  });

  it("cancelling the confirmation revokes nothing", async () => {
    const user = userEvent.setup();
    renderWithApp(<KeysPage />);
    await user.click(await screen.findByRole("button", { name: /Revoke/ }));
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(revokeKey).not.toHaveBeenCalled();
  });

  it("is not available to approvers", () => {
    renderWithApp(<KeysPage />, makeMe("approver"));
    expect(screen.getByText(/Only owners and admins/)).toBeInTheDocument();
  });
});
