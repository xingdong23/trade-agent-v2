<script setup lang="ts">
import type { CardEnvelope } from "../types";

defineProps<{ card: CardEnvelope }>();
const emit = defineEmits<{ refresh: [] }>();
</script>

<template>
  <article class="card unknown-card" aria-labelledby="unknown-card-title">
    <header>
      <span class="card-type">暂不支持的内容</span>
      <span class="state">只读</span>
    </header>
    <h2 id="unknown-card-title">需要更新后查看</h2>
    <p>{{ card.text_fallback || "当前客户端无法安全显示这张卡片。" }}</p>
    <p class="technical">{{ card.kind }} · v{{ card.schema_version }}</p>
    <button type="button" class="secondary" @click="emit('refresh')">
      刷新
    </button>
  </article>
</template>

<style scoped>
.technical {
  color: var(--text-muted);
  font-size: 0.78rem;
}
</style>
