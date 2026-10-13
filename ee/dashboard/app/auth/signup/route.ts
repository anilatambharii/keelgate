import { signup } from "@/lib/auth-routes";

export const dynamic = "force-dynamic";

export async function POST(req: Request): Promise<Response> {
  return signup(req);
}
