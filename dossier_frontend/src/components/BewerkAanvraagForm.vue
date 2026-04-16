<script setup>
// BewerkAanvraagForm
//
// Runs the `bewerkAanvraag` activity. The behandelaar edits the
// current aanvraag and we produce a new oe:aanvraag entity version
// that derives from the current one. The engine handles the
// derivation chain; we just have to supply `derivedFrom` pointing
// at the current version.
//
// Content shape is the same as the initial aanvraag (onderwerp /
// handeling / gemeente / object + aanvrager). We pre-fill from the
// current aanvraag so the behandelaar only changes what they need.

import { ref, computed, onMounted } from "vue";
import { executeActivity, uuid4 } from "../api";

const props = defineProps({
  dossier: { type: Object, required: true },
  activityType: { type: String, required: true },
});
const emit = defineEmits(["success", "error"]);

const submitting = ref(false);
const error = ref(null);

// Find the current aanvraag entity. The backend guarantees there
// is at most one per dossier (it's declared `singleton: true`).
const currentAanvraag = computed(() =>
  props.dossier.currentEntities.find((e) => e.type === "oe:aanvraag")
);

const form = ref({
  onderwerp: "",
  handeling: "renovatie",
  gemeente: "",
  objectUri: "",
});

onMounted(() => {
  if (currentAanvraag.value) {
    const c = currentAanvraag.value.content ?? {};
    form.value.onderwerp = c.onderwerp ?? "";
    form.value.handeling = c.handeling ?? "renovatie";
    form.value.gemeente = c.gemeente ?? "";
    form.value.objectUri = c.object ?? "";
  }
});

async function submit() {
  if (!currentAanvraag.value) {
    error.value = "Geen aanvraag gevonden om te bewerken.";
    return;
  }
  if (!form.value.onderwerp.trim()) {
    error.value = "Onderwerp is verplicht.";
    return;
  }
  submitting.value = true;
  error.value = null;

  const activityId = uuid4();
  const newVersionId = uuid4();

  // Build entity refs in the canonical form.
  const e = currentAanvraag.value;
  const previousRef = `oe:aanvraag/${e.entityId}@${e.versionId}`;
  const newRef = `oe:aanvraag/${e.entityId}@${newVersionId}`;

  // Preserve the aanvrager identity from the existing aanvraag —
  // the behandelaar isn't changing who the applicant is.
  const aanvrager = (e.content?.aanvrager) ?? {};

  const body = {
    used: [{ entity: form.value.objectUri.trim() }],
    generated: [
      {
        entity: newRef,
        derivedFrom: previousRef,
        content: {
          onderwerp: form.value.onderwerp.trim(),
          handeling: form.value.handeling,
          aanvrager,
          gemeente: form.value.gemeente.trim(),
          object: form.value.objectUri.trim(),
          // Preserve bijlagen exactly — behandelaars don't edit these
          // in this form.
          bijlagen: e.content?.bijlagen ?? [],
        },
      },
    ],
  };

  try {
    await executeActivity(
      props.dossier.id,
      activityId,
      props.activityType,
      body,
    );
    emit("success", "Aanvraag bewerkt.");
  } catch (err) {
    error.value = err.message;
    emit("error", err.message);
  } finally {
    submitting.value = false;
  }
}
</script>

<template>
  <form class="space-y-5" @submit.prevent="submit">
    <p class="text-sm text-ink-muted">
      Bewerk de aanvraag. Een nieuwe versie wordt gegenereerd met
      een verwijzing (<code>derivedFrom</code>) naar de huidige versie.
    </p>

    <div>
      <label class="label-eyebrow block mb-1.5">Onderwerp</label>
      <input v-model="form.onderwerp" type="text" />
    </div>

    <div class="grid grid-cols-1 sm:grid-cols-2 gap-5">
      <div>
        <label class="label-eyebrow block mb-1.5">Handeling</label>
        <select v-model="form.handeling">
          <option value="renovatie">Renovatie</option>
          <option value="restauratie">Restauratie</option>
          <option value="herbestemming">Herbestemming</option>
          <option value="verbouwing">Verbouwing</option>
          <option value="sloop_deel">Gedeeltelijke afbraak</option>
          <option value="plaatsing">Plaatsing</option>
        </select>
      </div>
      <div>
        <label class="label-eyebrow block mb-1.5">Gemeente</label>
        <input v-model="form.gemeente" type="text" />
      </div>
    </div>

    <div>
      <label class="label-eyebrow block mb-1.5">URI erfgoedobject</label>
      <input v-model="form.objectUri" type="url" />
    </div>

    <div
      v-if="error"
      class="border border-status-rejected/60 bg-paper-card px-4 py-2 text-sm text-ink"
    >{{ error }}</div>

    <div class="flex justify-end gap-3 pt-3 border-t border-ink-line">
      <button type="submit" class="btn-primary" :disabled="submitting">
        {{ submitting ? "Bezig …" : "Opslaan" }}
      </button>
    </div>
  </form>
</template>
