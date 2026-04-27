<script setup>
// New aanvraag form. Triggers the `dienAanvraagIn` activity, which:
//   - creates a new dossier row (can_create_dossier: true)
//   - generates an oe:aanvraag entity with the content the user fills
//   - kicks off side-effect activities (assigns verantwoordelijke
//     organisatie based on the gemeente, sets system fields)
//   - schedules tasks (send ontvangstbevestiging etc.)
//
// The aanvrager identity comes from the logged-in user:
//   - Jan (RRN role "85010100123") → aanvrager.rrn set from his RRN
//   - ACME (KBO role starting "kbo-toevoeger:") → aanvrager.kbo set from the KBO
// Beheerders and behandelaars can't land on this page (we redirect
// them back) because allowed_roles: ["oe:aanvrager"] in the YAML.

import { ref, computed, onMounted } from "vue";
import { useRouter } from "vue-router";
import { executeActivity, uuid4 } from "../api";
import { useAuthStore } from "../stores/auth";

const router = useRouter();
const auth = useAuthStore();

// Simple gemeente picker — the backend also accepts any string,
// but constraining to a small list here makes the demo nicer and
// lets us match the behandelaar's gemeente for Marie (Brugge).
const GEMEENTEN = [
  "Brugge",
  "Gent",
  "Antwerpen",
  "Leuven",
  "Hasselt",
  "Mechelen",
];
const HANDELINGEN = [
  { value: "renovatie", label: "Renovatie" },
  { value: "restauratie", label: "Restauratie" },
  { value: "herbestemming", label: "Herbestemming" },
  { value: "verbouwing", label: "Verbouwing" },
  { value: "sloop_deel", label: "Gedeeltelijke afbraak" },
];

// Pull the aanvrager identifier (RRN or KBO) from the logged-in
// user's roles. POC users have exactly one of these.
const aanvragerIdentity = computed(() => {
  const roles = auth.currentUser?.roles ?? [];
  const rrn = roles.find((r) => /^\d{11}$/.test(r));
  if (rrn) return { rrn };
  const kbo = roles.find((r) => r.startsWith("kbo-toevoeger:"));
  if (kbo) return { kbo: kbo.split(":")[1] };
  return null;
});

onMounted(() => {
  // If the current user isn't an aanvrager, kick them out —
  // they can't submit this anyway.
  if (!aanvragerIdentity.value) {
    router.replace("/");
  }
});

const form = ref({
  onderwerp: "",
  handeling: "renovatie",
  gemeente: "Brugge",
  objectUri: "",
});

const submitting = ref(false);
const error = ref(null);

async function submit() {
  if (submitting.value) return;

  // Basic client-side validation. The backend does the authoritative
  // validation; these checks are only to catch obvious mistakes early.
  if (!form.value.onderwerp.trim()) {
    error.value = "Onderwerp is verplicht.";
    return;
  }
  if (!form.value.objectUri.trim()) {
    error.value = "URI van het beschermd erfgoedobject is verplicht.";
    return;
  }
  if (!form.value.gemeente) {
    error.value = "Gemeente is verplicht.";
    return;
  }

  submitting.value = true;
  error.value = null;

  // Generate the IDs client-side. The backend expects the caller to
  // supply these (idempotency key + explicit entity/version).
  const dossierId = uuid4();
  const activityId = uuid4();
  const entityId = uuid4();
  const versionId = uuid4();

  const body = {
    workflow: "toelatingen",
    used: [{ entity: form.value.objectUri.trim() }],
    generated: [
      {
        entity: `oe:aanvraag/${entityId}@${versionId}`,
        content: {
          onderwerp: form.value.onderwerp.trim(),
          handeling: form.value.handeling,
          aanvrager: aanvragerIdentity.value,
          gemeente: form.value.gemeente,
          object: form.value.objectUri.trim(),
          bijlagen: [],
        },
      },
    ],
  };

  try {
    // dienAanvraagIn is the kick-off activity for a brand-new
    // toelatingen dossier — workflow is known at this point because
    // this view only handles toelatingen applications. Activity type
    // is qualified with the plugin prefix (oe:…) to match the
    // engine's route scheme.
    await executeActivity(
      "toelatingen", dossierId, activityId, "oe:dienAanvraagIn", body,
    );
    // On success, jump to the detail view for the new dossier.
    router.push(`/dossiers/${dossierId}`);
  } catch (e) {
    error.value = e.message;
    submitting.value = false;
  }
}
</script>

