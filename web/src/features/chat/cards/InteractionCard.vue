<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from "vue";

import {
  createIdempotencyKey,
  HitlConflictError,
  HitlFieldError,
  type HitlResponseClient,
} from "../hitl";
import type { CardEnvelope, JsonValue } from "../types";
import { asRecord, asRecords, asString } from "../types";

interface FieldModel {
  readonly key: string;
  readonly label: string;
  readonly dataType: string;
  readonly controlType: string;
  readonly required: boolean;
  readonly readOnly: boolean;
  readonly constraints: Record<string, JsonValue>;
  readonly options: Record<string, JsonValue>[];
  readonly error: string;
  readonly visibleIf: Record<string, JsonValue>;
}

const props = defineProps<{
  card: CardEnvelope;
  hitlClient: HitlResponseClient;
}>();
const emit = defineEmits<{
  updated: [card: CardEnvelope];
  refresh: [];
}>();

const values = ref<Record<string, JsonValue>>({});
const fieldErrors = ref<Record<string, string>>({});
const submissionError = ref("");
const submitting = ref(false);
const activeRequest = ref<AbortController>();
const lastFocusedField = ref("");

const isPending = computed(() => props.card.state === "pending");
const title = computed(() => asString(props.card.data.title, "需要你的确认"));
const description = computed(() => asString(props.card.data.description));
const fields = computed<FieldModel[]>(() =>
  asRecords(props.card.data.fields)
    .map((field) => ({
      key: asString(field.key),
      label: asString(field.label),
      dataType: asString(field.data_type, "string"),
      controlType: asString(field.control_type, "text"),
      required: field.required === true,
      readOnly: field.read_only === true,
      constraints: asRecord(field.constraints),
      options: asRecords(field.options),
      error: asString(field.error),
      visibleIf: asRecord(field.visible_if),
    }))
    .filter((field) => field.key && field.label),
);
const choiceOptions = computed(() => asRecords(props.card.data.options));
const facts = computed(() =>
  asRecords(props.card.data.facts ?? props.card.data.findings),
);
const isChoice = computed(() => props.card.kind === "interaction.choice");
const isForm = computed(() => props.card.kind === "interaction.form");
const actions = computed(() => {
  const allowed =
    props.card.kind === "interaction.form" ||
    props.card.kind === "interaction.choice"
      ? new Set(["continue", "cancel"])
      : new Set(["confirm", "edit", "cancel"]);
  return props.card.actions.filter((action) => allowed.has(action));
});
const primaryAction = computed(() => {
  if (actions.value.includes("confirm")) return "confirm";
  if (actions.value.includes("continue")) return "continue";
  return actions.value[0] ?? "continue";
});
const canSubmit = computed(() => {
  if (!isPending.value || submitting.value) return false;
  if (isChoice.value)
    return (
      typeof values.value.choice === "string" && values.value.choice.length > 0
    );
  return fields.value
    .filter((field) => field.required && isVisible(field))
    .every((field) => hasValue(values.value[field.key]));
});

watch(
  () => `${props.card.card_id}:${props.card.revision}`,
  async () => {
    initialiseValues();
    fieldErrors.value = {};
    await restoreFocus();
  },
  { immediate: true },
);

onBeforeUnmount(() => activeRequest.value?.abort());

function initialiseValues(): void {
  const next: Record<string, JsonValue> = {};
  for (const field of fields.value) next[field.key] = fieldValue(field.key);
  if (isChoice.value && typeof values.value.choice === "string")
    next.choice = values.value.choice;
  values.value = next;
}

function fieldValue(key: string): JsonValue {
  const original = asRecords(props.card.data.fields).find(
    (field) => field.key === key,
  )?.value;
  if (original !== undefined) return original;
  const field = fields.value.find((candidate) => candidate.key === key);
  return field?.dataType === "boolean" ? false : "";
}

function updateValue(key: string, value: JsonValue): void {
  values.value = { ...values.value, [key]: value };
  if (fieldErrors.value[key])
    fieldErrors.value = { ...fieldErrors.value, [key]: "" };
}

function isVisible(field: FieldModel): boolean {
  const key = asString(field.visibleIf.field_key);
  return !key || values.value[key] === field.visibleIf.equals;
}

function hasValue(value: JsonValue | undefined): boolean {
  return value !== undefined && value !== null && value !== "";
}

