<script setup>
// Root layout. Top bar with the wordmark + current-user badge.
// Content renders into <router-view />.
import { computed } from "vue";
import { useRoute } from "vue-router";
import { useAuthStore } from "./stores/auth";
import UserBadge from "./components/UserBadge.vue";

const auth = useAuthStore();
const route = useRoute();
const isLogin = computed(() => route.path === "/login");
</script>

<template>
  <div class="min-h-screen flex flex-col">
    <!-- Top strip: a single hairline-bordered band with wordmark
         on the left and the current user on the right. Deliberate
         low-chrome — the content area does the heavy lifting. -->
    <header
      v-if="!isLogin"
      class="border-b border-ink-line bg-paper-card/80 backdrop-blur-sm sticky top-0 z-20"
    >
      <div class="max-w-[1200px] mx-auto px-8 h-16 flex items-center justify-between">
        <router-link
          to="/"
          class="group flex items-baseline gap-3 no-underline"
        >
          <span class="font-display text-xl text-ink">Toelatingen</span>
          <span class="label-eyebrow border-l border-ink-line pl-3">
            Onroerend Erfgoed
          </span>
        </router-link>

        <UserBadge v-if="auth.isLoggedIn" />
      </div>
    </header>

    <main class="flex-1">
      <router-view v-slot="{ Component }">
        <transition name="fade" mode="out-in">
          <component :is="Component" />
        </transition>
      </router-view>
    </main>

    <!-- Slim footer with the stack credits. Matches the
         institutional-document feel. -->
    <footer
      v-if="!isLogin"
      class="border-t border-ink-line mt-16"
    >
      <div class="max-w-[1200px] mx-auto px-8 py-6 text-xs text-ink-soft flex justify-between">
        <span>Dossierplatform · POC demonstratie</span>
        <span class="font-mono">PROV-O · pip · Vue 3</span>
      </div>
    </footer>
  </div>
</template>

<style>
.fade-enter-active,
.fade-leave-active {
  transition: opacity 140ms ease;
}
.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}
</style>
