import { apiBase } from "../config";
import type { CardActionRequest, CardEnvelope, JsonValue } from "../types";
import { isCardEnvelope, isRecord } from "../types";

export interface HitlResponseResult {
  readonly card?: CardEnvelope;
  readonly duplicate: boolean;
}

export interface HitlResponseClient {
  respond(
    interactionId: string,
    request: CardActionRequest,
    signal?: AbortSignal,
  ): Promise<HitlResponseResult>;
  refresh(interactionId: string, signal?: AbortSignal): Promise<CardEnvelope>;
}

export class HitlFieldError extends Error {
  constructor(
    message: string,
    readonly fieldErrors: Readonly<Record<string, string>>,
    readonly latestCard?: CardEnvelope,
  ) {
    super(message);
    this.name = "HitlFieldError";
  }
}

export class HitlConflictError extends Error {
  constructor(
    message: string,
    readonly latestCard?: CardEnvelope,
  ) {
    super(message);
    this.name = "HitlConflictError";
  }
}

export class HttpHitlResponseClient implements HitlResponseClient {
  constructor(
    private readonly baseUrl = apiBase,
    private readonly fetcher: typeof fetch = globalThis.fetch.bind(globalThis),
  ) {}

  async respond(
    interactionId: string,
    request: CardActionRequest,
    signal?: AbortSignal,
  ): Promise<HitlResponseResult> {
    const response = await this.fetcher(
      `${this.baseUrl}/hitl/${encodeURIComponent(interactionId)}/responses`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": request.idempotencyKey,
        },
        credentials: "same-origin",
        signal,
        body: JSON.stringify({
          action: request.action,
          values: request.values,
          interaction_version: request.interactionVersion,
          card_revision: request.cardRevision,
          payload_hash: request.payloadHash,
          idempotency_key: request.idempotencyKey,
        }),
      },
    );

    const body = await readJson(response);
    const latestCard = findCard(body);
    if (response.status === 409 || response.status === 412) {
      throw new HitlConflictError(
        readMessage(body, "该交互已更新，正在加载最新内容。"),
        latestCard,
      );
    }
    if (response.status === 422) {
      throw new HitlFieldError(
        readMessage(body, "请检查表单内容。"),
        readFieldErrors(body),
        latestCard,
      );
    }
    if (!response.ok)
      throw new Error(readMessage(body, `提交失败（${response.status}）`));
    return {
      card: latestCard,
      duplicate: isRecord(body) && body.duplicate === true,
    };
  }

  async refresh(
    interactionId: string,
    signal?: AbortSignal,
  ): Promise<CardEnvelope> {
    const response = await this.fetcher(
      `${this.baseUrl}/hitl/${encodeURIComponent(interactionId)}`,
      {
        credentials: "same-origin",
        signal,
      },
    );
    const body = await readJson(response);
    if (!response.ok)
      throw new Error(readMessage(body, `刷新失败（${response.status}）`));
    const card = findCard(body);
    if (!card) throw new Error("服务端未返回可识别的交互卡片。");
    return card;
  }
}

function findCard(value: unknown): CardEnvelope | undefined {
  if (isCardEnvelope(value)) return value;
  if (!isRecord(value)) return undefined;
  return isCardEnvelope(value.card) ? value.card : undefined;
}

function readMessage(value: unknown, fallback: string): string {
  if (!isRecord(value)) return fallback;
  return typeof value.message === "string" ? value.message : fallback;
}

function readFieldErrors(value: unknown): Record<string, string> {
  if (!isRecord(value) || !isRecord(value.field_errors)) return {};
  return Object.fromEntries(
    Object.entries(value.field_errors).filter(
      (entry): entry is [string, string] => typeof entry[1] === "string",
    ),
  );
}

async function readJson(response: Response): Promise<JsonValue | undefined> {
  const text = await response.text();
  if (!text) return undefined;
  try {
    return JSON.parse(text) as JsonValue;
  } catch {
    return undefined;
  }
}

export function createIdempotencyKey(
  interactionId: string,
  revision: number,
  action: string,
): string {
  const random =
    globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`;
  return `${interactionId}:${revision}:${action}:${random}`;
}
