export type JsonPrimitive = string | number | boolean | null;
export type JsonValue =
  JsonPrimitive | JsonValue[] | { [key: string]: JsonValue };

export type CardState =
  "pending" | "resolved" | "superseded" | "expired" | "cancelled" | "failed";

export interface CardSource {
  readonly source_type: string;
  readonly source_id: string;
  readonly version: number;
}

export interface CardEnvelope {
  readonly protocol_version: "card.v1";
  readonly card_id: string;
  readonly kind: string;
  readonly schema_version: number;
  readonly revision: number;
  readonly source: CardSource;
  readonly state: CardState;
  readonly data: Record<string, JsonValue>;
  readonly actions: readonly string[];
  readonly payload_hash: string;
  readonly expires_at: string | null;
  readonly text_fallback: string;
}

export interface ChatMessage {
  readonly id: string;
  readonly role: "user" | "assistant" | "system";
  readonly content: string;
  readonly sequence: number;
  readonly createdAt?: string;
}

export type TimelineEntry =
  | {
      readonly type: "message";
      readonly id: string;
      readonly sequence: number;
      readonly message: ChatMessage;
    }
  | {
      readonly type: "card";
      readonly id: string;
      readonly sequence: number;
      readonly card: CardEnvelope;
    };

export interface RunEvent {
  readonly event_id: string;
  readonly run_id: string;
  readonly sequence: number;
  readonly event_type: string;
  readonly payload: Record<string, JsonValue>;
  readonly occurred_at?: string;
  readonly cursor?: string;
}

export interface CardActionRequest {
  readonly action: string;
  readonly values: Record<string, JsonValue>;
  readonly interactionVersion: number;
  readonly cardRevision: number;
  readonly payloadHash: string;
  readonly idempotencyKey: string;
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function isCardEnvelope(value: unknown): value is CardEnvelope {
  if (!isRecord(value) || !isRecord(value.source) || !isRecord(value.data))
    return false;
  return (
    value.protocol_version === "card.v1" &&
    typeof value.card_id === "string" &&
    typeof value.kind === "string" &&
    typeof value.schema_version === "number" &&
    typeof value.revision === "number" &&
    typeof value.source.source_type === "string" &&
    typeof value.source.source_id === "string" &&
    typeof value.source.version === "number" &&
    typeof value.state === "string" &&
    Array.isArray(value.actions) &&
    value.actions.every((action) => typeof action === "string") &&
    typeof value.payload_hash === "string" &&
    (value.expires_at === null || typeof value.expires_at === "string") &&
    typeof value.text_fallback === "string"
  );
}

export function asString(value: JsonValue | undefined, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

export function asNumber(value: JsonValue | undefined): number | undefined {
  return typeof value === "number" && Number.isFinite(value)
    ? value
    : undefined;
}

export function asRecord(
  value: JsonValue | undefined,
): Record<string, JsonValue> {
  return isRecord(value) ? (value as Record<string, JsonValue>) : {};
}

export function asRecords(
  value: JsonValue | undefined,
): Record<string, JsonValue>[] {
  return Array.isArray(value)
    ? value.filter((item): item is Record<string, JsonValue> => isRecord(item))
    : [];
}
