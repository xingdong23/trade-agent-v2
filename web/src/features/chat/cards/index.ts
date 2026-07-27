import type { Component } from "vue";

import ArtifactCard from "./ArtifactCard.vue";
import InteractionCard from "./InteractionCard.vue";
import NoticeCard from "./NoticeCard.vue";
import ProgressCard from "./ProgressCard.vue";
import type { CardEnvelope } from "../types";

export type CardFamily = "interaction" | "artifact" | "progress" | "notice";

export interface RegisteredCardRenderer {
  readonly family: CardFamily;
  readonly component: Component;
}

const CARD_RENDERER_REGISTRY = new Map<string, RegisteredCardRenderer>();

function register(
  kind: string,
  family: CardFamily,
  component: Component,
): void {
  CARD_RENDERER_REGISTRY.set(`${kind}@1`, { family, component });
}

for (const kind of [
  "interaction.form",
  "interaction.choice",
  "interaction.approval",
  "interaction.review",
  "interaction.correction",
])
  register(kind, "interaction", InteractionCard);

for (const kind of [
  "artifact.research",
  "artifact.strategy",
  "artifact.quantitative_snapshot",
  "artifact.scan_result",
  "artifact.trade_plan",
  "artifact.reminder",
])
  register(kind, "artifact", ArtifactCard);

for (const kind of ["progress.research", "progress.scan"])
  register(kind, "progress", ProgressCard);
for (const kind of ["notice.unsupported", "notice.data_gap", "notice.failure"])
  register(kind, "notice", NoticeCard);

export function resolveCardRenderer(
  card: CardEnvelope,
): RegisteredCardRenderer | undefined {
  return CARD_RENDERER_REGISTRY.get(`${card.kind}@${card.schema_version}`);
}

export function isSupportedCard(card: CardEnvelope): boolean {
  return resolveCardRenderer(card) !== undefined;
}

export const supportedCardKeys = Object.freeze([
  ...CARD_RENDERER_REGISTRY.keys(),
]);
