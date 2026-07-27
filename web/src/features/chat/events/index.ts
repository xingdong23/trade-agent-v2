import { apiBase } from "../config";
import type {
  CardEnvelope,
  ChatMessage,
  JsonValue,
  RunEvent,
  TimelineEntry,
} from "../types";
import { isCardEnvelope, isRecord } from "../types";

export interface ChatSnapshot {
  readonly threadId: string;
  readonly cursor: string;
  readonly lastSequenceByRun: Readonly<Record<string, number>>;
  readonly messages: readonly ChatMessage[];
  readonly cards: readonly CardEnvelope[];
}

export const emptySnapshot = (threadId: string): ChatSnapshot => ({
  threadId,
  cursor: "",
  lastSequenceByRun: {},
  messages: [],
  cards: [],
});

export function reduceRunEvent(
  snapshot: ChatSnapshot,
  event: RunEvent,
): ChatSnapshot {
  const lastSequence = snapshot.lastSequenceByRun[event.run_id] ?? 0;
  if (event.sequence <= lastSequence) return snapshot;

  const next = {
    ...snapshot,
    cursor: event.cursor ?? event.event_id,
    lastSequenceByRun: {
      ...snapshot.lastSequenceByRun,
      [event.run_id]: event.sequence,
    },
  };

  if (event.event_type.startsWith("card.")) {
    const card = extractCard(event.payload);
    return card ? mergeCards(next, [card]) : next;
  }
  if (
    event.event_type === "message.created" ||
    event.event_type === "assistant.message"
  ) {
    const message = extractMessage(event);
    return message ? mergeMessages(next, [message]) : next;
  }
  return next;
}

export function mergeCards(
  snapshot: ChatSnapshot,
  incoming: readonly CardEnvelope[],
): ChatSnapshot {
  const cards = new Map(snapshot.cards.map((card) => [card.card_id, card]));
  for (const card of incoming) {
    const current = cards.get(card.card_id);
    if (!current || card.revision > current.revision)
      cards.set(card.card_id, card);
  }
  return { ...snapshot, cards: [...cards.values()] };
}

export function mergeMessages(
  snapshot: ChatSnapshot,
  incoming: readonly ChatMessage[],
): ChatSnapshot {
  const messages = new Map(
    snapshot.messages.map((message) => [message.id, message]),
  );
  for (const message of incoming) messages.set(message.id, message);
  return { ...snapshot, messages: [...messages.values()] };
}

export function toTimeline(snapshot: ChatSnapshot): TimelineEntry[] {
  const entries: TimelineEntry[] = [
    ...snapshot.messages.map((message) => ({
      type: "message" as const,
      id: message.id,
      sequence: message.sequence,
      message,
    })),
    ...snapshot.cards.map((card) => ({
      type: "card" as const,
      id: card.card_id,
      sequence: sourceSequence(card),
      card,
    })),
  ];
  return entries.sort(
    (left, right) =>
      left.sequence - right.sequence || left.id.localeCompare(right.id),
  );
}

export interface RecoveryPayload {
  readonly cards: readonly CardEnvelope[];
  readonly messages: readonly ChatMessage[];
  readonly cursor: string;
}

export class ChatRecoveryClient {
  constructor(
    private readonly baseUrl = apiBase,
    private readonly fetcher: typeof fetch = globalThis.fetch.bind(globalThis),
  ) {}

  async recover(
    threadId: string,
    signal?: AbortSignal,
  ): Promise<RecoveryPayload> {
    const encodedThreadId = encodeURIComponent(threadId);
    const response = await this.fetcher(
      `${this.baseUrl}/conversations/${encodedThreadId}/snapshot`,
      { credentials: "same-origin", signal },
    );
    if (!response.ok) throw new Error(`恢复数据失败（${response.status}）`);
    if (!isJsonResponse(response)) throw new Error("恢复接口未返回 JSON");
    const body: unknown = await response.json();
    if (!isRecord(body)) throw new Error("恢复接口返回格式无效");
    const rawCards = Array.isArray(body.cards) ? body.cards : [];
    const rawMessages = Array.isArray(body.messages) ? body.messages : [];
    return {
      cards: rawCards.flatMap((value) =>
        isCardEnvelope(value) ? [value] : [],
      ),
      messages: rawMessages.flatMap((value, index) =>
        parseMessage(value, index),
      ),
      cursor: typeof body.cursor === "string" ? body.cursor : "",
    };
  }
}

export interface EventStreamHandlers {
  readonly onEvent: (event: RunEvent) => void;
  readonly onStatus: (
    status: "idle" | "connecting" | "open" | "retrying",
  ) => void;
}

export class ReconnectingEventStream {
  private source?: EventSource;
  private stopped = false;
  private retryCount = 0;
  private retryTimer?: number;
  private cursor = "";

