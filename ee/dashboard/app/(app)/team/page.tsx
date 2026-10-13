import type { Metadata } from "next";
import { TeamPage } from "@/components/team-page";

export const metadata: Metadata = { title: "Team" };

export default function Page() {
  return <TeamPage />;
}
