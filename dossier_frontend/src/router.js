import { createRouter, createWebHistory } from "vue-router";
import { useAuthStore } from "./stores/auth";

import LoginView from "./views/LoginView.vue";
import DashboardView from "./views/DashboardView.vue";
import DossierDetailView from "./views/DossierDetailView.vue";
import NewAanvraagView from "./views/NewAanvraagView.vue";

const routes = [
  { path: "/login", component: LoginView, meta: { public: true } },
  { path: "/", component: DashboardView, meta: { title: "Dossiers" } },
  {
    path: "/dossiers/:id",
    component: DossierDetailView,
    meta: { title: "Dossier" },
    props: true,
  },
  {
    path: "/nieuwe-aanvraag",
    component: NewAanvraagView,
    meta: { title: "Nieuwe aanvraag" },
  },
];

const router = createRouter({
  history: createWebHistory(),
  routes,
});

// Guard: must be logged in (= must have a current POC user) for
// everything except /login.
router.beforeEach((to) => {
  const auth = useAuthStore();
  if (!to.meta.public && !auth.isLoggedIn) {
    return { path: "/login" };
  }
  if (to.path === "/login" && auth.isLoggedIn) {
    return { path: "/" };
  }
});

export default router;
