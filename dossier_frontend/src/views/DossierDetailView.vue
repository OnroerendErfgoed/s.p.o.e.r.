<script setup>
// Detail view for a single dossier. Shows:
//  - workflow + status banner
//  - allowed-activities panel (contextual: changes with the logged-
//    in user's role)
//  - activity timeline (chronological list of what's happened)
//  - current entities (aanvraag content etc.)
//  - archive-download button (visible to beheerders)
//
// The activity panels are swapped in/out based on which activity
// the user clicks — the same page doubles as an activity-execution
// surface. That's idiomatic for workflow apps: you don't navigate
// to a form, the form takes over part of the current view.

import { onMounted, ref, computed, watch } from "vue";
import { useRouter } from "vue-router";
import { getDossier, downloadArchive } from "../api";
import { useAuthStore } from "../stores/auth";
import StatusPill from "../components/StatusPill.vue";
import ActivityCard from "../components/ActivityCard.vue";
import EntityDisplay from "../components/EntityDisplay.vue";
import ReviseAanvraagForm from "../components/ReviseAanvraagForm.vue";
import NeemBeslissingForm from "../components/NeemBeslissingForm.vue";

const props = defineProps({ id: String });
const router = useRouter();
const auth = useAuthStore();

const dossier = ref(null);
const loading = ref(true);
const error = ref(null);

// activeActivity is the label/type of whichever allowed activity
// the user chose to execute. When non-null, its form is shown.
const activeActivity = ref(null);
const statusMsg = ref(null);

async function load() {
  loading.value = true;
  error.value = null;
  try {
    dossier.value = await getDossier(props.id);
  } catch (e) {
    error.value = e.message;
  } finally {
    loading.value = false;
  }
}

watch(() => props.id, load);
onMounted(load);

// Group activities in oldest-first order for the timeline.
const timelineActivities = computed(() =>
  (dossier.value?.activities ?? []).slice().sort((a, b) =>
    (a.startedAtTime ?? "").localeCompare(b.startedAtTime ?? "")
  )
);

// Drop the tombstone / system-side entities from the main display.
// The user mostly wants to see the substantive content (aanvraag,
// beslissing), not the internal task rows.
const visibleEntities = computed(() => {
  const arr = dossier.value?.currentEntities ?? [];
  return arr.filter(
    (e) =>
      !e.type.startsWith("system:") && e.type !== "oe:dossier_access"
  );
});

const canExport = computed(() => {
  // Beheerder role → full archive export. Others get the button
  // disabled; the backend also enforces via access rules.
  return (auth.currentUser?.roles ?? []).includes("beheerder");
});

// Find activity-definition metadata so we can pick the right form.
function pickForm(actType) {
  // Maps activity type to the form component. The `ReviseAanvraagForm`
  // handles both activities that produce a new `oe:aanvraag` version
  // from an existing one — they share the same data operation and
  // only differ in who's allowed to run them and in which dossier
  // state. `dienAanvraagIn` has its own view because it also creates
  // the dossier row. System activities (e.g.
  // `duidVerantwoordelijkeOrganisatieAan`) aren't client-callable.
  const map = {
    bewerkAanvraag: ReviseAanvraagForm,
    vervolledigAanvraag: ReviseAanvraagForm,
    neemBeslissing: NeemBeslissingForm,
  };
  return map[actType] || null;
}

function selectActivity(act) {
  statusMsg.value = null;
  activeActivity.value = act;
}

function onActivitySucceeded(msg) {
  activeActivity.value = null;
  statusMsg.value = msg ?? "Activiteit uitgevoerd.";
  load();
}

async function onDownloadArchive() {
  try {
    await downloadArchive(props.id);
  } catch (e) {
    statusMsg.value = `Kon archief niet downloaden: ${e.message}`;
  }
}
</script>

