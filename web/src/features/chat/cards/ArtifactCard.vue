<script setup lang="ts">
import { computed } from "vue";

import type { CardEnvelope } from "../types";
import { asRecords, asString } from "../types";

const props = defineProps<{ card: CardEnvelope }>();
const title = computed(() => asString(props.card.data.title, "分析结果"));
const summary = computed(() => asString(props.card.data.summary));
const sections = computed(() => asRecords(props.card.data.sections));
const provenance = computed(() => asRecords(props.card.data.provenance));
const typeLabel = computed(() =>
  asString(props.card.data.type_label, "结构化产物"),
);
</script>

<template>
  <article class="card artifact-card">
    <header>
      <span class="card-type">{{ typeLabel }}</span>
      <span class="state">{{
        card.state === "resolved" ? "已完成" : "已保存"
      }}</span>
    </header>
    <h2>{{ title }}</h2>
    <p class="summary">{{ summary }}</p>
    <section
      v-for="(section, index) in sections"
      :key="`${asString(section.title)}-${index}`"
    >
      <h3>{{ asString(section.title) }}</h3>
      <p>{{ asString(section.content) }}</p>
    </section>
    <details v-if="provenance.length">
      <summary>查看来源（{{ provenance.length }}）</summary>
      <dl>
        <template
          v-for="(source, index) in provenance"
          :key="`${asString(source.source_id)}-${index}`"
        >
          <dt>{{ asString(source.label, "来源") }}</dt>
          <dd>{{ asString(source.value) }}</dd>
        </template>
      </dl>
    </details>
  </article>
</template>

<style scoped>
.summary {
  color: var(--text);
  line-height: 1.65;
}
section {
  border-top: 1px solid var(--border);
  padding-top: 0.8rem;
  margin-top: 0.8rem;
}
h3 {
  font-size: 0.88rem;
  margin: 0 0 0.35rem;
}
section p {
  white-space: pre-wrap;
  margin: 0;
  color: var(--text-secondary);
}
details {
  margin-top: 0.9rem;
  color: var(--text-secondary);
}
summary {
  cursor: pointer;
  font-size: 0.82rem;
}
dl {
  display: grid;
  grid-template-columns: minmax(5rem, auto) 1fr;
  gap: 0.35rem 0.75rem;
  font-size: 0.8rem;
}
dt {
  font-weight: 650;
}
dd {
  margin: 0;
  overflow-wrap: anywhere;
}
</style>
