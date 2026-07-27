<script setup lang="ts">
import { ChatShell } from "./features/chat";
</script>

<template>
  <ChatShell />
</template>

<style>
:root {
  color-scheme: light;
  font-family:
    Inter,
    ui-sans-serif,
    system-ui,
    -apple-system,
    BlinkMacSystemFont,
    "Segoe UI",
    sans-serif;
  font-synthesis: none;
  --page: #f4f6f5;
  --surface: #ffffff;
  --surface-subtle: #eef1ef;
  --border: #dce1de;
  --border-strong: #aeb8b2;
  --text: #18211c;
  --text-secondary: #47534c;
  --text-muted: #6d7871;
  --accent: #146b4a;
  --accent-soft: #e8f3ed;
  --focus: #82bda5;
  --success: #198754;
  --warning: #b26a00;
  --danger: #b42318;
  --danger-soft: #e8aaa5;
  --danger-bg: #fff4f2;
  --user-message: #e3ece7;
}

* {
  box-sizing: border-box;
}
html,
body,
#app {
  min-width: 20rem;
  min-height: 100%;
  margin: 0;
}
body {
  color: var(--text);
  background: var(--page);
}
button,
input,
textarea,
select {
  letter-spacing: 0;
}
button {
  font: inherit;
}
.sr-only {
  position: absolute !important;
  width: 1px !important;
  height: 1px !important;
  padding: 0 !important;
  margin: -1px !important;
  overflow: hidden !important;
  clip: rect(0, 0, 0, 0) !important;
  white-space: nowrap !important;
  border: 0 !important;
}
.card {
  width: min(100%, 42rem);
  align-self: flex-start;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--surface);
  padding: 1rem;
  box-shadow: 0 1px 2px rgb(20 35 27 / 5%);
  overflow: hidden;
}
.card > header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 1rem;
  margin-bottom: 0.65rem;
}
.card-type {
  color: var(--accent);
  font-size: 0.72rem;
  font-weight: 750;
}
.state {
  color: var(--text-muted);
  font-size: 0.72rem;
  white-space: nowrap;
}
.card h2 {
  margin: 0 0 0.55rem;
  font-size: 1rem;
  line-height: 1.35;
  overflow-wrap: anywhere;
}
.card p {
  overflow-wrap: anywhere;
}
.primary,
.secondary {
  min-height: 2.25rem;
  border-radius: 5px;
  padding: 0.48rem 0.8rem;
  cursor: pointer;
  white-space: nowrap;
}
.primary {
  border: 1px solid var(--accent);
  background: var(--accent);
  color: white;
}
.secondary {
  border: 1px solid var(--border-strong);
  background: var(--surface);
  color: var(--text);
}
.primary:disabled,
.secondary:disabled {
  opacity: 0.48;
  cursor: not-allowed;
}
.primary:focus-visible,
.secondary:focus-visible,
button:focus-visible {
  outline: 2px solid var(--focus);
  outline-offset: 2px;
}

@media (prefers-color-scheme: dark) {
  :root {
    color-scheme: dark;
    --page: #111714;
    --surface: #18201c;
    --surface-subtle: #222c27;
    --border: #303c35;
    --border-strong: #66736c;
    --text: #edf2ef;
    --text-secondary: #c4cec8;
    --text-muted: #98a59e;
    --accent: #43a77d;
    --accent-soft: #17382a;
    --focus: #73c7a3;
    --user-message: #253b31;
    --danger-bg: #351c1a;
  }
}
</style>