<template>
  <div class="max-w-[820px] mx-auto px-8 py-12">
    <button
      class="text-ink-soft text-sm hover:text-ink mb-6"
      @click="router.back()"
    >← Terug</button>

    <div class="mb-10">
      <p class="label-eyebrow mb-2">Dien aanvraag in</p>
      <h1 class="font-display text-4xl text-ink mb-3">Nieuwe aanvraag</h1>
      <p class="text-ink-muted text-sm max-w-xl">
        Dien een toelatingsaanvraag in voor een handeling op beschermd
        erfgoed. De aanvraag wordt automatisch toegewezen aan de
        verantwoordelijke organisatie van de gekozen gemeente.
      </p>
    </div>

    <!-- Form card -->
    <form class="paper-card p-7 space-y-6" @submit.prevent="submit">
      <!-- Aanvrager (read-only, derived from login) -->
      <div>
        <label class="label-eyebrow block mb-1.5">Aanvrager</label>
        <div
          class="border border-ink-line bg-paper-tint/40 px-3 py-2 text-sm flex items-center justify-between"
        >
          <span class="text-ink">{{ auth.currentUser.name }}</span>
          <span class="text-[11px] font-mono text-ink-soft">
            {{ aanvragerIdentity?.rrn ? `RRN · ${aanvragerIdentity.rrn}` : `KBO · ${aanvragerIdentity?.kbo}` }}
          </span>
        </div>
      </div>

      <div>
        <label for="onderwerp" class="label-eyebrow block mb-1.5">Onderwerp</label>
        <input
          id="onderwerp"
          v-model="form.onderwerp"
          type="text"
          placeholder="Bv. Restauratie gevelbekleding stadhuis"
          autofocus
        />
      </div>

      <div class="grid grid-cols-1 sm:grid-cols-2 gap-5">
        <div>
          <label for="handeling" class="label-eyebrow block mb-1.5">Handeling</label>
          <select id="handeling" v-model="form.handeling">
            <option v-for="h in HANDELINGEN" :key="h.value" :value="h.value">
              {{ h.label }}
            </option>
          </select>
        </div>
        <div>
          <label for="gemeente" class="label-eyebrow block mb-1.5">Gemeente</label>
          <select id="gemeente" v-model="form.gemeente">
            <option v-for="g in GEMEENTEN" :key="g" :value="g">{{ g }}</option>
          </select>
          <p class="text-[11px] text-ink-soft mt-1">
            Voor de demo: kies Brugge zodat Marie (behandelaar)
            het dossier in behandeling kan nemen.
          </p>
        </div>
      </div>

      <div>
        <label for="object" class="label-eyebrow block mb-1.5">
          URI beschermd erfgoedobject
        </label>
        <input
          id="object"
          v-model="form.objectUri"
          type="url"
          placeholder="https://id.erfgoed.net/erfgoedobjecten/10001"
        />
        <p class="text-[11px] text-ink-soft mt-1">
          Referentie naar het object waarop de handeling wordt uitgevoerd.
        </p>
      </div>

      <!-- Bijlagen: noted as out of scope for this demo. -->
      <div class="border border-dashed border-ink-line px-4 py-3 bg-paper-tint/40">
        <p class="label-eyebrow text-ink-soft mb-0.5">Bijlagen</p>
        <p class="text-xs text-ink-soft">
          Bestandsupload is in deze front-end demo niet geïmplementeerd.
          Gebruik de API rechtstreeks om bijlagen toe te voegen.
        </p>
      </div>

      <div v-if="error" class="border border-status-rejected/60 bg-paper-card px-4 py-3">
        <p class="label-eyebrow text-status-rejected mb-0.5">Fout</p>
        <p class="text-sm text-ink">{{ error }}</p>
      </div>

      <div class="flex items-center justify-end gap-3 pt-2 border-t border-ink-line">
        <button
          type="button"
          class="btn-ghost"
          :disabled="submitting"
          @click="router.push('/')"
        >Annuleren</button>
        <button
          type="submit"
          class="btn-primary"
          :disabled="submitting"
        >
          {{ submitting ? "Bezig …" : "Aanvraag indienen" }}
        </button>
      </div>
    </form>
  </div>
</template>
