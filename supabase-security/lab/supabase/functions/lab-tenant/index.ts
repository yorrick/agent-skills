// Local-only fixture: verify the caller before using a service-role read.
Deno.serve(async (request: Request): Promise<Response> => {
  const authorization = request.headers.get("authorization");
  if (!authorization?.startsWith("Bearer ")) {
    return new Response("Authentication required", { status: 401 });
  }

  const url = Deno.env.get("SUPABASE_URL")!;
  const anonKey = Deno.env.get("SUPABASE_ANON_KEY")!;
  const serviceKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
  const userResponse = await fetch(`${url}/auth/v1/user`, {
    headers: { apikey: anonKey, authorization },
  });
  if (!userResponse.ok) {
    return new Response("Authentication required", { status: 401 });
  }
  const user = await userResponse.json();
  if (typeof user.id !== "string") {
    return new Response("Authentication required", { status: 401 });
  }

  // The service key bypasses RLS, so the verified user ID must constrain the query.
  const query = new URL(`${url}/rest/v1/lab_http_documents`);
  query.searchParams.set("select", "id,owner,body");
  query.searchParams.set("owner", `eq.${user.id}`);
  const rows = await fetch(query, {
    headers: { apikey: serviceKey, authorization: `Bearer ${serviceKey}` },
  });
  if (!rows.ok) {
    return new Response("Lab query failed", { status: 502 });
  }
  return new Response(await rows.text(), { headers: { "content-type": "application/json" } });
});
