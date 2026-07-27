const DEFAULT_API_BASE = "/api";

function normalizeApiBase(value?: string): string {
  const trimmed = value?.trim();
  if (!trimmed) return DEFAULT_API_BASE;
  return trimmed === "/" ? "" : trimmed.replace(/\/+$/u, "");
}

export const apiBase = normalizeApiBase(import.meta.env.VITE_API_BASE_URL);
