import type { Config } from "tailwindcss";

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx,mjs}"],
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
        // Neutral (Apple-style) grays. The `stone` name stays so existing
        // classes keep working, but the scale is no longer warm.
        stone: {
          50: "#fafafa",
          100: "#f5f5f7",
          200: "#e5e5ea",
          300: "#d1d1d6",
          400: "#aeaeb2",
          500: "#8e8e93",
          600: "#6e6e73",
          700: "#48484a",
          800: "#2c2c2e",
          900: "#1d1d1f",
          950: "#000000",
        },
      },
      fontFamily: {
        sans: ["-apple-system", "BlinkMacSystemFont", "SF Pro Text", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "system-ui", "sans-serif"],
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
