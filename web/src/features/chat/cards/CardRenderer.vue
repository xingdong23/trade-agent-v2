<script setup lang="ts">
import { computed } from "vue";

import UnknownCard from "./UnknownCard.vue";
import { resolveCardRenderer } from "./index";
import type { HitlResponseClient } from "../hitl";
import type { CardEnvelope } from "../types";

const props = defineProps<{
  card: CardEnvelope;
  hitlClient: HitlResponseClient;
}>();

const emit = defineEmits<{
  updated: [card: CardEnvelope];
  refresh: [];
}>();

const renderer = computed(() => resolveCardRenderer(props.card));
</script>

<template>
  <component
    :is="renderer.component"
    v-if="renderer"
    :card="card"
    :hitl-client="hitlClient"
    @updated="emit('updated', $event)"
    @refresh="emit('refresh')"
  />
  <UnknownCard v-else :card="card" @refresh="emit('refresh')" />
</template>
