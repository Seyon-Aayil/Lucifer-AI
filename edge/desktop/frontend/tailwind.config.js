/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg: "#0A0B0F",
        surface: "#13151B",
        "surface-elev": "#1B1E27",
        border: "#23262F",
        text: "#E8E9EE",
        "text-2": "#9CA0AB",
        "text-3": "#6B6F7B",
        primary: "#7C5CFF",
        success: "#4ADE80",
        warn: "#FBBF24",
        danger: "#F87171",
        info: "#60A5FA",
        agent: {
          personal: "#7C5CFF",
          coding: "#22D3EE",
          financial: "#4ADE80",
          health: "#F472B6",
          research: "#FBBF24",
          librarian: "#9CA0AB",
          news: "#60A5FA",
        },
      },
      fontFamily: {
        sans: ["Geist", "-apple-system", "SF Pro Text", "system-ui", "sans-serif"],
        mono: ["JetBrains Mono", "ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
      },
      borderRadius: {
        DEFAULT: "12px",
        lg: "16px",
      },
      boxShadow: {
        glass:
          "inset 0 0 0 1px rgba(255,255,255,0.06), 0 8px 24px rgba(0,0,0,0.40)",
      },
    },
  },
  plugins: [],
};