  constructor(
    private readonly url: string,
    private readonly handlers: EventStreamHandlers,
    private readonly cursorParameter = "cursor",
  ) {}

  connect(cursor = ""): void {
    this.closeSource();
    this.stopped = false;
    this.cursor = cursor || this.cursor;
    this.handlers.onStatus(this.retryCount === 0 ? "connecting" : "retrying");
    const separator = this.url.includes("?") ? "&" : "?";
    this.source = new EventSource(
      `${this.url}${this.cursor ? `${separator}${this.cursorParameter}=${encodeURIComponent(this.cursor)}` : ""}`,
      {
        withCredentials: true,
      },
    );
    this.source.onopen = () => {
      this.retryCount = 0;
      this.handlers.onStatus("open");
    };
    this.source.onmessage = (message) => this.handleMessage(message);
    for (const eventName of [
      "card.created",
      "card.updated",
      "card.resolved",
      "card.superseded",
      "card.failed",
      "message.created",
    ]) {
      this.source.addEventListener(eventName, (message) =>
        this.handleMessage(message as MessageEvent<string>),
      );
    }
    this.source.onerror = () => this.scheduleReconnect();
  }

  close(): void {
    this.stopped = true;
    this.closeSource();
    if (this.retryTimer !== undefined) {
      window.clearTimeout(this.retryTimer);
      this.retryTimer = undefined;
    }
  }

  private handleMessage(message: MessageEvent<string>): void {
    try {
      const value: unknown = JSON.parse(message.data);
      const event = parseRunEvent(value, message.lastEventId);
      if (event) {
        this.cursor = event.cursor ?? event.event_id;
        this.handlers.onEvent(event);
      }
    } catch {
      // 无效或未知事件不进入状态；后续资源恢复会校正客户端。
    }
  }

  private scheduleReconnect(): void {
    this.closeSource();
    if (this.stopped || this.retryTimer !== undefined) return;
    this.retryCount += 1;
    this.handlers.onStatus("retrying");
    const delay = Math.min(10_000, 500 * 2 ** Math.min(this.retryCount, 5));
    this.retryTimer = window.setTimeout(() => {
      this.retryTimer = undefined;
      this.connect(this.cursor);
    }, delay);
  }

  private closeSource(): void {
    this.source?.close();
    this.source = undefined;
  }
}

function parseRunEvent(value: unknown, cursor: string): RunEvent | undefined {
  if (!isRecord(value)) return undefined;
  if (
    typeof value.event_id !== "string" ||
    typeof value.run_id !== "string" ||
    typeof value.sequence !== "number" ||
    (typeof value.event_type !== "string" && typeof value.type !== "string") ||
    !isRecord(value.payload)
  )
    return undefined;
  return {
    event_id: value.event_id,
    run_id: value.run_id,
    sequence: value.sequence,
    event_type:
      typeof value.event_type === "string"
        ? value.event_type
        : String(value.type),
    payload: value.payload as Record<string, JsonValue>,
    occurred_at:
      typeof value.occurred_at === "string" ? value.occurred_at : undefined,
    cursor:
      cursor ||
      (typeof value.cursor === "string" ? value.cursor : value.event_id),
  };
}

function extractCard(
  payload: Record<string, JsonValue>,
): CardEnvelope | undefined {
  return isCardEnvelope(payload)
    ? payload
    : isCardEnvelope(payload.card)
      ? payload.card
      : undefined;
}

function extractMessage(event: RunEvent): ChatMessage | undefined {
  const message = isRecord(event.payload.message)
    ? event.payload.message
    : event.payload;
  if (!isRecord(message) || typeof message.content !== "string")
    return undefined;
  const role =
    message.role === "user" || message.role === "system"
      ? message.role
      : "assistant";
  return {
    id: typeof message.id === "string" ? message.id : event.event_id,
    role,
    content: message.content,
    sequence: event.sequence,
    createdAt:
      typeof message.created_at === "string"
        ? message.created_at
        : event.occurred_at,
  };
}

function parseMessage(value: unknown, fallbackSequence: number): ChatMessage[] {
  if (!isRecord(value) || typeof value.content !== "string") return [];
  return [
    {
      id:
        typeof value.id === "string"
          ? value.id
          : `recovered-${fallbackSequence}`,
      role:
        value.role === "user" || value.role === "system"
          ? value.role
          : "assistant",
      content: value.content,
      sequence:
        typeof value.sequence === "number" ? value.sequence : fallbackSequence,
      createdAt:
        typeof value.created_at === "string" ? value.created_at : undefined,
    },
  ];
}

function sourceSequence(card: CardEnvelope): number {
  const dataSequence = card.data.sequence;
  return typeof dataSequence === "number"
    ? dataSequence
    : card.source.version * 1_000 + card.revision;
}

function isJsonResponse(response: Response): boolean {
  return (
    response.headers
      .get("content-type")
      ?.toLowerCase()
      .includes("application/json") === true
  );
}