function errorFor(field: FieldModel): string {
  return fieldErrors.value[field.key] || field.error;
}

function rememberFocus(event: FocusEvent): void {
  const target = event.target;
  if (target instanceof HTMLElement && target.dataset.fieldKey)
    lastFocusedField.value = target.dataset.fieldKey;
}

async function restoreFocus(): Promise<void> {
  await nextTick();
  const key = lastFocusedField.value;
  const selector = key
    ? `[data-field-key="${CSS.escape(key)}"]`
    : "[data-first-field]";
  document.querySelector<HTMLElement>(selector)?.focus({ preventScroll: true });
}

async function submit(action = primaryAction.value): Promise<void> {
  if (submitting.value || !isPending.value) return;
  if (action !== "cancel" && !canSubmit.value) return;
  const focused = document.activeElement;
  if (focused instanceof HTMLElement && focused.dataset.fieldKey)
    lastFocusedField.value = focused.dataset.fieldKey;
  submitting.value = true;
  submissionError.value = "";
  fieldErrors.value = {};
  const controller = new AbortController();
  activeRequest.value = controller;
  try {
    const result = await props.hitlClient.respond(
      props.card.source.source_id,
      {
        action,
        values: values.value,
        interactionVersion: props.card.source.version,
        cardRevision: props.card.revision,
        payloadHash: props.card.payload_hash,
        idempotencyKey: createIdempotencyKey(
          props.card.source.source_id,
          props.card.revision,
          action,
        ),
      },
      controller.signal,
    );
    if (result.card) emit("updated", result.card);
    else emit("refresh");
  } catch (error) {
    if (error instanceof HitlFieldError) {
      fieldErrors.value = { ...error.fieldErrors };
      submissionError.value = error.message;
      if (error.latestCard) emit("updated", error.latestCard);
    } else if (error instanceof HitlConflictError) {
      submissionError.value = error.message;
      if (error.latestCard) emit("updated", error.latestCard);
      else {
        try {
          emit(
            "updated",
            await props.hitlClient.refresh(props.card.source.source_id),
          );
        } catch (refreshError) {
          submissionError.value =
            refreshError instanceof Error
              ? refreshError.message
              : "刷新失败，请稍后再试。";
        }
      }
    } else if (!(
      error instanceof DOMException && error.name === "AbortError"
    )) {
      submissionError.value =
        error instanceof Error ? error.message : "提交失败，请稍后再试。";
    }
  } finally {
    submitting.value = false;
    activeRequest.value = undefined;
    await restoreFocus();
  }
}

function onKeydown(event: KeyboardEvent): void {
  if (event.key === "Escape" && actions.value.includes("cancel")) {
    event.preventDefault();
    void submit("cancel");
  }
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
    event.preventDefault();
    void submit();
  }
}

function actionLabel(action: string): string {
  return (
    { continue: "继续", confirm: "确认", edit: "修改", cancel: "取消" }[
      action
    ] ?? "提交"
  );
}
</script>

