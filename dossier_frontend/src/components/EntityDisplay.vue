<script setup>
// Generic entity-content renderer. The backend returns entities with
// `type`, `entityId`, `versionId`, and `content` (a dict of whatever
// schema that type defines). Rather than building bespoke renderers
// per type, we render keys/values as a definition list and special-
// case a few known fields for readability.

import { computed } from "vue";

const props = defineProps({
  entity: { type: Object, required: true },
});

// Label maps: translate the machine type to a human heading.
const TYPE_LABELS = {
  "oe:aanvraag": "Aanvraag",
  "oe:beslissing": "Beslissing",
  "oe:verantwoordelijke_organisatie": "Verantwoordelijke organisatie",
  "oe:dossier_access": "Toegangsrechten",
  "oe:aanvrager_rrn": "Aanvrager (RRN)",
  "oe:aanvrager_kbo": "Aanvrager (KBO)",
  "oe:systeem_velden": "Systeemvelden",
};
const typeLabel = computed(() => TYPE_LABELS[props.entity.type] ?? props.entity.type);

// Fields that should render differently (e.g. beslissing outcome as
// a coloured pill, URI fields as clickable links).
function renderValue(key, val) {
  if (val == null || val === "") return { kind: "empty" };
  if (Array.isArray(val)) {
    return { kind: "list", value: val };
  }
  if (typeof val === "object") {
    return { kind: "object", value: val };
  }
  if (typeof val === "string" && /^https?:\/\//.test(val)) {
    return { kind: "url", value: val };
  }
  if (key === "beslissing") {
    return { kind: "pill", value: val };
  }
  return { kind: "text", value: val };
}

const outcomeColours = {
  goedgekeurd: "bg-status-approved",
  afgekeurd: "bg-status-rejected",
  onvolledig: "bg-status-review",
};

function pillClass(v) {
  return outcomeColours[v] ?? "bg-status-draft";
}

// Ordered iteration of content entries. Display order follows the
// object's insertion order (the backend sends the schema order).
const entries = computed(() => {
  const c = props.entity.content ?? {};
  return Object.entries(c);
});
</script>

<template>
  <article class="paper-card">
    <header class="px-5 py-3 border-b border-ink-line flex items-center justify-between gap-4">
      <div>
        <p class="label-eyebrow mb-0.5">{{ entity.type }}</p>
        <h3 class="font-display text-xl text-ink leading-tight">{{ typeLabel }}</h3>
      </div>
      <div class="text-right text-[11px] font-mono text-ink-soft shrink-0">
        <div>v: {{ entity.versionId?.slice(0, 8) }}</div>
        <div>e: {{ entity.entityId?.slice(0, 8) }}</div>
      </div>
    </header>

    <dl class="px-5 py-4 space-y-3">
      <div
        v-for="[k, v] in entries"
        :key="k"
        class="grid grid-cols-[180px_1fr] gap-4 items-start"
      >
        <dt class="label-eyebrow pt-[3px]">
          {{ k }}
        </dt>
        <dd class="text-sm text-ink min-w-0 break-words">
          <template v-if="renderValue(k, v).kind === 'pill'">
            <span class="pill" :class="pillClass(v)">{{ v }}</span>
          </template>
          <template v-else-if="renderValue(k, v).kind === 'url'">
            <a
              :href="v"
              target="_blank"
              rel="noopener"
              class="btn-link font-mono text-xs"
            >{{ v }}</a>
          </template>
          <template v-else-if="renderValue(k, v).kind === 'list'">
            <ul class="space-y-1">
              <li
                v-for="(item, i) in v"
                :key="i"
                class="text-sm"
              >
                <template v-if="typeof item === 'object'">
                  <pre class="font-mono text-[11px] text-ink-muted whitespace-pre-wrap">{{ JSON.stringify(item, null, 2) }}</pre>
                </template>
                <template v-else>{{ item }}</template>
              </li>
              <li v-if="v.length === 0" class="text-ink-soft text-sm italic">
                (geen)
              </li>
            </ul>
          </template>
          <template v-else-if="renderValue(k, v).kind === 'object'">
            <pre class="font-mono text-[11px] text-ink-muted bg-paper-tint/50 p-2 whitespace-pre-wrap">{{ JSON.stringify(v, null, 2) }}</pre>
          </template>
          <template v-else-if="renderValue(k, v).kind === 'empty'">
            <span class="text-ink-soft italic">—</span>
          </template>
          <template v-else>
            {{ v }}
          </template>
        </dd>
      </div>
    </dl>
  </article>
</template>
