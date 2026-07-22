/** @type {import('tailwindcss').Config} */
import espressoPreset from './chronicle-espresso-preset.js'

export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  darkMode: 'class',
  presets: [espressoPreset],
  theme: {
    extend: {},
  },
  plugins: [],
}