<template>
  <article class="card interaction-card" @keydown="onKeydown">
    <header>
      <span class="card-type">需要操作</span>
      <span class="state">{{ isPending ? "待处理" : "已结束" }}</span>
    </header>
    <h2>{{ title }}</h2>
    <p v-if="description" class="description">{{ description }}</p>

    <form @submit.prevent="submit()">
      <fieldset :disabled="submitting || !isPending">
        <legend class="sr-only">{{ title }}</legend>

        <div v-if="isChoice" class="choice-list">
          <label
            v-for="(option, index) in choiceOptions"
            :key="asString(option.key)"
            :class="{ disabled: option.disabled === true }"
          >
            <input
              v-model="values.choice"
              type="radio"
              name="choice"
              :value="asString(option.key)"
              :disabled="option.disabled === true"
              :data-first-field="index === 0 || undefined"
              data-field-key="choice"
              @focus="rememberFocus"
            />
            <span
              ><strong>{{ asString(option.label) }}</strong
              ><small v-if="asString(option.description)">{{
                asString(option.description)
              }}</small></span
            >
          </label>
        </div>

        <div v-if="isForm" class="field-grid">
          <div
            v-for="(field, index) in fields"
            v-show="isVisible(field)"
            :key="field.key"
            class="field"
            :class="{ checkbox: field.controlType === 'checkbox' }"
          >
            <label :for="`${card.card_id}-${field.key}`"
              >{{ field.label
              }}<span v-if="field.required" aria-hidden="true"> *</span></label
            >
            <textarea
              v-if="field.controlType === 'textarea'"
              :id="`${card.card_id}-${field.key}`"
              :value="String(values[field.key] ?? '')"
              :readonly="field.readOnly"
              :required="field.required"
              :minlength="Number(field.constraints.min_length) || undefined"
              :maxlength="Number(field.constraints.max_length) || undefined"
              :aria-invalid="Boolean(errorFor(field))"
              :aria-describedby="
                errorFor(field)
                  ? `${card.card_id}-${field.key}-error`
                  : undefined
              "
              :data-first-field="index === 0 || undefined"
              :data-field-key="field.key"
              @input="
                updateValue(
                  field.key,
                  ($event.target as HTMLTextAreaElement).value,
                )
              "
              @focus="rememberFocus"
            />
            <select
              v-else-if="field.controlType === 'select'"
              :id="`${card.card_id}-${field.key}`"
              :value="String(values[field.key] ?? '')"
              :disabled="field.readOnly"
              :required="field.required"
              :aria-invalid="Boolean(errorFor(field))"
              :aria-describedby="
                errorFor(field)
                  ? `${card.card_id}-${field.key}-error`
                  : undefined
              "
              :data-first-field="index === 0 || undefined"
              :data-field-key="field.key"
              @change="
                updateValue(
                  field.key,
                  ($event.target as HTMLSelectElement).value,
                )
              "
              @focus="rememberFocus"
            >
              <option value="">请选择</option>
              <option
                v-for="option in field.options"
                :key="asString(option.key)"
                :value="asString(option.key)"
                :disabled="option.disabled === true"
              >
                {{ asString(option.label) }}
              </option>
            </select>
            <input
              v-else-if="field.controlType === 'checkbox'"
              :id="`${card.card_id}-${field.key}`"
              type="checkbox"
              :checked="values[field.key] === true"
              :disabled="field.readOnly"
              :data-first-field="index === 0 || undefined"
              :data-field-key="field.key"
              @change="
                updateValue(
                  field.key,
                  ($event.target as HTMLInputElement).checked,
                )
              "
              @focus="rememberFocus"
            />
            <input
              v-else
              :id="`${card.card_id}-${field.key}`"
              :type="
                field.controlType === 'number'
                  ? 'number'
                  : field.controlType === 'date'
                    ? 'date'
                    : 'text'
              "
              :value="String(values[field.key] ?? '')"
              :readonly="field.readOnly"
              :required="field.required"
              :min="
                typeof field.constraints.min === 'number'
                  ? field.constraints.min
                  : undefined
              "
              :max="
                typeof field.constraints.max === 'number'
                  ? field.constraints.max
                  : undefined
              "
              :minlength="Number(field.constraints.min_length) || undefined"
              :maxlength="Number(field.constraints.max_length) || undefined"
              :pattern="
                typeof field.constraints.pattern === 'string'
                  ? field.constraints.pattern
                  : undefined
              "
              :aria-invalid="Boolean(errorFor(field))"
              :aria-describedby="
                errorFor(field)
                  ? `${card.card_id}-${field.key}-error`
                  : undefined
              "
              :data-first-field="index === 0 || undefined"
              :data-field-key="field.key"
              @input="
                updateValue(
                  field.key,
                  field.controlType === 'number'
                    ? Number(($event.target as HTMLInputElement).value)
                    : ($event.target as HTMLInputElement).value,
                )
              "
              @focus="rememberFocus"
            />
            <p
              v-if="errorFor(field)"
              :id="`${card.card_id}-${field.key}-error`"
              class="field-error"
              role="alert"
            >
              {{ errorFor(field) }}
            </p>
          </div>
        </div>

        <div
          v-if="card.kind === 'interaction.approval'"
          class="approval-summary"
        >
          <p>{{ asString(card.data.summary) }}</p>
        </div>
        <div v-if="card.kind === 'interaction.correction'" class="comparison">
          <div>
            <span>当前内容</span>
            <p>{{ asString(card.data.current_value) }}</p>
          </div>
          <div>
            <span>建议修改</span>
            <p>{{ asString(card.data.suggested_value) }}</p>
          </div>
        </div>
        <ul v-if="facts.length" class="facts">
          <li
            v-for="(fact, index) in facts"
            :key="`${asString(fact.label)}-${index}`"
          >
            <strong>{{ asString(fact.label) }}</strong
            ><span>{{ asString(fact.detail) }}</span>
          </li>
        </ul>
      </fieldset>

      <p v-if="submissionError" class="submission-error" role="alert">
        {{ submissionError }}
      </p>
      <div class="actions">
        <button
          v-for="action in actions"
          :key="action"
          type="button"
          :class="action === primaryAction ? 'primary' : 'secondary'"
          :disabled="
            submitting || !isPending || (action === primaryAction && !canSubmit)
          "
          @click="submit(action)"
        >
          {{
            submitting && action === primaryAction
              ? "提交中…"
              : actionLabel(action)
          }}
        </button>
      </div>
    </form>
  </article>
