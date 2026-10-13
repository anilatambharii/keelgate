import type { ApprovalRequest, Usage } from "./types";

export interface ChecklistInput {
  keyCount: number | null; // null = unknown (the viewer cannot list keys)
  usage: Usage | null;
  traceCount: number | null;
  approvals: ApprovalRequest[] | null;
  billingStatus: string;
  hasSubscription: boolean | null;
}

export interface ChecklistItem {
  id: "org" | "key" | "agent" | "events" | "approval" | "plan";
  title: string;
  done: boolean;
  unknown?: boolean;
  href?: string;
  hint: string;
}

export function computeChecklist(i: ChecklistInput): ChecklistItem[] {
  const gated = i.usage?.usage?.gated_actions ?? 0;
  const agents = i.usage?.agents ?? 0;
  const events =
    gated > 0 ||
    (i.traceCount ?? 0) > 0 ||
    Object.values(i.usage?.usage ?? {}).some((v) => typeof v === "number" && v > 0);
  const decided = (i.approvals ?? []).some((a) => a.status !== "PENDING");
  const planChosen = i.hasSubscription === true || i.billingStatus === "active";
  return [
    { id: "org", title: "Organisation created", done: true, hint: "You are signed in to your organisation." },
    {
      id: "key",
      title: "Create an API key",
      done: (i.keyCount ?? 0) > 0,
      unknown: i.keyCount === null,
      href: "/keys",
      hint: "Agents authenticate to the control plane with an API key.",
    },
    {
      id: "agent",
      title: "Install and run a governed agent",
      done: agents > 0,
      hint: "Use the snippet below, then run an agent that calls a gated tool.",
    },
    {
      id: "events",
      title: "First events seen",
      done: events,
      href: "/traces",
      hint: "Audit events, traces and gated actions appear here once your agent runs.",
    },
    {
      id: "approval",
      title: "Decide your first approval",
      done: decided,
      href: "/approvals",
      hint: "When a policy requires a person, the request waits in the approvals inbox.",
    },
    {
      id: "plan",
      title: "Choose a plan",
      done: planChosen,
      unknown: i.hasSubscription === null && i.billingStatus !== "active",
      href: "/billing",
      hint: "Pick a plan before the trial ends to keep your agents running.",
    },
  ];
}

export function agentSnippet(baseUrl: string, tenantId: string): string {
  return [
    "# pip install keelgate",
    "import os",
    "from keelgate.cloud import (",
    "    CloudApprovalQueue, CloudAuditStore, CloudClient, CloudPolicyEngine, CloudSpanExporter,",
    ")",
    "",
    "# The key is shown once, when you create it. Keep it in your environment, not in code.",
    `client = CloudClient(base_url="${baseUrl}", api_key=os.environ["KEELGATE_API_KEY"])`,
    `TENANT_ID = "${tenantId}"  # your organisation`,
    "",
    "print(client.whoami())                    # checks the key",
    "audit = CloudAuditStore(client)           # audit events, chained by the server",
    "approvals = CloudApprovalQueue(client)    # actions that need a person go to the inbox",
    "exporter = CloudSpanExporter(client)      # OpenTelemetry spans to the trace explorer",
    "# policy = CloudPolicyEngine.from_registry(client, \"my-pack\")  # after you deploy a pack",
  ].join("\n");
}
