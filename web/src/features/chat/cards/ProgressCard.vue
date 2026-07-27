<script setup lang="ts">
import { computed } from "vue";

import type { CardEnvelope } from "../types";
import { asNumber, asString } from "../types";

const props = defineProps<{ card: CardEnvelope }>();
const emit = defineEmits<{ refresh: [] }>();
const progress = computed(() =>
  Math.max(0, Math.min(100, asNumber(props.card.data.progress) ?? 0)),
);
const isIndeterminate = computed(
  () => asNumber(props.card.data.progress) === undefined,
);
</script>

<template>
  <article class="card progress-card" aria-live="polite">
    <header>
      <span class="card-type">后台任务</span>
      <span class="state">{{
        card.state === "pending" ? "处理中" : "已结束"
      }}</span>
    </header>
    <h2>{{ asString(card.data.title, "任务进度") }}</h2>
    <p>{{ asString(card.data.message) }}</p>
    <div
      class="progress-track"
      role="progressbar"
      :aria-label="asString(card.data.current_step, '任务进度')"
      :aria-valuenow="isIndeterminate ? undefined : progress"
      aria-valuemin="0"
      aria-valuemax="100"
    >
      <span
        :class="{ indeterminate: isIndeterminate }"
        :style="{ width: isIndeterminate ? '36%' : `${progress}%` }"
      />
    </div>
    <div class="progress-meta">
      <span>{{ asString(card.data.current_step) }}</span>
      <span v-if="!isIndeterminate">{{ progress }}%</span>
    </div>
    <button
      v-if="card.actions.includes('retry')"
      type="button"
      class="secondary"
      @click="emit('refresh')"
    >
      重试
    </button>
  </article>
</template>

<style scoped>
.progress-track {
  height: 0.42rem;
  overflow: hidden;
  border-radius: 3px;
  background: var(--surface-subtle);
  margin-top: 0.8rem;
}
.progress-track span {
  display: block;
  height: 100%;
  background: var(--accent);
  transition: width 180ms ease;
}
.progress-track .indeterminate {
  animation: slide 1.4s ease-in-out infinite alternate;
}
.progress-meta {
  display: flex;
  justify-content: space-between;
  gap: 1rem;
  margin-top: 0.4rem;
  color: var(--text-muted);
  font-size: 0.78rem;
}
@keyframes slide {
  from {
    transform: translateX(-70%);
  }
  to {
    transform: translateX(180%);
  }
}
@media (prefers-reduced-motion: reduce) {
  .progress-track .indeterminate {
    animation: none;
  }
}
</style>
