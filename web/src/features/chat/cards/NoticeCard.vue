<script setup lang="ts">
import { computed } from "vue";

import type { CardEnvelope } from "../types";
import { asString } from "../types";

const props = defineProps<{ card: CardEnvelope }>();
const emit = defineEmits<{ refresh: [] }>();
const isFailure = computed(
  () =>
    props.card.state === "failed" ||
    typeof props.card.data.error_code === "string",
);
</script>

<template>
  <article
    class="card notice-card"
    :class="{ failure: isFailure }"
    role="status"
  >
    <header>
      <span class="card-type">{{ isFailure ? "处理失败" : "需要注意" }}</span>
      <span class="state">{{
        card.state === "failed" ? "未完成" : "信息"
      }}</span>
    </header>
    <h2>{{ asString(card.data.title, "暂时无法完成") }}</h2>
    <p>{{ asString(card.data.message, card.text_fallback) }}</p>
    <ul v-if="Array.isArray(card.data.missing_fields)">
      <li v-for="field in card.data.missing_fields" :key="String(field)">
        {{ String(field) }}
      </li>
    </ul>
    <button
      v-if="card.actions.includes('retry') || card.actions.includes('refresh')"
      type="button"
      class="secondary"
      @click="emit('refresh')"
    >
      {{ card.actions.includes("retry") ? "重试" : "刷新" }}
    </button>
  </article>
</template>

<style scoped>
.notice-card {
  border-left: 3px solid var(--warning);
}
.notice-card.failure {
  border-left-color: var(--danger);
}
ul {
  margin: 0.65rem 0;
  padding-left: 1.1rem;
  color: var(--text-secondary);
}
</style>
