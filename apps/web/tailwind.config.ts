import type { Config } from "tailwindcss";

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Muted teal brand scale — low-saturation, content-first.
        manor: {
          50: "#f2f6f5",
          100: "#e5eeeb",
          200: "#ccded9",
          300: "#abccc4",
          400: "#82ada4",
          500: "#5f928a",
          600: "#4f7d75",
          700: "#436b65",
          800: "#395a54",
          900: "#314c47",
        },
      },
      fontFamily: {
        sans: ["Inter", "-apple-system", "BlinkMacSystemFont", "system-ui", "sans-serif"],
        mono: ['"JetBrains Mono"', "ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
      },
      borderRadius: {
        glass: "16px",
        "glass-lg": "20px",
      },
      backdropBlur: {
        glass: "16px",
      },
    },
  },
  plugins: [],
} satisfies Config;
