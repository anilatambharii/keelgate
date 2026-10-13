import { forward } from "@/lib/bff";

export const dynamic = "force-dynamic";

type Ctx = { params: Promise<{ path: string[] }> };

async function handle(req: Request, ctx: Ctx): Promise<Response> {
  const { path } = await ctx.params;
  return forward(req, path);
}

export { handle as GET, handle as POST, handle as PUT, handle as PATCH, handle as DELETE };
