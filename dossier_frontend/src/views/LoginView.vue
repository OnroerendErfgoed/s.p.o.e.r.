<script setup>
// POC "login" — pick a user, set it in the auth store, go home.
// Not real authentication; the backend uses X-POC-User for this.
// Shown as a quick card-based chooser so the demo's role-based
// access differences are self-evident on first load.
import { useRouter } from "vue-router";
import { useAuthStore } from "../stores/auth";

const auth = useAuthStore();
const router = useRouter();

function pick(user) {
  auth.setUser(user);
  router.push("/");
}
</script>

<template>
  <div class="min-h-screen flex items-center justify-center px-6 py-12">
    <div class="w-full max-w-2xl">
      <div class="text-center mb-12">
        <p class="label-eyebrow mb-3">Onroerend Erfgoed</p>
        <h1 class="font-display text-5xl text-ink leading-none mb-3">
          Toelatingen
        </h1>
        <p class="text-ink-muted text-sm max-w-md mx-auto">
          Platform voor het beheer van toelatingsaanvragen op beschermd
          erfgoed. Demonstratie-opstelling — kies een profiel om verder te gaan.
        </p>
      </div>

      <div class="paper-card">
        <div class="px-6 py-4 border-b border-ink-line">
          <p class="label-eyebrow">Demo-profielen</p>
        </div>
        <ul>
          <li
            v-for="(u, idx) in auth.availableUsers"
            :key="u.username"
            :class="idx > 0 && 'border-t border-ink-line'"
          >
            <button
              class="group w-full flex items-start gap-4 px-6 py-5 text-left hover:bg-paper-tint transition-colors"
              @click="pick(u)"
            >
              <span
                class="w-10 h-10 flex items-center justify-center bg-brass-pale text-brass-dark font-display text-base border border-brass/30 shrink-0 mt-0.5"
              >
                {{ u.name.charAt(0) }}
              </span>
              <div class="flex-1 min-w-0">
                <div class="flex items-baseline gap-3 mb-0.5">
                  <span class="text-ink font-medium">{{ u.name }}</span>
                  <span class="label-eyebrow">{{ u.role_summary }}</span>
                </div>
                <p class="text-sm text-ink-muted">{{ u.description }}</p>
              </div>
              <span
                class="text-ink-soft group-hover:text-ink transition-colors self-center"
              >→</span>
            </button>
          </li>
        </ul>
      </div>

      <p class="text-center text-[11px] text-ink-soft mt-8 font-mono">
        POC · geen wachtwoord vereist · sessie blijft in localStorage
      </p>
    </div>
  </div>
</template>
