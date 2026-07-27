<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";

import ChatTimeline from "./ChatTimeline.vue";
import {
  ChatRecoveryClient,
  emptySnapshot,
  mergeCards,
  mergeMessages,
  ReconnectingEventStream,
  reduceRunEvent,
  toTimeline,
  type ChatSnapshot,
} from "./events";
import { HttpHitlResponseClient } from "./hitl";
import type { CardEnvelope, ChatMessage } from "./types";

const apiBase = "/api";
const searchParameters = new URLSearchParams(window.location.search);
const threadId = searchParameters.get("thread") || "local";
const runStorageKey = `trade-agent:thread:${threadId}:run`;
const cursorStorageKey = `trade-agent:thread:${threadId}:cursor`;
const activeRunId = ref(
  searchParameters.get("run") || sessionStorage.getItem(runStorageKey) || "",
);
const recoveryClient = new ChatRecoveryClient(apiBase);
const hitlClient = new HttpHitlResponseClient(apiBase);
const snapshot = ref<ChatSnapshot>({
  ...emptySnapshot(threadId),
  cursor: sessionStorage.getItem(cursorStorageKey) || "",
});
const connectionStatus = ref<"idle" | "connecting" | "open" | "retrying">(
  "idle",
);
const recoveryError = ref("");
const loading = ref(true);
const draft = ref("");
const sending = ref(false);
const composer = ref<HTMLTextAreaElement>();
let stream: ReconnectingEventStream | undefined;
let recoveryController: AbortController | undefined;

const entries = computed(() => toTimeline(snapshot.value));
const statusLabel = computed(
  () =>
    ({
      idle: "待命",
      connecting: "连接中",
      open: "已连接",
      retrying: "正在重连",
    })[connectionStatus.value],
);

onMounted(() => void recover());
onBeforeUnmount(() => {
  recoveryController?.abort();
  stream?.close();
});

async function recover(): Promise<void> {
  recoveryController?.abort();
  recoveryController = new AbortController();
  loading.value = true;
  recoveryError.value = "";
  try {
    const recovered = await recoveryClient.recover(
      threadId,
      recoveryController.signal,
    );
    let next = emptySnapshot(threadId);
    next = mergeCards(next, [
      ...recovered.pending,
      ...recovered.artifacts,
      ...recovered.jobs,
    ]);
    next = mergeMessages(next, recovered.messages);
    snapshot.value = {
      ...next,
      cursor:
        recovered.cursor || sessionStorage.getItem(cursorStorageKey) || "",
    };
    connectEvents();
  } catch (error) {
    if (!(error instanceof DOMException && error.name === "AbortError")) {
      recoveryError.value = "暂时无法恢复完整对话，请稍后重试。";
      connectEvents();
    }
  } finally {
    loading.value = false;
  }
}

function connectEvents(): void {
  stream?.close();
  if (!activeRunId.value) {
    connectionStatus.value = "idle";
    return;
  }
  const url = `${apiBase}/runs/${encodeURIComponent(activeRunId.value)}/events`;
  stream = new ReconnectingEventStream(
    url,
    {
      onEvent: (event) => {
        snapshot.value = reduceRunEvent(snapshot.value, event);
        sessionStorage.setItem(cursorStorageKey, snapshot.value.cursor);
      },
      onStatus: (status) => {
        connectionStatus.value = status;
      },
    },
    "after",
  );
  stream.connect(snapshot.value.cursor);
}

function updateCard(card: CardEnvelope): void {
  snapshot.value = mergeCards(snapshot.value, [card]);
}

async function sendMessage(): Promise<void> {
  const content = draft.value.trim();
  if (!content || sending.value) return;
  sending.value = true;
  recoveryError.value = "";
  const optimistic: ChatMessage = {
    id: `local-${Date.now()}`,
    role: "user",
    content,
    sequence:
      Math.max(
        0,
        ...snapshot.value.messages.map((message) => message.sequence),
      ) + 1,
  };
  snapshot.value = mergeMessages(snapshot.value, [optimistic]);
  draft.value = "";
  try {
    const response = await fetch(`${apiBase}/conversations/runs`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({ thread_id: threadId, message: content }),
    });
    if (!response.ok) throw new Error(`发送失败（${response.status}）`);
    const body: unknown = await response.json();
    if (
      typeof body === "object" &&
      body !== null &&
      "run_id" in body &&
      typeof body.run_id === "string"
    ) {
      activeRunId.value = body.run_id;
      sessionStorage.setItem(runStorageKey, body.run_id);
      const url = new URL(window.location.href);
      url.searchParams.set("thread", threadId);
      url.searchParams.set("run", body.run_id);
      window.history.replaceState(null, "", url);
      connectEvents();
    }
  } catch (error) {
    recoveryError.value =
      error instanceof Error ? error.message : "消息发送失败。";
    draft.value = content;
    snapshot.value = {
      ...snapshot.value,
      messages: snapshot.value.messages.filter(
        (message) => message.id !== optimistic.id,
      ),
    };
  } finally {
    sending.value = false;
    composer.value?.focus();
  }
}