</template>

<style scoped>
fieldset {
  border: 0;
  padding: 0;
  margin: 0;
  min-width: 0;
}
.description {
  color: var(--text-secondary);
}
.field-grid {
  display: grid;
  gap: 0.9rem;
}
.field {
  display: grid;
  gap: 0.35rem;
  min-width: 0;
}
.field > label {
  font-size: 0.82rem;
  font-weight: 650;
  color: var(--text-secondary);
}
.field input:not([type="checkbox"]),
.field textarea,
.field select {
  width: 100%;
  min-width: 0;
  box-sizing: border-box;
  border: 1px solid var(--border-strong);
  border-radius: 5px;
  background: var(--surface);
  color: var(--text);
  padding: 0.65rem 0.7rem;
  font: inherit;
}
.field textarea {
  min-height: 6.5rem;
  resize: vertical;
}
.field input:focus,
.field textarea:focus,
.field select:focus {
  outline: 2px solid var(--focus);
  outline-offset: 1px;
  border-color: var(--accent);
}
.field input[aria-invalid="true"],
.field textarea[aria-invalid="true"],
.field select[aria-invalid="true"] {
  border-color: var(--danger);
}
.field.checkbox {
  grid-template-columns: 1fr auto;
  align-items: center;
}
.field.checkbox .field-error {
  grid-column: 1 / -1;
}
.field-error,
.submission-error {
  color: var(--danger);
  font-size: 0.8rem;
  margin: 0;
}
.submission-error {
  margin-top: 0.75rem;
}
.choice-list {
  display: grid;
  gap: 0.55rem;
}
.choice-list label {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  align-items: start;
  gap: 0.65rem;
  border: 1px solid var(--border);
  border-radius: 5px;
  padding: 0.7rem;
  cursor: pointer;
}
.choice-list label:has(input:checked) {
  border-color: var(--accent);
  background: var(--accent-soft);
}
.choice-list label.disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.choice-list input {
  margin-top: 0.18rem;
}
.choice-list span {
  display: grid;
  gap: 0.15rem;
  min-width: 0;
}
.choice-list small {
  color: var(--text-muted);
  overflow-wrap: anywhere;
}
.approval-summary {
  padding: 0.75rem;
  background: var(--surface-subtle);
  border-left: 3px solid var(--warning);
}
.approval-summary p {
  margin: 0;
  white-space: pre-wrap;
}
.comparison {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0.65rem;
}
.comparison > div {
  padding: 0.7rem;
  background: var(--surface-subtle);
  min-width: 0;
}
.comparison span {
  font-size: 0.75rem;
  color: var(--text-muted);
}
.comparison p {
  overflow-wrap: anywhere;
  white-space: pre-wrap;
}
.facts {
  list-style: none;
  padding: 0;
  display: grid;
  gap: 0.45rem;
}
.facts li {
  display: grid;
  grid-template-columns: minmax(5rem, 0.35fr) minmax(0, 1fr);
  gap: 0.65rem;
  font-size: 0.84rem;
}
.facts span {
  overflow-wrap: anywhere;
}
.actions {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 0.5rem;
  margin-top: 1rem;
}
@media (max-width: 36rem) {
  .comparison {
    grid-template-columns: 1fr;
  }
  .facts li {
    grid-template-columns: 1fr;
    gap: 0.15rem;
  }
  .actions {
    display: grid;
    grid-template-columns: 1fr;
  }
  .actions button {
    width: 100%;
  }
}
</style>
