// API client. All requests go through /api (Vite proxies to the
// dossier app on :8000 in dev). The current POC user is pulled from
// the auth store and sent as X-POC-User on every request — that's
// how the backend's POCAuthMiddleware identifies the caller.

import { useAuthStore } from "./stores/auth";

function apiHeaders() {
  const auth = useAuthStore();
  const h = { "Content-Type": "application/json" };
  if (auth.currentUser) {
    h["X-POC-User"] = auth.currentUser.username;
  }
  return h;
}

async function handle(resp) {
  if (!resp.ok) {
    // Try to extract the error detail so the UI can show it.
    let detail = resp.statusText;
    try {
      const body = await resp.json();
      detail = body.detail || body.message || detail;
    } catch (_) { /* not JSON */ }
    const err = new Error(detail);
    err.status = resp.status;
    throw err;
  }
  if (resp.status === 204) return null;
  const ct = resp.headers.get("content-type") || "";
  if (ct.includes("application/json")) return await resp.json();
  return await resp.blob();
}

export async function listDossiers(workflow) {
  const q = workflow ? `?workflow=${encodeURIComponent(workflow)}` : "";
  const r = await fetch(`/api/dossiers${q}`, { headers: apiHeaders() });
  return handle(r);
}

export async function getDossier(id) {
  const r = await fetch(`/api/dossiers/${id}`, { headers: apiHeaders() });
  return handle(r);
}

export async function executeActivity(dossierId, activityId, activityType, body) {
  // Typed endpoint: /dossiers/{id}/activities/{id}/{activity_type}
  // The engine has both a generic endpoint (requires `type` in body)
  // and a typed endpoint per activity. We use the typed one — it's
  // self-documenting in the OpenAPI spec and matches the pattern
  // test_requests.sh uses.
  const r = await fetch(
    `/api/dossiers/${dossierId}/activities/${activityId}/${activityType}`,
    { method: "PUT", headers: apiHeaders(), body: JSON.stringify(body) }
  );
  return handle(r);
}

// The archive endpoint streams a PDF. We fetch it as a blob and kick
// off a browser download rather than trying to render it inline.
export async function downloadArchive(dossierId) {
  const r = await fetch(`/api/dossiers/${dossierId}/archive`, {
    headers: apiHeaders(),
  });
  if (!r.ok) {
    const err = new Error(`Archive download failed (${r.status})`);
    err.status = r.status;
    throw err;
  }
  const blob = await r.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `dossier-${dossierId.slice(0, 8)}-archief.pdf`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// UUID v4 (for generating fresh dossier_id / activity_id /
// entity_id / version_id on new-application submission).
export function uuid4() {
  if (crypto.randomUUID) return crypto.randomUUID();
  // Fallback for older environments.
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    return (c === "x" ? r : (r & 0x3) | 0x8).toString(16);
  });
}
