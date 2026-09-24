import type { Config } from 'tailwindcss'

export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        // One colour per agent, reused by the timeline rail, the step badge
        // and the evidence chips so a step is identifiable at a glance.
        agent: {
          log: '#38bdf8',
          metrics: '#a78bfa',
          cause: '#fb923c',
          fix: '#34d399',
        },
      },
      fontFamily: {
        mono: ['ui-monospace', 'SFMono-Regular', 'Menlo', 'Consolas', 'monospace'],
      },
      keyframes: {
        'fade-up': {
          '0%': { opacity: '0', transform: 'translateY(8px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
        'pulse-ring': {
          '0%': { boxShadow: '0 0 0 0 rgba(56, 189, 248, 0.5)' },
          '70%': { boxShadow: '0 0 0 10px rgba(56, 189, 248, 0)' },
          '100%': { boxShadow: '0 0 0 0 rgba(56, 189, 248, 0)' },
        },
      },
      animation: {
        'fade-up': 'fade-up 0.35s ease-out',
        'pulse-ring': 'pulse-ring 1.8s ease-out infinite',
      },
    },
  },
  plugins: [],
} satisfies Config