function onComposerKeydown(event: KeyboardEvent): void {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    void sendMessage();
  }
}
</script>

<template>
  <div class="chat-shell">
    <header class="app-header">
      <div>
        <span class="brand-mark" aria-hidden="true">TA</span>
        <div><strong>Trade Agent</strong><span>美股研究与计划</span></div>
      </div>
      <span class="connection" :class="connectionStatus"
        ><i />{{ statusLabel }}</span
      >
    </header>

    <main>
      <div v-if="loading" class="loading" role="status">正在恢复对话…</div>
      <div v-if="recoveryError" class="recovery-error" role="alert">
        <span>{{ recoveryError }}</span>
        <button type="button" class="secondary" @click="recover">
          重新加载
        </button>
      </div>
      <ChatTimeline
        :entries="entries"
        :hitl-client="hitlClient"
        @updated="updateCard"
        @refresh="recover"
      />
    </main>

    <footer class="composer-bar">
      <form class="composer" @submit.prevent="sendMessage">
        <label class="sr-only" for="message-input">输入消息</label>
        <textarea
          id="message-input"
          ref="composer"
          v-model="draft"
          rows="1"
          placeholder="输入美股代码、研究主题或计划需求"
          :disabled="sending"
          @keydown="onComposerKeydown"
        />
        <button
          class="send-button"
          type="submit"
          :disabled="sending || !draft.trim()"
          title="发送消息"
          aria-label="发送消息"
        >
          ↑
        </button>
      </form>
      <p>内容仅用于研究和决策辅助，不提供经纪商下单能力。</p>
    </footer>
  </div>
</template>

<style scoped>
.chat-shell {
  min-height: 100dvh;
  display: grid;
  grid-template-rows: auto minmax(0, 1fr) auto;
  background: var(--page);
}
.app-header {
  position: sticky;
  top: 0;
  z-index: 10;
  min-height: 3.5rem;
  border-bottom: 1px solid var(--border);
  background: color-mix(in srgb, var(--surface) 96%, transparent);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
  padding: 0.55rem max(1rem, calc((100vw - 52rem) / 2));
  backdrop-filter: blur(8px);
}
.app-header > div {
  display: flex;
  align-items: center;
  gap: 0.65rem;
  min-width: 0;
}
.app-header > div > div {
  display: grid;
  min-width: 0;
}
.app-header strong {
  font-size: 0.94rem;
}
.app-header span {
  color: var(--text-muted);
  font-size: 0.72rem;
}
.brand-mark {
  display: grid;
  place-items: center;
  width: 2rem;
  height: 2rem;
  flex: 0 0 2rem;
  border-radius: 5px;
  background: var(--text);
  color: var(--surface) !important;
  font-weight: 800;
}
.connection {
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  white-space: nowrap;
}
.connection i {
  width: 0.45rem;
  height: 0.45rem;
  border-radius: 50%;
  background: var(--warning);
}
.connection.open i {
  background: var(--success);
}
main {
  min-height: 0;
  overflow-y: auto;
}
.loading {
  text-align: center;
  color: var(--text-muted);
  padding: 0.75rem;
  font-size: 0.82rem;
}
.recovery-error {
  width: min(calc(100% - 2rem), 46rem);
  margin: 0.75rem auto 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
  padding: 0.65rem 0.75rem;
  border: 1px solid var(--danger-soft);
  background: var(--danger-bg);
  color: var(--danger);
  font-size: 0.82rem;
}
.composer-bar {
  position: sticky;
  bottom: 0;
  z-index: 10;
  border-top: 1px solid var(--border);
  background: var(--surface);
  padding: 0.7rem max(1rem, calc((100vw - 50rem) / 2))
    max(0.55rem, env(safe-area-inset-bottom));
}
.composer {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 2.3rem;
  gap: 0.5rem;
  align-items: end;
}
.composer textarea {
  width: 100%;
  min-height: 2.35rem;
  max-height: 8rem;
  box-sizing: border-box;
  resize: vertical;
  border: 1px solid var(--border-strong);
  border-radius: 6px;
  padding: 0.55rem 0.7rem;
  background: var(--surface);
  color: var(--text);
  font: inherit;
  line-height: 1.35;
}
.composer textarea:focus {
  outline: 2px solid var(--focus);
  outline-offset: 1px;
  border-color: var(--accent);
}
.send-button {
  width: 2.3rem;
  height: 2.3rem;
  padding: 0;
  border: 0;
  border-radius: 5px;
  background: var(--accent);
  color: white;
  font-size: 1.15rem;
  cursor: pointer;
}
.send-button:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
.composer-bar p {
  margin: 0.35rem 0 0;
  color: var(--text-muted);
  font-size: 0.68rem;
  text-align: center;
}
@media (max-width: 36rem) {
  .app-header {
    padding-inline: 0.7rem;
  }
  .app-header > div > div span {
    display: none;
  }
  .recovery-error {
    width: calc(100% - 1.3rem);
    box-sizing: border-box;
    align-items: flex-start;
  }
  .composer-bar {
    padding-inline: 0.65rem;
  }
}
</style>
