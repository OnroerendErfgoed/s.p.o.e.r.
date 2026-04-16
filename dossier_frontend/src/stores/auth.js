// Auth store. The POC backend doesn't do real authentication —
// it just reads X-POC-User off each request and looks up a user
// definition by username. This store holds the currently-selected
// user and persists the choice in localStorage so a page refresh
// doesn't kick you back to the login page.

import { defineStore } from "pinia";
import { ref, computed } from "vue";

// The POC users from the toelatingen workflow.yaml, hard-coded so
// the frontend can show a user picker before any backend call is
// made. Keep this in sync if the workflow file changes.
const POC_USERS = [
  {
    username: "claeyswo",
    id: "1",
    name: "Wouter Claeys",
    role_summary: "Beheerder",
    description: "Ziet alle dossiers. Kan archiefexporten nemen.",
    roles: ["beheerder"],
  },
  {
    username: "jan.aanvrager",
    id: "aaa00000-0000-0000-0000-000000000001",
    name: "Jan Peeters",
    role_summary: "Aanvrager (rijksregisternummer)",
    description: "Particuliere aanvrager. Kan een nieuwe aanvraag indienen.",
    roles: ["85010100123"],
  },
  {
    username: "firma.acme",
    id: "aaa00000-0000-0000-0000-000000000002",
    name: "ACME BV",
    role_summary: "Aanvrager (onderneming)",
    description: "Aanvrager als rechtspersoon. Kan een nieuwe aanvraag indienen.",
    roles: ["kbo-toevoeger:0123456789"],
  },
  {
    username: "marie.brugge",
    id: "bbb00000-0000-0000-0000-000000000001",
    name: "Marie Vandenbroeck",
    role_summary: "Behandelaar Brugge",
    description: "Behandelt dossiers voor gemeente Brugge. Kan aanvragen bewerken en beslissingen nemen.",
    roles: ["behandelaar", "beslisser"],
  },
];

export const useAuthStore = defineStore("auth", () => {
  const currentUser = ref(loadFromStorage());

  function loadFromStorage() {
    try {
      const raw = localStorage.getItem("dossier.currentUser");
      if (!raw) return null;
      return JSON.parse(raw);
    } catch {
      return null;
    }
  }

  function setUser(user) {
    currentUser.value = user;
    if (user) {
      localStorage.setItem("dossier.currentUser", JSON.stringify(user));
    } else {
      localStorage.removeItem("dossier.currentUser");
    }
  }

  const availableUsers = computed(() => POC_USERS);
  const isLoggedIn = computed(() => !!currentUser.value);

  return { currentUser, setUser, availableUsers, isLoggedIn };
});
