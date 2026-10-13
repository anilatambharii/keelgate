"use client";
import { createContext, useContext, type ReactNode } from "react";
import type { Me } from "@/lib/types";

const Ctx = createContext<Me | null>(null);

export function MeProvider({ me, children }: { me: Me; children: ReactNode }) {
  return <Ctx.Provider value={me}>{children}</Ctx.Provider>;
}

export function useMe(): Me {
  const v = useContext(Ctx);
  if (!v) throw new Error("useMe must be used inside <MeProvider>");
  return v;
}

export function isAdmin(role: string): boolean {
  return role === "owner" || role === "admin";
}
