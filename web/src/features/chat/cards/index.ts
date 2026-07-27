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
const CARD_FAMILY_RENDERER_REGISTRY = new Map<
  string,
  RegisteredCardRenderer
>();

function register(
  kind: string,
  family: CardFamily,
  component: Component,
): void {
  CARD_RENDERER_REGISTRY.set(`${kind}@1`, { family, component });
}

function registerFamily(
  family: Exclude<CardFamily, "interaction">,
  component: Component,
): void {
  CARD_FAMILY_RENDERER_REGISTRY.set(`${family}@1`, { family, component });
}

for (const kind of [
  "interaction.form",
  "interaction.choice",
  "interaction.approval",
  "interaction.review",
  "interaction.correction",
])
  register(kind, "interaction", InteractionCard);

registerFamily("artifact", ArtifactCard);
registerFamily("progress", ProgressCard);
registerFamily("notice", NoticeCard);

export function resolveCardRenderer(
  card: CardEnvelope,
): RegisteredCardRenderer | undefined {
  const exact = CARD_RENDERER_REGISTRY.get(
    `${card.kind}@${card.schema_version}`,
  );
  if (exact) return exact;
  const separator = card.kind.indexOf(".");
  if (separator < 1) return undefined;
  const family = card.kind.slice(0, separator);
  return CARD_FAMILY_RENDERER_REGISTRY.get(
    `${family}@${card.schema_version}`,
  );
}

export function isSupportedCard(card: CardEnvelope): boolean {
  return resolveCardRenderer(card) !== undefined;
}

export const supportedCardKeys = Object.freeze([
  ...CARD_RENDERER_REGISTRY.keys(),
  ...[...CARD_FAMILY_RENDERER_REGISTRY.keys()].map((key) =>
    key.replace("@", ".*@"),
  ),
]);
