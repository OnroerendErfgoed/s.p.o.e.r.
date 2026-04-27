<script setup>
// ReviseAanvraagForm
//
// Shared form for every activity that revises the `oe:aanvraag`
// entity in place:
//
//   - `bewerkAanvraag`    — a behandelaar corrects or reshapes the
//                           aanvraag (status: klaar_voor_behandeling)
//   - `vervolledigAanvraag` — the aanvrager completes an incomplete
//                           submission after an "onvolledig" beslissing
//                           (status: aanvraag_onvolledig)
//
// Both activities have identical data semantics: generate a new
// `oe:aanvraag` version with `derivedFrom` pointing at the current
// one. They differ only in authorisation and required status. The
// backend enforces both. Here we only vary the intro copy and the
// success message depending on which activity the parent selected.

import { ref, computed, onMounted } from "vue";
import { executeActivity, uuid4 } from "../api";

const props = defineProps({
  dossier: { type: Object, required: true },
  activityType: { type: String, required: true },
});
const emit = defineEmits(["success", "error"]);

const submitting = ref(false);
const error = ref(null);

// Copy that depends on which activity we're driving. Kept as a
// lookup table rather than if/else so the symmetry is obvious.
// Keys are qualified activity types as emitted by the engine.
const COPY = {
  "oe:bewerkAanvraag": {
    intro: "Bewerk de aanvraag. Een nieuwe versie wordt gegenereerd met een verwijzing (derivedFrom) naar de huidige versie.",
    submitButton: "Opslaan",
    submittingButton: "Bezig …",
    successMsg: "Aanvraag bewerkt.",
  },
  "oe:vervolledigAanvraag": {
    intro: "Vervolledig je aanvraag met de ontbrekende informatie. Na indiening wordt de aanvraag opnieuw voorgelegd aan de behandelaar.",
    submitButton: "Opnieuw indienen",
    submittingButton: "Bezig …",
    successMsg: "Aanvraag vervolledigd en opnieuw ingediend.",
  },
};

const copy = computed(() => COPY[props.activityType] ?? COPY["oe:bewerkAanvraag"]);

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

  const e = currentAanvraag.value;
  const previousRef = `oe:aanvraag/${e.entityId}@${e.versionId}`;
  const newRef = `oe:aanvraag/${e.entityId}@${newVersionId}`;

  // Preserve the aanvrager identity from the existing aanvraag.
  // Both activities leave it unchanged: the behandelaar isn't
  // impersonating the aanvrager, and the aanvrager isn't switching
  // identity mid-dossier.
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
          // Preserve bijlagen exactly — this form doesn't edit them.
          bijlagen: e.content?.bijlagen ?? [],
        },
      },
    ],
  };

  try {
    await executeActivity(
      props.dossier.workflow,
      props.dossier.id,
      activityId,
      props.activityType,
      body,
    );
    emit("success", copy.value.successMsg);
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
    <p class="text-sm text-ink-muted">{{ copy.intro }}</p>

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
        {{ submitting ? copy.submittingButton : copy.submitButton }}
      </button>
    </div>
  </form>
</template>
