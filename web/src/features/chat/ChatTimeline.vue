<script setup lang="ts">
import { nextTick, ref, watch } from "vue";

import CardRenderer from "./cards/CardRenderer.vue";
import type { HitlResponseClient } from "./hitl";
import type { CardEnvelope, TimelineEntry } from "./types";

const props = defineProps<{
  entries: readonly TimelineEntry[];
  hitlClient: HitlResponseClient;
}>();
const emit = defineEmits<{
  updated: [card: CardEnvelope];
  refresh: [];
}>();
const timeline = ref<HTMLElement>();

watch(
  () => props.entries.length,
  async () => {
    await nextTick();
    timeline.value?.lastElementChild?.scrollIntoView({
      behavior: "smooth",
      block: "nearest",
    });
  },
);
</script>

<template>
  <section
    ref="timeline"
    class="timeline"
    aria-label="对话记录"
    aria-live="polite"
  >
    <div v-if="entries.length === 0" class="empty-state">
      <h2>开始一项研究</h2>
      <p>输入美股代码、研究主题，或描述需要整理的策略。</p>
    </div>
    <template v-for="entry in entries" :key="`${entry.type}-${entry.id}`">
      <article
        v-if="entry.type === 'message'"
        class="message"
        :class="entry.message.role"
      >
        <span class="speaker">{{
          entry.message.role === "user"
            ? "你"
            : entry.message.role === "assistant"
              ? "Trade Agent"
              : "系统"
        }}</span>
        <p>{{ entry.message.content }}</p>
      </article>
      <CardRenderer
        v-else
        :card="entry.card"
        :hitl-client="hitlClient"
        @updated="emit('updated', $event)"
        @refresh="emit('refresh')"
      />
    </template>
  </section>
</template>

<style scoped>
.timeline {
  width: min(100%, 48rem);
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: 1rem;
  padding: 1.25rem 1rem 2rem;
}
.empty-state {
  margin: min(16vh, 7rem) auto 2rem;
  max-width: 30rem;
  text-align: center;
  color: var(--text-secondary);
}
.empty-state h2 {
  color: var(--text);
  font-size: 1.25rem;
  margin-bottom: 0.35rem;
}
.empty-state p {
  margin: 0;
}
.message {
  max-width: min(85%, 38rem);
}
.message.user {
  align-self: flex-end;
  border-radius: 6px 6px 2px 6px;
  padding: 0.7rem 0.85rem;
  background: var(--user-message);
}
.message.assistant,
.message.system {
  align-self: flex-start;
}
.speaker {
  display: block;
  margin-bottom: 0.28rem;
  color: var(--text-muted);
  font-size: 0.72rem;
  font-weight: 700;
}
.message p {
  margin: 0;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  line-height: 1.55;
}
@media (max-width: 36rem) {
  .timeline {
    padding: 0.9rem 0.65rem 1.5rem;
  }
  .message {
    max-width: 92%;
  }
}
</style>
