<script setup>
// Top-right widget showing who's currently "logged in" and letting
// them switch. Click opens a dropdown with the other POC users —
// handy for demoing access control without going back to /login.
import { ref, onBeforeUnmount } from "vue";
import { useRouter } from "vue-router";
import { useAuthStore } from "../stores/auth";

const auth = useAuthStore();
const router = useRouter();
const open = ref(false);

function close() {
  open.value = false;
}
function switchTo(user) {
  auth.setUser(user);
  close();
  router.push("/");
}
function logout() {
  auth.setUser(null);
  close();
  router.push("/login");
}

// Click-outside: close when clicking anywhere else.
function onDocClick(e) {
  if (!e.target.closest("[data-user-menu]")) close();
}
document.addEventListener("click", onDocClick);
onBeforeUnmount(() => document.removeEventListener("click", onDocClick));
</script>

<template>
  <div class="relative" data-user-menu>
    <button
      class="flex items-center gap-3 py-1 px-2 -mx-2 hover:bg-paper-tint/60 transition-colors"
      @click="open = !open"
    >
      <!-- Monogram disc: first letter on brass-pale. Adds texture. -->
      <span
        class="w-8 h-8 flex items-center justify-center bg-brass-pale text-brass-dark font-display text-sm border border-brass/30"
      >
        {{ auth.currentUser.name.charAt(0) }}
      </span>
      <span class="text-left">
        <span class="block text-sm font-medium text-ink">
          {{ auth.currentUser.name }}
        </span>
        <span class="block text-[11px] text-ink-soft -mt-0.5">
          {{ auth.currentUser.role_summary }}
        </span>
      </span>
      <svg
        class="w-3 h-3 text-ink-soft"
        :class="{ 'rotate-180': open }"
        viewBox="0 0 12 12"
        fill="none"
        stroke="currentColor"
        stroke-width="1.5"
      >
        <path d="M2 4.5 L6 8.5 L10 4.5" />
      </svg>
    </button>

    <transition name="drop">
      <div
        v-if="open"
        class="absolute right-0 mt-1 w-80 paper-card z-30"
      >
        <div class="px-4 py-3 border-b border-ink-line">
          <p class="label-eyebrow">Wissel naar</p>
        </div>
        <ul class="py-1">
          <li
            v-for="u in auth.availableUsers"
            :key="u.username"
          >
            <button
              class="w-full text-left px-4 py-2.5 hover:bg-paper-tint transition-colors"
              :class="{ 'bg-brass-pale/40': u.username === auth.currentUser.username }"
              @click="switchTo(u)"
            >
              <div class="text-sm text-ink">{{ u.name }}</div>
              <div class="text-[11px] text-ink-soft">{{ u.role_summary }}</div>
            </button>
          </li>
        </ul>
        <div class="border-t border-ink-line px-4 py-2.5">
          <button
            class="text-xs text-ink-soft hover:text-ink"
            @click="logout"
          >
            Uitloggen
          </button>
        </div>
      </div>
    </transition>
  </div>
</template>

<style scoped>
.drop-enter-active,
.drop-leave-active {
  transition: all 140ms ease;
}
.drop-enter-from,
.drop-leave-to {
  opacity: 0;
  transform: translateY(-4px);
}
</style>