<template>
  <div class="max-w-[1200px] mx-auto px-8 py-10">
    <!-- Breadcrumb + back link. Minimal. -->
    <button
      class="text-ink-soft text-sm hover:text-ink mb-6 flex items-center gap-1"
      @click="router.back()"
    >
      ← Terug
    </button>

    <div v-if="loading" class="text-ink-soft text-sm py-16 text-center">
      Laden …
    </div>

    <div v-else-if="error" class="paper-card border-status-rejected/50 px-6 py-5">
      <p class="label-eyebrow text-status-rejected mb-1">Kon niet laden</p>
      <p class="text-ink mb-3">{{ error }}</p>
      <button class="btn-ghost" @click="load">Opnieuw proberen</button>
    </div>

    <div v-else-if="dossier">
      <!-- HEADER: displays workflow + id + status as a big editorial
           line, followed by the archive button for beheerders. -->
      <div class="flex items-start justify-between gap-8 mb-8">
        <div class="min-w-0">
          <p class="label-eyebrow mb-2">{{ dossier.workflow }}</p>
          <h1 class="font-display text-4xl text-ink mb-3">
            Dossier
            <span class="font-mono text-2xl text-ink-muted">
              {{ dossier.id.slice(0, 8) }}
            </span>
          </h1>
          <div class="flex items-center gap-3">
            <StatusPill :status="dossier.status" />
            <span class="text-xs text-ink-soft font-mono">{{ dossier.id }}</span>
          </div>
        </div>

        <div class="flex items-center gap-3 shrink-0">
          <button
            v-if="canExport"
            class="btn-ghost"
            @click="onDownloadArchive"
          >
            <svg class="w-4 h-4" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5">
              <path d="M8 2v9m0 0l-3-3m3 3l3-3M3 13h10" />
            </svg>
            Archief (PDF/A)
          </button>
        </div>
      </div>

      <!-- Status-message banner (post-activity feedback) -->
      <div
        v-if="statusMsg"
        class="paper-card border-brass/50 bg-brass-pale/40 px-5 py-3 mb-8 text-sm flex items-center justify-between gap-4"
      >
        <span class="text-ink">{{ statusMsg }}</span>
        <button
          class="text-ink-soft hover:text-ink text-xs"
          @click="statusMsg = null"
        >✕</button>
      </div>

      <!-- ACTIVE ACTIVITY FORM (takes over the top of the view when open) -->
      <div v-if="activeActivity" class="paper-card p-6 mb-8">
        <div class="flex items-start justify-between mb-5">
          <div>
            <p class="label-eyebrow mb-1">Activiteit uitvoeren</p>
            <h2 class="font-display text-2xl text-ink">
              {{ activeActivity.label }}
            </h2>
          </div>
          <button
            class="text-ink-soft hover:text-ink text-sm"
            @click="activeActivity = null"
          >
            Annuleren
          </button>
        </div>

        <component
          v-if="pickForm(activeActivity.type)"
          :is="pickForm(activeActivity.type)"
          :dossier="dossier"
          :activity-type="activeActivity.type"
          @success="onActivitySucceeded"
          @error="(e) => (statusMsg = e)"
        />
        <p v-else class="text-sm text-ink-muted">
          Deze activiteit heeft nog geen UI in deze demo.
          Gebruik de API rechtstreeks.
        </p>
      </div>

      <!-- MAIN TWO-COLUMN LAYOUT -->
      <div class="grid grid-cols-1 lg:grid-cols-[1fr_320px] gap-10">
        <!-- Left column: entities + timeline -->
        <div class="space-y-10">
          <!-- Current entities (aanvraag content, beslissing, ...) -->
          <section v-if="visibleEntities.length">
            <h2 class="font-display text-2xl text-ink mb-1">Inhoud</h2>
            <p class="text-xs text-ink-soft mb-5">
              Huidige entiteiten van dit dossier.
            </p>
            <div class="space-y-5">
              <EntityDisplay
                v-for="e in visibleEntities"
                :key="`${e.type}/${e.entityId}@${e.versionId}`"
                :entity="e"
              />
            </div>
          </section>

          <!-- Activity timeline -->
          <section>
            <h2 class="font-display text-2xl text-ink mb-1">Tijdlijn</h2>
            <p class="text-xs text-ink-soft mb-5">
              Chronologisch overzicht van alle gebeurtenissen op dit dossier.
            </p>
            <ol class="relative border-l border-ink-line pl-6 ml-2 space-y-5">
              <ActivityCard
                v-for="a in timelineActivities"
                :key="a.id"
                :activity="a"
              />
            </ol>
          </section>
        </div>

        <!-- Right column: allowed-activities panel -->
        <aside class="space-y-6">
          <div class="paper-card">
            <div class="px-5 py-3 border-b border-ink-line">
              <p class="label-eyebrow">Volgende stap</p>
            </div>
            <div class="p-5">
              <p
                v-if="!dossier.allowedActivities.length"
                class="text-sm text-ink-muted"
              >
                Er zijn momenteel geen acties beschikbaar voor u op dit dossier.
              </p>
              <ul v-else class="space-y-2">
                <li
                  v-for="act in dossier.allowedActivities"
                  :key="act.type"
                >
                  <button
                    class="w-full text-left p-3 border border-ink-line hover:border-brass hover:bg-paper-tint transition-colors group"
                    @click="selectActivity(act)"
                  >
                    <div class="flex items-center justify-between gap-2">
                      <span class="text-ink group-hover:text-brass-dark transition-colors text-sm font-medium">
                        {{ act.label }}
                      </span>
                      <span class="text-ink-soft group-hover:text-brass">→</span>
                    </div>
                    <span class="text-[11px] text-ink-soft font-mono">{{ act.type }}</span>
                  </button>
                </li>
              </ul>
            </div>
          </div>

          <!-- Role banner: remind the user what hat they're wearing. -->
          <div class="border border-ink-line bg-paper-tint/50 px-4 py-3">
            <p class="label-eyebrow mb-1">Actieve rol</p>
            <p class="text-sm text-ink">{{ auth.currentUser.name }}</p>
            <p class="text-xs text-ink-soft">{{ auth.currentUser.role_summary }}</p>
          </div>
        </aside>
      </div>
    </div>
  </div>
</template>
