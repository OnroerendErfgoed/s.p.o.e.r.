<script setup>
// Timeline entry for one activity. Designed for a vertical timeline
// rendered by the parent as a <ol> with border-l. Each card draws
// a small disc dot on the border and spans the rest of the row.

import { computed } from "vue";

const props = defineProps({
  activity: { type: Object, required: true },
});

const when = computed(() => {
  if (!props.activity.startedAtTime) return "—";
  const d = new Date(props.activity.startedAtTime);
  if (isNaN(d)) return props.activity.startedAtTime;
  return d.toLocaleString("nl-BE", {
    dateStyle: "medium",
    timeStyle: "short",
  });
});

// Friendly display for the activity type. Strip the systemAction
// prefix and un-camelCase the rest for reading.
const label = computed(() => {
  const t = props.activity.type;
  if (!t) return "—";
  // Break camel case to Title Case words: "bewerkAanvraag" → "Bewerk Aanvraag"
  return t
    .replace(/([A-Z])/g, " $1")
    .replace(/^./, (c) => c.toUpperCase())
    .trim();
});

// System activities (e.g. the `systemAction` for tasks) get a different
// tone so they fade into the background vs. user-visible activities.
const isSystem = computed(() =>
  props.activity.type === "systemAction" ||
  props.activity.type?.startsWith("setSystem") ||
  props.activity.type === "duidVerantwoordelijkeOrganisatieAan"
);
</script>

<template>
  <li class="relative" :class="isSystem && 'opacity-60'">
    <!-- The dot on the timeline border. Offset -32px left (6px padding
         + 26px to reach the border at 24px padding). -->
    <span
      class="absolute -left-[31px] top-[7px] w-[11px] h-[11px] border rounded-full bg-paper-card"
      :class="isSystem ? 'border-ink-soft' : 'border-brass-dark'"
    ></span>

    <div class="flex items-baseline justify-between gap-4 mb-0.5">
      <span class="text-ink font-medium">{{ label }}</span>
      <span class="text-[11px] text-ink-soft font-mono shrink-0">{{ when }}</span>
    </div>
    <div class="text-[11px] text-ink-soft font-mono">
      {{ activity.type }}
      <template v-if="activity.informedBy">
        · informedBy
        <span class="text-ink-soft">{{ activity.informedBy.slice(0, 12) }}…</span>
      </template>
    </div>
  </li>
</template>
