<script setup>
// Dashboard: list of dossiers the current user can see, plus a
// context-aware CTA. Aanvragers see a prominent "Nieuwe aanvraag"
// button; behandelaars see an overview of what needs their attention.
// The backend filters by the same access-control logic used for the
// detail view, so this just reflects whatever the server returns.

import { onMounted, ref, computed } from "vue";
import { useRouter } from "vue-router";
import { useAuthStore } from "../stores/auth";
import { listDossiers } from "../api";
import StatusPill from "../components/StatusPill.vue";

const auth = useAuthStore();
const router = useRouter();

const dossiers = ref([]);
const loading = ref(true);
const error = ref(null);

const canCreateAanvraag = computed(() => {
  // In this POC, aanvragers are users with an RRN role (individuals)
  // or a KBO role (organisations). Neither Wouter (beheerder) nor
  // Marie (behandelaar) have these. The backend would enforce the
  // same via activity.allowed_roles = ["oe:aanvrager"], we just use
  // this to decide whether to show the CTA prominently.
  const roles = auth.currentUser?.roles || [];
  return roles.some((r) => /^\d{11}$/.test(r) || r.startsWith("kbo-"));
});

async function load() {
  loading.value = true;
  error.value = null;
  try {
    const resp = await listDossiers("toelatingen");
    dossiers.value = resp.dossiers || [];
  } catch (e) {
    error.value = e.message;
  } finally {
    loading.value = false;
  }
}

onMounted(load);

function shortId(id) {
  return id.slice(0, 8);
}
</script>

<template>
  <div class="max-w-[1200px] mx-auto px-8 py-12">
    <!-- Header block: eyebrow + display heading + role note.
         Generous whitespace above to open the page; the lead into
         the list is an editorial title, not a toolbar. -->
    <div class="mb-10 flex items-start justify-between gap-8">
      <div>
        <p class="label-eyebrow mb-2">Werkbeeld</p>
        <h1 class="font-display text-4xl text-ink mb-2">Dossiers</h1>
        <p class="text-ink-muted text-sm max-w-xl">
          Weergave gefilterd op basis van uw rol
          <span class="text-ink font-medium">({{ auth.currentUser.role_summary }})</span>.
          Het platform toont enkel dossiers waarop u toegang hebt.
        </p>
      </div>

      <router-link
        v-if="canCreateAanvraag"
        to="/nieuwe-aanvraag"
        class="btn-brass shrink-0"
      >
        <svg class="w-4 h-4" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5">
          <path d="M8 3v10M3 8h10" />
        </svg>
        Nieuwe aanvraag
      </router-link>
    </div>

    <!-- Loading state. Plain text, no spinners — matches tone. -->
    <div v-if="loading" class="text-ink-soft text-sm py-16 text-center">
      Laden …
    </div>

    <div
      v-else-if="error"
      class="paper-card border-status-rejected/50 px-6 py-4 text-sm"
    >
      <p class="label-eyebrow text-status-rejected mb-1">Kon niet laden</p>
      <p class="text-ink">{{ error }}</p>
      <button class="btn-ghost mt-3" @click="load">Opnieuw proberen</button>
    </div>

    <div v-else-if="dossiers.length === 0" class="paper-card px-8 py-16 text-center">
      <p class="label-eyebrow mb-3">Geen dossiers</p>
      <p class="text-ink-muted text-sm mb-6 max-w-sm mx-auto">
        Er zijn nog geen dossiers die aan uw profiel zijn gekoppeld.
      </p>
      <router-link
        v-if="canCreateAanvraag"
        to="/nieuwe-aanvraag"
        class="btn-primary"
      >
        Eerste aanvraag indienen
      </router-link>
    </div>

    <!-- List. A ruled table with serif identifiers, sans status pills.
         Each row is a link to the detail page. No zebra striping —
         the hairline rules do enough work. -->
    <div v-else class="paper-card overflow-hidden">
      <div
        class="grid grid-cols-[1fr_auto_auto] gap-6 px-6 py-3 border-b border-ink-line label-eyebrow"
      >
        <span>Dossier</span>
        <span>Status</span>
        <span class="sr-only">Actie</span>
      </div>
      <ul>
        <li
          v-for="(d, idx) in dossiers"
          :key="d.id"
          :class="idx > 0 && 'border-t border-ink-line'"
        >
          <router-link
            :to="`/dossiers/${d.id}`"
            class="grid grid-cols-[1fr_auto_auto] gap-6 px-6 py-4 items-center hover:bg-paper-tint transition-colors"
          >
            <div>
              <div class="font-display text-lg text-ink">
                Dossier <span class="font-mono text-base text-ink-muted">{{ shortId(d.id) }}</span>
              </div>
              <div class="text-xs text-ink-soft mt-0.5 font-mono">
                {{ d.workflow }} · {{ d.id }}
              </div>
            </div>
            <StatusPill :status="d.status" />
            <span class="text-ink-soft text-sm">Openen →</span>
          </router-link>
        </li>
      </ul>
    </div>
  </div>
</template>
