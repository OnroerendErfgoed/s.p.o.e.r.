<script setup>
// Small filled pill rendering a dossier status. Colours are drawn
// from the "status" palette in tailwind.config.js. Each toelatingen
// status maps to one of the generic buckets (draft/submitted/
// review/approved/rejected) so the palette stays small and calm.

import { computed } from "vue";

const props = defineProps({
  status: { type: String, default: null },
});

// Map toelatingen-specific status names onto colour buckets + Dutch
// display labels. When we see an unknown status, fall through to a
// neutral grey draft tone.
const STATUS_MAP = {
  ingediend: { colour: "submitted", label: "Ingediend" },
  klaar_voor_behandeling: { colour: "review", label: "Behandeling" },
  in_behandeling: { colour: "review", label: "In behandeling" },
  aanvraag_onvolledig: { colour: "review", label: "Onvolledig" },
  beslissing_te_tekenen: { colour: "review", label: "Te tekenen" },
  beslissing_ondertekend: { colour: "review", label: "Ondertekend" },
  aanvraag_ingetrokken: { colour: "rejected", label: "Ingetrokken" },
  toelating_verleend: { colour: "approved", label: "Verleend" },
  toelating_geweigerd: { colour: "rejected", label: "Geweigerd" },
  afgesloten: { colour: "approved", label: "Afgesloten" },
  verwijderd: { colour: "rejected", label: "Verwijderd" },
};

const meta = computed(() => {
  if (!props.status) return { colour: "draft", label: "—" };
  return STATUS_MAP[props.status] ?? { colour: "draft", label: props.status };
});

const style = computed(() => {
  const colourMap = {
    draft: "bg-status-draft",
    submitted: "bg-status-submitted",
    review: "bg-status-review",
    approved: "bg-status-approved",
    rejected: "bg-status-rejected",
  };
  return colourMap[meta.value.colour];
});
</script>

<template>
  <span class="pill" :class="style">{{ meta.label }}</span>
</template>
