/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{vue,js,ts}"],
  theme: {
    extend: {
      colors: {
        // Institutional, restrained. "Ink" replaces generic black;
        // "paper" is the off-white background; "brass" is the single
        // accent — evokes heritage metalwork, not startup purple.
        ink: {
          DEFAULT: "#1a1814",
          muted: "#4a4742",
          soft: "#8b867d",
          line: "#d9d4c9",
        },
        paper: {
          DEFAULT: "#faf7f0",
          tint: "#f3ede0",
          card: "#fffdf7",
        },
        brass: {
          DEFAULT: "#9e7e3a",
          dark: "#7a5f27",
          light: "#d4b878",
          pale: "#f0e6cc",
        },
        // Status colours kept dim — these are dossier states, not
        // iOS notification badges. Used as small filled pills.
        status: {
          draft: "#7a6f5a",
          submitted: "#4a5568",
          review: "#8b6914",
          approved: "#4a6e4a",
          rejected: "#8b3a3a",
        },
      },
      fontFamily: {
        // Pair a distinctive serif with a compact sans. Both load from
        // Google Fonts. Avoided the "Inter everywhere" default.
        display: ['"Source Serif 4"', '"Source Serif Pro"', "Georgia", "serif"],
        sans: ['"IBM Plex Sans"', "system-ui", "sans-serif"],
        mono: ['"IBM Plex Mono"', "ui-monospace", "monospace"],
      },
      letterSpacing: {
        wider: "0.08em",
        widest: "0.18em",
      },
      boxShadow: {
        // One subtle shadow, sparingly used. No blur clouds.
        paper: "0 1px 0 rgba(26, 24, 20, 0.06)",
      },
    },
  },
  plugins: [],
};
