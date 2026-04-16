<script setup>
// NeemBeslissingForm
//
// Runs the `neemBeslissing` activity. This generates TWO entities
// in one atomic transaction:
//   - oe:beslissing with outcome (goedgekeurd/afgekeurd/onvolledig),
//     datum, object URI, and `brief` (a file id for the decision
//     letter PDF)
//   - oe:handtekening with `getekend: true`
// plus a `oe:neemtAkteVan` relation.
//
// NOTE on `brief`: the backend's FileId type is just a tagged string,
// so a fresh UUID passes validation. In this demo we generate a
// placeholder file id without actually uploading anything — so the
// `brief_download_url` the server injects in GET responses will 404
// at the file_service. A proper implementation would upload a PDF
// first and use that file id; see `routes/files.py`.

import { ref, computed } from "vue";
import { executeActivity, uuid4 } from "../api";

const props = defineProps({
  dossier: { type: Object, required: true },
  activityType: { type: String, required: true },
});
const emit = defineEmits(["success", "error"]);

const submitting = ref(false);
const error = ref(null);

const form = ref({
  beslissing: "goedgekeurd",
  datum: new Date().toISOString().slice(0, 10), // yyyy-mm-dd
});

// The aanvraag entity is used as input. auto_resolve would let us
// skip this, but supplying it explicitly makes the PROV record
// clearer.
const currentAanvraag = computed(() =>
  props.dossier.currentEntities.find((e) => e.type === "oe:aanvraag")
);

const objectUri = computed(() => currentAanvraag.value?.content?.object);

async function submit() {
  if (!currentAanvraag.value) {
    error.value = "Geen aanvraag om een beslissing op te nemen.";
    return;
  }

  submitting.value = true;
  error.value = null;

  const a = currentAanvraag.value;
  const aanvraagRef = `oe:aanvraag/${a.entityId}@${a.versionId}`;

  // Generate fresh IDs for activity + beslissing + handtekening.
  const activityId = uuid4();
  const beslissingEntityId = uuid4();
  const beslissingVersionId = uuid4();
  const handtekeningEntityId = uuid4();
  const handtekeningVersionId = uuid4();
  const briefFileId = uuid4(); // placeholder — see NOTE above

  const beslissingRef =
    `oe:beslissing/${beslissingEntityId}@${beslissingVersionId}`;
  const handtekeningRef =
    `oe:handtekening/${handtekeningEntityId}@${handtekeningVersionId}`;

  // Timestamps on beslissing must be ISO-8601 — convert the date to
  // midnight UTC.
  const datumIso = new Date(form.value.datum).toISOString();

  const body = {
    used: [{ entity: aanvraagRef }],
    generated: [
      {
        entity: beslissingRef,
        content: {
          beslissing: form.value.beslissing,
          datum: datumIso,
          object: objectUri.value,
          brief: briefFileId,
        },
      },
      {
        entity: handtekeningRef,
        content: { getekend: true },
      },
    ],
    // `oe:neemtAkteVan` relations only apply when the activity `used`
    // an older version of the aanvraag and needs to acknowledge that
    // a newer version exists (stale-used-reference workflow). Since
    // we always use the current latest aanvraag, no acks are needed.
  };

  try {
    await executeActivity(
      props.dossier.id,
      activityId,
      props.activityType,
      body,
    );
    emit("success", `Beslissing genomen: ${form.value.beslissing}.`);
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
      Neem een beslissing op de aanvraag. Dit genereert een
      <code class="text-xs">oe:beslissing</code> en
      <code class="text-xs">oe:handtekening</code> entiteit in één
      atomaire transactie.
    </p>

    <!-- Context summary: which aanvraag this is about. -->
    <div
      v-if="currentAanvraag"
      class="border border-ink-line bg-paper-tint/40 px-4 py-3 space-y-1"
    >
      <p class="label-eyebrow">Betreft aanvraag</p>
      <p class="text-sm text-ink font-medium">
        {{ currentAanvraag.content?.onderwerp ?? "(geen onderwerp)" }}
      </p>
      <p class="text-[11px] font-mono text-ink-soft">
        {{ currentAanvraag.content?.handeling }}
        ·
        {{ currentAanvraag.content?.gemeente }}
      </p>
    </div>

    <!-- Outcome choice as three big radio cards so the semantic
         weight of each option is obvious. -->
    <fieldset>
      <legend class="label-eyebrow mb-2 block">Uitkomst</legend>
      <div class="grid grid-cols-1 sm:grid-cols-3 gap-3">
        <label
          v-for="opt in [
            { value: 'goedgekeurd', label: 'Goedgekeurd', colour: 'approved', hint: 'De toelating wordt verleend.' },
            { value: 'afgekeurd', label: 'Afgekeurd', colour: 'rejected', hint: 'De toelating wordt geweigerd.' },
            { value: 'onvolledig', label: 'Onvolledig', colour: 'review', hint: 'De aanvraag moet aangevuld worden.' },
          ]"
          :key="opt.value"
          class="border cursor-pointer px-4 py-3 transition-colors select-none"
          :class="form.beslissing === opt.value
            ? 'border-brass bg-brass-pale/60'
            : 'border-ink-line hover:bg-paper-tint'"
        >
          <input
            v-model="form.beslissing"
            type="radio"
            :value="opt.value"
            class="sr-only"
          />
          <div class="flex items-center gap-2 mb-1">
            <span
              class="pill"
              :class="`bg-status-${opt.colour}`"
            >{{ opt.label }}</span>
          </div>
          <p class="text-[11px] text-ink-soft leading-snug">{{ opt.hint }}</p>
        </label>
      </div>
    </fieldset>

    <div>
      <label class="label-eyebrow block mb-1.5">Datum</label>
      <input v-model="form.datum" type="date" class="w-auto" />
    </div>

    <!-- Note about the placeholder brief. Makes the demo's limitation
         visible rather than silently producing a broken PDF link. -->
    <div class="border border-dashed border-ink-line px-4 py-3 bg-paper-tint/40">
      <p class="label-eyebrow text-ink-soft mb-0.5">Beslissingsbrief (PDF)</p>
      <p class="text-xs text-ink-soft">
        In deze demo wordt een placeholder <code class="text-[11px]">file_id</code>
        gegenereerd; er is geen echte brief opgeladen. De download-URL
        in het dossier zal dus 404 geven.
      </p>
    </div>

    <div
      v-if="error"
      class="border border-status-rejected/60 bg-paper-card px-4 py-2 text-sm text-ink"
    >{{ error }}</div>

    <div class="flex justify-end gap-3 pt-3 border-t border-ink-line">
      <button type="submit" class="btn-primary" :disabled="submitting">
        {{ submitting ? "Bezig …" : "Beslissing registreren" }}
      </button>
    </div>
  </form>
</template>
