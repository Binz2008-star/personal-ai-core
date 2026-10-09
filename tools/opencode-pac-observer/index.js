/**
 * OpenCode's supported Promise session HTTP hook, scoped to PAC.
 * This observer adds only the request-kind header. It never reads or changes
 * the request body, advertised tools, system text, options, or permissions.
 * The native Plugin.define helper returns this same { id, setup } shape.
 */
const REQUEST_KINDS = new Set([
  "primary", "title", "summary", "compaction", "auxiliary", "generate",
]);

export function observeRequestKind(event) {
  if (event?.model?.providerID !== "pac" || !REQUEST_KINDS.has(event.kind)) return;
  const headers = new Headers(event.request.headers);
  headers.set("X-PAC-Request-Kind", event.kind);
  event.request = new Request(event.request, { headers });
}

export default {
  id: "pac.opencode.request-kind-observer",
  async setup(context) {
    const registration = await context.session.hook(
      "http.request", observeRequestKind, { providerID: "pac" },
    );
    return async () => { await registration.dispose(); };
  },
};
